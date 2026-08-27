#!/usr/bin/env python3
"""Uplink daemon — mirror shared local conversations up to the MASTER homeserver.

PLAN-MASTER-SYNC.md §5.4 / §7 / §8. A headless, OUTBOUND-ONLY Matrix client. It
authenticates to the teammate's LOCAL homeserver (read) and to the MASTER
homeserver (write, as the teammate's own scoped account). It never listens on a
socket, never sends to any external network, and never holds a bridge session or
external send-capability.

Security invariants enforced here:
  - Outbound only: two urllib clients, no server. Master is written strictly as
    the teammate's scoped account (MASTER_TOKEN); the manager is invited at PL 0
    with events_default 50 so the manager can read but never send (§8.3).
  - One-way: events flow LOCAL -> MASTER only. Nothing is read from MASTER and
    applied to LOCAL.
  - Consent boundary: only rooms whose effective state resolves to shared
    (agents/uplink/consent.py, a byte-parity port of shared/model/consent.js)
    are mirrored. Flip-to-not-shared DELETES the master mirror (revocation).
  - Idempotency: SQLite event_map prevents duplicate posts across restarts; the
    per-room watermark advances ONLY after the master confirms 200 OK. When the
    master is unreachable the daemon buffers and retries with backoff and does
    NOT advance the watermark (§7, §8.2, §11).

Config is via environment only (no config file, no secrets on argv):
  LOCAL_HS_URL   base URL of the teammate's local homeserver (e.g. http://127.0.0.1:8008)
  LOCAL_USER     the teammate's local mxid  (e.g. @jkali:localhost)
  LOCAL_TOKEN    the teammate's local access token
  MASTER_HS_URL  base URL of the master homeserver (e.g. http://127.0.0.1:8018)
  MASTER_USER    the teammate's master mxid  (e.g. @alice:master)
  MASTER_TOKEN   the teammate's SCOPED master access token
  MANAGER_MXID   the manager mxid to invite read-only (e.g. @manager:master)
  MASTER_SPACE   the teammate's per-user space room id on master (e.g. !RbaZ...:master)
Optional:
  UPLINK_DB          state db path       (default: <this dir>/state.db)
  UPLINK_BACKFILL    backfill message cap (default: 500)
  UPLINK_SYNC_TIMEOUT  /sync long-poll ms (default: 30000)
  UPLINK_LOG_LEVEL   INFO|DEBUG           (default: INFO)

Python 3.9+ stdlib only (urllib + sqlite3). No pip dependencies.
"""
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Pure logic (no I/O) lives beside this file so tests import it without booting
# the daemon. sys.path already contains this dir when run as a script; add it
# explicitly so `python3 agents/uplink/uplink.py` works from any cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import consent            # noqa: E402
import reconcile          # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))

# ---- source detection: the known bridge spaces, mirroring shared/ui/sources.js.
# A local room's source is the source-space (by m.room.name) it is a child of.
SOURCE_LABEL_TO_ID = {
    "WhatsApp": "whatsapp",
    "iMessage": "imessage",
    "Google Messages": "gmessages",
    "Instagram": "instagram",
    "LinkedIn": "linkedin",
    "Twitter": "twitter",
    "X": "twitter",
}
SOURCE_ID_TO_LABEL = {
    "whatsapp": "WhatsApp", "imessage": "iMessage", "gmessages": "Google Messages",
    "instagram": "Instagram", "linkedin": "LinkedIn", "twitter": "X",
}

SOURCE_TAG_TYPE = "com.jkali.source"        # state event on the mirror room (badge)
FROM_ME_KEY = "com.jkali.from_me"
ORIGIN_TS_KEY = "com.jkali.origin_ts"
SOURCE_KEY = "com.jkali.source"
ORIGIN_SENDER_KEY = "com.jkali.origin_sender"

log = logging.getLogger("uplink")


class MasterUnreachable(Exception):
    """Raised when the MASTER homeserver cannot be reached (buffer + backoff)."""


class Config:
    def __init__(self, env):
        def req(k):
            v = env.get(k)
            if not v:
                sys.stderr.write("uplink: missing required env %s\n" % k)
                sys.exit(2)
            return v
        self.local_hs = req("LOCAL_HS_URL").rstrip("/")
        self.local_user = req("LOCAL_USER")
        self.local_token = req("LOCAL_TOKEN")
        self.master_hs = req("MASTER_HS_URL").rstrip("/")
        self.master_user = req("MASTER_USER")
        self.master_token = req("MASTER_TOKEN")
        self.manager_mxid = req("MANAGER_MXID")
        self.master_space = req("MASTER_SPACE")
        self.db_path = env.get("UPLINK_DB") or os.path.join(BASE, "state.db")
        self.backfill = max(0, min(int(env.get("UPLINK_BACKFILL", "500")), 500))
        self.sync_timeout = int(env.get("UPLINK_SYNC_TIMEOUT", "30000"))
        self.log_level = env.get("UPLINK_LOG_LEVEL", "INFO")


# --------------------------------------------------------------------- matrix
def _mx(base, token, method, path, body=None, query=None, timeout=60):
    """One Matrix client-server API call. Returns parsed JSON (or {} on 200/no body).

    Raises MasterUnreachable on a transport error against a master URL so the
    caller can buffer; re-raises HTTPError otherwise.
    """
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


class Uplink:
    def __init__(self, cfg):
        self.cfg = cfg
        self.db = self._open_db(cfg.db_path)
        self.backoff = 1.0

    # -- local (read) and master (write) transports -------------------------
    def local(self, method, path, body=None, query=None, timeout=60):
        return _mx(self.cfg.local_hs, self.cfg.local_token, method, path, body, query, timeout)

    def master(self, method, path, body=None, query=None, timeout=60):
        try:
            return _mx(self.cfg.master_hs, self.cfg.master_token, method, path, body, query, timeout)
        except urllib.error.HTTPError:
            raise  # a real API error (auth, forbidden, bad request) — surface it
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise MasterUnreachable(str(e))

    # -- state db -----------------------------------------------------------
    @staticmethod
    def _open_db(path):
        db = sqlite3.connect(path, check_same_thread=False)
        db.execute(
            "CREATE TABLE IF NOT EXISTS mirror_rooms ("
            "local_room_id TEXT PRIMARY KEY, master_room_id TEXT UNIQUE, "
            "source TEXT, last_synced_pos TEXT)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS event_map ("
            "local_event_id TEXT PRIMARY KEY, master_event_id TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        db.commit()
        try:
            os.chmod(path, 0o600)  # secrets/state 600 regardless of umask
        except OSError:
            pass
        return db

    def meta_get(self, k, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def meta_set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)", (k, v))
        self.db.commit()

    def mirror_for(self, local_room_id):
        r = self.db.execute(
            "SELECT master_room_id, source, last_synced_pos FROM mirror_rooms "
            "WHERE local_room_id=?", (local_room_id,)).fetchone()
        return r  # (master_room_id, source, last_synced_pos) or None

    def existing_mirror_ids(self):
        return [r[0] for r in self.db.execute(
            "SELECT local_room_id FROM mirror_rooms").fetchall()]

    def mapped_master_event(self, local_event_id):
        r = self.db.execute(
            "SELECT master_event_id FROM event_map WHERE local_event_id=?",
            (local_event_id,)).fetchone()
        return r[0] if r else None

    def mapped_ids_for_room(self, local_room_id):
        # event_map is global-keyed by local event id; membership is what the
        # idempotency filter needs, so a single set lookup suffices.
        return {r[0] for r in self.db.execute(
            "SELECT local_event_id FROM event_map").fetchall()}

    # -- consent ------------------------------------------------------------
    def read_policy(self):
        path = ("/_matrix/client/v3/user/" + urllib.parse.quote(self.cfg.local_user, safe="")
                + "/account_data/" + consent.SHARE_POLICY_TYPE)
        try:
            return consent.normalize_policy(self.local("GET", path))
        except urllib.error.HTTPError:
            return {"global": "private", "sources": {}}  # absent -> safe default

    # -- source detection from a /sync response -----------------------------
    @staticmethod
    def sources_from_sync(sync_data):
        """Map local_room_id -> source_id via source-space m.space.child state.

        A room is attributed to a source when it is a child of a space whose
        m.room.name matches a known bridge label (shared/ui/sources.js). Rooms
        not under any known source space have source None and are left private.
        """
        join = (((sync_data or {}).get("rooms") or {}).get("join")) or {}
        # First pass: find source spaces and their names.
        space_source = {}
        child_of = {}
        for rid, room in join.items():
            state_events = ((room.get("state") or {}).get("events")) or []
            name = None
            children = []
            for e in state_events:
                if not isinstance(e, dict):
                    continue
                t = e.get("type")
                if t == "m.room.name":
                    name = (e.get("content") or {}).get("name")
                elif t == "m.space.child" and (e.get("content") or {}).get("via"):
                    children.append(e.get("state_key"))
            if name in SOURCE_LABEL_TO_ID:
                space_source[rid] = SOURCE_LABEL_TO_ID[name]
                for c in children:
                    child_of[c] = rid
        out = {}
        for child_rid, space_rid in child_of.items():
            src = space_source.get(space_rid)
            if src:
                out[child_rid] = src
        return out

    @staticmethod
    def room_name_from_sync(room):
        for e in (((room or {}).get("state") or {}).get("events")) or []:
            if isinstance(e, dict) and e.get("type") == "m.room.name":
                return (e.get("content") or {}).get("name")
        return None

    # -- reconcile ----------------------------------------------------------
    def full_sync(self):
        """A bounded full /sync of the LOCAL hs: state + account-data + recent."""
        flt = json.dumps({
            "room": {"timeline": {"limit": 1}, "state": {"lazy_load_members": False}},
        })
        return self.local("GET", "/_matrix/client/v3/sync",
                           query={"filter": flt, "timeout": "0"}, timeout=120)

    def desired_shared(self, sync_data):
        """Return (desired {local_room_id: bool}, source_of {room_id: source})."""
        policy = self.read_policy()
        overrides = consent.overrides_from_sync(sync_data)
        source_of = self.sources_from_sync(sync_data)
        join = (((sync_data or {}).get("rooms") or {}).get("join")) or {}
        desired = {}
        for rid in join:
            src = source_of.get(rid)
            if not src:
                continue  # not a bridged conversation room -> never shared
            convo = {"id": rid, "sourceId": src, "sourceLabel": SOURCE_ID_TO_LABEL.get(src, src)}
            desired[rid] = consent.effective_shared(convo, policy, overrides.get(rid))
        return desired, source_of, join

    def reconcile(self):
        sync_data = self.full_sync()
        desired, source_of, join = self.desired_shared(sync_data)
        plan = reconcile.reconcile_decisions(desired, self.existing_mirror_ids())
        log.info("reconcile: create=%d delete=%d keep=%d",
                 len(plan["create"]), len(plan["delete"]), len(plan["keep"]))
        for rid in plan["create"]:
            try:
                self.create_mirror(rid, source_of.get(rid), self.room_name_from_sync(join.get(rid)))
            except MasterUnreachable:
                raise
            except urllib.error.HTTPError as e:
                log.warning("create_mirror %s failed: %s", rid, e)
        for rid in plan["delete"]:
            try:
                self.delete_mirror(rid)
            except MasterUnreachable:
                raise
            except urllib.error.HTTPError as e:
                log.warning("delete_mirror %s failed: %s", rid, e)
        # Backfill/tail every kept + freshly-created room.
        for rid in sorted(set(plan["create"]) | set(plan["keep"])):
            self.sync_room(rid)

    # -- mirror lifecycle ---------------------------------------------------
    def create_mirror(self, local_room_id, source, name):
        """Create a mirror room on MASTER, add to space, tag source, invite mgr."""
        cfg = self.cfg
        body = {
            "name": name or "conversation",
            "preset": "private_chat",
            "invite": [cfg.manager_mxid],
            "creation_content": {"com.jkali.mirror_of": local_room_id},
            "initial_state": [
                {"type": SOURCE_TAG_TYPE, "state_key": "", "content": {"source": source or "unknown"}},
                {"type": "m.space.parent", "state_key": cfg.master_space,
                 "content": {"via": [self._server_name(cfg.master_user)], "canonical": True}},
            ],
            # Read-only enforcement (§8.3): owner writes (100), events_default 50,
            # manager pinned to 0 -> manager can read, cannot send.
            "power_level_content_override": {
                "users": {cfg.master_user: 100, cfg.manager_mxid: 0},
                "events_default": 50,
                "invite": 100, "kick": 100, "ban": 100, "redact": 100,
                "state_default": 100,
            },
        }
        res = self.master("POST", "/_matrix/client/v3/createRoom", body)
        master_room_id = res["room_id"]
        # Link the space -> child (so the master app groups it under this user).
        self.master("PUT", "/_matrix/client/v3/rooms/"
                    + urllib.parse.quote(cfg.master_space, safe="")
                    + "/state/m.space.child/" + urllib.parse.quote(master_room_id, safe=""),
                    {"via": [self._server_name(cfg.master_user)]})
        self.db.execute(
            "INSERT OR REPLACE INTO mirror_rooms "
            "(local_room_id, master_room_id, source, last_synced_pos) VALUES (?,?,?,?)",
            (local_room_id, master_room_id, source, None))
        self.db.commit()
        log.info("created mirror %s -> %s (source=%s)", local_room_id, master_room_id, source)
        self.backfill(local_room_id, master_room_id)
        return master_room_id

    def delete_mirror(self, local_room_id):
        """Revoke: remove the master copy from the manager's view, drop state.

        A CS-API client cannot purge a room server-side (that needs the Synapse
        admin API). It CAN revoke the manager's access and orphan the room:
        remove the space child, kick the manager, then leave. The durable copy
        stops being reachable by the manager, satisfying §9 revocation. A note is
        logged for the operator's admin-side purge if desired.
        """
        row = self.mirror_for(local_room_id)
        if not row:
            return
        master_room_id, _, _ = row
        cfg = self.cfg
        try:
            self.master("PUT", "/_matrix/client/v3/rooms/"
                        + urllib.parse.quote(cfg.master_space, safe="")
                        + "/state/m.space.child/" + urllib.parse.quote(master_room_id, safe=""), {})
        except urllib.error.HTTPError:
            pass
        for act, payload in (("kick", {"user_id": cfg.manager_mxid, "reason": "unshared"}),):
            try:
                self.master("POST", "/_matrix/client/v3/rooms/"
                            + urllib.parse.quote(master_room_id, safe="") + "/" + act, payload)
            except urllib.error.HTTPError:
                pass
        try:
            self.master("POST", "/_matrix/client/v3/rooms/"
                        + urllib.parse.quote(master_room_id, safe="") + "/leave", {})
        except urllib.error.HTTPError:
            pass
        # Drop local mappings so a later re-share creates a fresh mirror.
        self.db.execute("DELETE FROM mirror_rooms WHERE local_room_id=?", (local_room_id,))
        self.db.commit()
        log.info("revoked mirror for %s (master room %s orphaned; admin purge optional)",
                 local_room_id, master_room_id)

    # -- backfill + tail ----------------------------------------------------
    def backfill(self, local_room_id, master_room_id):
        """Post the last N messages in chronological order, then set watermark."""
        res = self.local("GET", "/_matrix/client/v3/rooms/"
                         + urllib.parse.quote(local_room_id, safe="") + "/messages",
                         query={"dir": "b", "limit": str(self.cfg.backfill)})
        chunk = list(reversed(res.get("chunk") or []))  # oldest -> newest
        posted = self.forward_events(local_room_id, master_room_id, chunk)
        # Watermark starts at the sync 'from' token for this room's tail.
        end = res.get("start")  # forward token for subsequent /messages if needed
        if end:
            self._set_watermark(local_room_id, self.meta_get("sync_since") or "")
        log.info("backfill %s: posted %d/%d", local_room_id, posted, len(chunk))

    def sync_room(self, local_room_id):
        """Placeholder per-room catch-up hook; tail is driven by the global loop."""
        return

    def _set_watermark(self, local_room_id, pos):
        self.db.execute("UPDATE mirror_rooms SET last_synced_pos=? WHERE local_room_id=?",
                        (pos, local_room_id))
        self.db.commit()

    def forward_events(self, local_room_id, master_room_id, events):
        """Forward a list of local timeline events to the mirror room, in order.

        Idempotent: events already in event_map are skipped (no dup on replay).
        Preserves com.jkali.from_me, com.jkali.origin_ts, source, and the origin
        sender display name. Returns the count actually posted. Raises
        MasterUnreachable (via self.master) without advancing anything on a
        transport failure, so the caller buffers + retries.
        """
        row = self.mirror_for(local_room_id)
        source = row[1] if row else None
        mapped = {r[0] for r in self.db.execute(
            "SELECT local_event_id FROM event_map").fetchall()}
        order = [e.get("event_id") for e in events if isinstance(e, dict)]
        todo = set(reconcile.select_new_events(order, mapped))
        posted = 0
        for e in events:
            if not isinstance(e, dict):
                continue
            eid = e.get("event_id")
            if eid not in todo:
                continue
            etype = e.get("type")
            if etype == "m.room.message":
                self._forward_message(local_room_id, master_room_id, source, e)
                posted += 1
            elif etype == "m.room.redaction":
                self._forward_redaction(master_room_id, e)
                posted += 1
            todo.discard(eid)
        return posted

    def _display_name(self, local_room_id, sender):
        """Best-effort origin sender display name from local room member state."""
        try:
            res = self.local("GET", "/_matrix/client/v3/rooms/"
                             + urllib.parse.quote(local_room_id, safe="")
                             + "/state/m.room.member/" + urllib.parse.quote(sender, safe=""))
            return res.get("displayname") or sender
        except urllib.error.HTTPError:
            return sender

    def _forward_message(self, local_room_id, master_room_id, source, ev):
        content = dict(ev.get("content") or {})
        sender = ev.get("sender") or ""
        rel = content.get("m.relates_to") or {}
        # Edit: an m.replace targeting a mapped local event -> edit the mirror.
        if rel.get("rel_type") == "m.replace":
            target_local = rel.get("event_id")
            master_target = self.mapped_master_event(target_local)
            if master_target:
                content = dict(content)
                content["m.relates_to"] = {"rel_type": "m.replace", "event_id": master_target}
        # Metadata the master app renders by (§8.2).
        content[FROM_ME_KEY] = (sender == self.cfg.local_user)
        content[ORIGIN_TS_KEY] = ev.get("origin_server_ts")
        content[SOURCE_KEY] = source or "unknown"
        content[ORIGIN_SENDER_KEY] = self._display_name(local_room_id, sender)
        # Media placeholder (v1): never leak a filename/mxc; show a label.
        mt = content.get("msgtype")
        if mt in ("m.image", "m.video", "m.audio", "m.file"):
            label = {"m.image": "Photo", "m.video": "Video", "m.audio": "Audio", "m.file": "File"}[mt]
            content = {k: v for k, v in content.items() if k not in ("url", "file", "info")}
            content["body"] = label
            content["com.jkali.media_placeholder"] = True
        txn = urllib.parse.quote(ev.get("event_id"), safe="")
        res = self.master("PUT", "/_matrix/client/v3/rooms/"
                          + urllib.parse.quote(master_room_id, safe="")
                          + "/send/m.room.message/uplink_" + txn, content)
        self.db.execute(
            "INSERT OR REPLACE INTO event_map (local_event_id, master_event_id) VALUES (?,?)",
            (ev.get("event_id"), res.get("event_id")))
        self.db.commit()

    def _forward_redaction(self, master_room_id, ev):
        target_local = ev.get("redacts") or (ev.get("content") or {}).get("redacts")
        master_target = self.mapped_master_event(target_local)
        if not master_target:
            return
        txn = urllib.parse.quote(ev.get("event_id"), safe="")
        res = self.master("PUT", "/_matrix/client/v3/rooms/"
                          + urllib.parse.quote(master_room_id, safe="")
                          + "/redact/" + urllib.parse.quote(master_target, safe="")
                          + "/uplinkr_" + txn, {})
        self.db.execute(
            "INSERT OR REPLACE INTO event_map (local_event_id, master_event_id) VALUES (?,?)",
            (ev.get("event_id"), res.get("event_id")))
        self.db.commit()

    @staticmethod
    def _server_name(mxid):
        return mxid.split(":", 1)[1] if ":" in mxid else mxid

    # -- main loop ----------------------------------------------------------
    def tail_once(self):
        """One /sync of the LOCAL hs; forward new events in shared mirror rooms."""
        since = self.meta_get("sync_since")
        query = {"timeout": str(self.cfg.sync_timeout)}
        if since:
            query["since"] = since
        data = self.local("GET", "/_matrix/client/v3/sync", query=query,
                          timeout=(self.cfg.sync_timeout // 1000) + 30)
        join = (((data or {}).get("rooms") or {}).get("join")) or {}
        for local_room_id, room in join.items():
            row = self.mirror_for(local_room_id)
            if not row:
                continue  # only mirror rooms are tailed
            master_room_id = row[0]
            events = ((room.get("timeline") or {}).get("events")) or []
            self.forward_events(local_room_id, master_room_id, events)
            # Advance the per-room watermark only after the above succeeded.
            self._set_watermark(local_room_id, data.get("next_batch") or "")
        # Global sync token advances only after every room forwarded (no master
        # failure raised MasterUnreachable above).
        self.meta_set("sync_since", data.get("next_batch") or since or "")

    def run(self):
        log.info("uplink starting: local=%s master=%s space=%s manager=%s",
                 self.cfg.local_hs, self.cfg.master_hs, self.cfg.master_space, self.cfg.manager_mxid)
        while True:
            try:
                self.reconcile()
                self.tail_once()
                self.backoff = 1.0
            except MasterUnreachable as e:
                log.warning("master unreachable (%s); buffering, retry in %.0fs "
                            "(watermark NOT advanced)", e, self.backoff)
                time.sleep(self.backoff)
                self.backoff = min(self.backoff * 2, 60.0)
            except urllib.error.HTTPError as e:
                log.error("http error: %s", e)
                time.sleep(5)
            except KeyboardInterrupt:
                log.info("uplink stopping")
                return


def main():
    cfg = Config(os.environ)
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    Uplink(cfg).run()


if __name__ == "__main__":
    main()
