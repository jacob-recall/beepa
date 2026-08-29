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
  UPLINK_MEDIA_MAX   media re-upload byte cap (default: 26214400 = 25MB; over -> placeholder)
  UPLINK_SYNC_TIMEOUT  /sync long-poll ms for the LOCAL tail AND the master
                       proposal pull (default: 5000). The idle loop period is
                       roughly the SUM of the two long-polls, so keep it short:
                       each side's worst-case forwarding latency is about the
                       OTHER side's long-poll plus processing.
  UPLINK_RECONCILE_MS  min ms between full reconcile passes (default: 30000).
                       Reconcile does a full initial /sync of the local hs, so
                       it stays on its own slower cadence while tail/pull spin.
  UPLINK_LOG_LEVEL   INFO|DEBUG           (default: INFO)

Python 3.9+ stdlib only (urllib + sqlite3). No pip dependencies.
"""
import json
import logging
import os
import re
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
# §12 phase 5 unified contacts: a conversation may belong to a contact profile
# (one person across sources). Profiles live in user account-data
# com.jkali.contact_profiles (shared/model/contacts.js). When the uplink mirrors
# a member of a SHARED profile it STAMPS the mirror room with com.jkali.profile
# {id, displayName} so the master can group that person's threads together.
CONTACT_PROFILES_TYPE = "com.jkali.contact_profiles"  # user account-data
PROFILE_TAG_TYPE = "com.jkali.profile"      # state event on the mirror room
PROFILE_SHARE_STATES = {"share", "private", "inherit"}
FROM_ME_KEY = "com.jkali.from_me"
ORIGIN_TS_KEY = "com.jkali.origin_ts"
SOURCE_KEY = "com.jkali.source"
ORIGIN_SENDER_KEY = "com.jkali.origin_sender"
MEDIA_PLACEHOLDER_KEY = "com.jkali.media_placeholder"
# The teammate's own bridge-ghost mxids (self-align, shared/ui/account-data.js
# source (1)): messages the teammate sends from the native app (phone WhatsApp,
# etc.) arrive with the GHOST as sender, not cfg.local_user, so from_me must
# also match this user-written allowlist. Only the authoritative account-data
# list is used here — the UI's cosmetic frequency heuristic (source (2)) is
# deliberately NOT ported, because this stamp is a durable record on master.
SELF_IDENTITIES_TYPE = "com.jkali.self_identities"

# V2 proposal channel (PLAN-MASTER-SYNC.md §2 v2 / §7). A DEDICATED per-teammate
# proposal room on the master carries manager-authored com.jkali.proposal events;
# the uplink pulls each new one DOWN into a DEDICATED LOCAL proposals room it owns
# on the teammate hub. Both rooms are marked with the com.jkali.proposals state
# event. Proposals NEVER live in a mirror/conversation room and are NEVER
# auto-sent — the teammate reviews and sends from their own guarded local path.
PROPOSAL_TYPE = "com.jkali.proposal"        # timeline event carrying a suggestion
PROPOSALS_MARKER = "com.jkali.proposals"    # state marker on a proposals room

# Byte-parity with shared/matrix/client.js MXC_RE (server / media-id charset).
MXC_RE = re.compile(r"^mxc://([A-Za-z0-9.\-:]+)/([A-Za-z0-9_-]+)$")
# Generic Matrix room-id shape (any server_name) — the proposal's target_room is
# a teammate-LOCAL room id, so it is validated by shape, not by a server-pinned
# regex. The uplink never SENDS to target_room; it only records it inside the
# forwarded com.jkali.proposal event for the teammate's guarded send path to
# re-validate later.
ROOMID_RE = re.compile(r"^![^:]+:[A-Za-z0-9.\-:]+$")
# Mxid shape gate for self_identities entries. shared/ui/account-data.js pins
# its MXID_RE to ':localhost'; here the same-server constraint is enforced in
# read_self_mxids() against cfg.local_user's own server name instead of a
# hardcoded literal, so the daemon stays deployable off-localhost.
MXID_RE = re.compile(r"^@[^:]+:[A-Za-z0-9.\-:]+$")
MEDIA_MSGTYPES = ("m.image", "m.video", "m.audio", "m.file")
MEDIA_LABELS = {"m.image": "Photo", "m.video": "Video", "m.audio": "Audio", "m.file": "File"}
DEFAULT_MEDIA_MAX = 25 * 1024 * 1024        # 25 MB re-upload cap (§8.2, v1.5)

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
        # MASTER_* are OPTIONAL. They may come from env (the link.sh install path)
        # OR from the local hub account-data com.jkali.master_link, which the user
        # app's Settings > "Connect to organization" writes after redeeming an
        # enrollment code. Uplink.refresh_master_config() resolves the effective
        # values each loop; account-data wins over env when present.
        self.master_hs = (env.get("MASTER_HS_URL") or "").rstrip("/")
        self.master_user = env.get("MASTER_USER") or ""
        self.master_token = env.get("MASTER_TOKEN") or ""
        self.manager_mxid = env.get("MANAGER_MXID") or ""
        self.master_space = env.get("MASTER_SPACE") or ""
        self.env_master = {
            "master_hs": self.master_hs, "master_user": self.master_user,
            "master_token": self.master_token, "manager_mxid": self.manager_mxid,
            "master_space": self.master_space,
        }
        self.db_path = env.get("UPLINK_DB") or os.path.join(BASE, "state.db")
        self.backfill = max(0, min(int(env.get("UPLINK_BACKFILL", "500")), 500))
        self.sync_timeout = int(env.get("UPLINK_SYNC_TIMEOUT", "5000"))
        self.reconcile_ms = int(env.get("UPLINK_RECONCILE_MS", "30000"))
        self.log_level = env.get("UPLINK_LOG_LEVEL", "INFO")
        # v1.5 media re-upload: skip (-> placeholder) anything above this many bytes.
        self.media_max = max(0, int(env.get("UPLINK_MEDIA_MAX", str(DEFAULT_MEDIA_MAX))))


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
        self.self_mxids = set()   # refreshed at the top of every reconcile()
        self._last_sourceless = None  # change-detector for the sourceless-share warning
        self._last_reconcile = 0.0    # monotonic-enough throttle for reconcile()

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
        # V2 proposal-pull dedup: master proposal event id -> the local event id
        # we wrote into the local proposals room. Mirrors event_map's role but for
        # the DOWN direction, so a restart/replay never double-posts a proposal.
        db.execute(
            "CREATE TABLE IF NOT EXISTS proposal_map ("
            "master_event_id TEXT PRIMARY KEY, local_event_id TEXT)")
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
    def read_self_mxids(self):
        """The teammate's own ghost mxids from com.jkali.self_identities.

        Mirrors fetchSelfIdentityAccountData() in shared/ui/account-data.js:
        admit only well-formed mxid strings; absent/error -> empty set.
        """
        path = ("/_matrix/client/v3/user/" + urllib.parse.quote(self.cfg.local_user, safe="")
                + "/account_data/" + SELF_IDENTITIES_TYPE)
        try:
            data = self.local("GET", path)
        except urllib.error.HTTPError:
            return set()
        mxids = data.get("mxids") if isinstance(data, dict) else None
        local_server = self.cfg.local_user.rsplit(":", 1)[-1]
        return {m for m in (mxids if isinstance(mxids, list) else [])
                if isinstance(m, str) and MXID_RE.match(m)
                and m.rsplit(":", 1)[-1] == local_server}

    def read_policy(self):
        path = ("/_matrix/client/v3/user/" + urllib.parse.quote(self.cfg.local_user, safe="")
                + "/account_data/" + consent.SHARE_POLICY_TYPE)
        try:
            return consent.normalize_policy(self.local("GET", path))
        except urllib.error.HTTPError:
            return {"global": "private", "sources": {}}  # absent -> safe default

    def read_profiles(self):
        """Return {local_room_id: {'id','displayName','share'}} from account-data.

        Reads com.jkali.contact_profiles and mirrors shared/model/contacts.js
        normalizeProfiles: a room belongs to AT MOST ONE profile (first profile
        wins), unknown share collapses to 'inherit', junk room ids dropped. A
        room with no profile is simply absent from the map. Absent event -> {}.
        """
        path = ("/_matrix/client/v3/user/" + urllib.parse.quote(self.cfg.local_user, safe="")
                + "/account_data/" + CONTACT_PROFILES_TYPE)
        try:
            data = self.local("GET", path)
        except urllib.error.HTTPError:
            return {}  # absent -> no profiles
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profiles, list):
            return {}
        out = {}
        seen_ids = set()
        claimed = set()
        for p in profiles:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            if not isinstance(pid, str) or not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            dn = p.get("displayName")
            dn = dn if isinstance(dn, str) else ""
            share = p.get("share")
            share = share if share in PROFILE_SHARE_STATES else "inherit"
            rooms = p.get("roomIds")
            if not isinstance(rooms, list):
                continue
            for rid in rooms:
                if not isinstance(rid, str) or not ROOMID_RE.match(rid):
                    continue
                if rid in claimed:
                    continue
                claimed.add(rid)
                out[rid] = {"id": pid, "displayName": dn, "share": share}
        return out

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
            # Prefix match to mirror shared/ui/sources.js (buildConvos uses
            # name.startsWith(spaceName)): real bridge spaces are named e.g.
            # "WhatsApp (+1...)" / "Google Messages (email)", not the bare label.
            # "X" is a whole-name label, so it is matched exactly (a prefix of
            # "X" would swallow arbitrary chat names).
            sid = None
            if name:
                for label, _sid in SOURCE_LABEL_TO_ID.items():
                    if label == "X":
                        if name == "X":
                            sid = _sid
                            break
                    elif name.startswith(label):
                        sid = _sid
                        break
            if sid:
                space_source[rid] = sid
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
        """Return (desired {room_id: bool}, source_of, join, profile_of).

        profile_of maps room_id -> {'id','displayName','share'} for rooms that
        belong to a contact profile (§12 phase 5). The profile share-state is
        applied by the 4-level resolver (per-conv override > profile > source >
        global > private) and also drives the master stamp in create_mirror.
        """
        policy = self.read_policy()
        overrides = consent.overrides_from_sync(sync_data)
        source_of = self.sources_from_sync(sync_data)
        profile_of = self.read_profiles()
        join = (((sync_data or {}).get("rooms") or {}).get("join")) or {}
        desired = {}
        for rid in join:
            src = source_of.get(rid)
            if not src:
                continue  # not a bridged conversation room -> never shared
            convo = {"id": rid, "sourceId": src, "sourceLabel": SOURCE_ID_TO_LABEL.get(src, src)}
            prof = profile_of.get(rid)
            profile_arg = ({"displayName": prof["displayName"], "share": prof["share"]}
                           if prof else None)
            desired[rid] = consent.effective_shared(convo, policy, overrides.get(rid), profile_arg)
        # Visibility (pm_mng-es1): an explicit per-room 'share' the source
        # detector cannot attribute is a consent decision this daemon cannot
        # honor — say so instead of silently skipping. Only explicit overrides
        # are checked (a share-all policy would flag every unbridged room),
        # and only on change so the loop does not repeat itself every pass.
        sourceless = sorted(rid for rid, st in overrides.items()
                            if st == "share" and rid in join and not source_of.get(rid))
        if sourceless != self._last_sourceless:
            self._last_sourceless = sourceless
            if sourceless:
                log.warning("shared-but-sourceless (will NOT mirror): %s",
                            ", ".join(sourceless))
        return desired, source_of, join, profile_of

    def reconcile(self):
        # Refresh once per pass; _forward_message (backfill + tail) reads it.
        self.self_mxids = self.read_self_mxids()
        sync_data = self.full_sync()
        desired, source_of, join, profile_of = self.desired_shared(sync_data)
        plan = reconcile.reconcile_decisions(desired, self.existing_mirror_ids())
        log.info("reconcile: create=%d delete=%d keep=%d",
                 len(plan["create"]), len(plan["delete"]), len(plan["keep"]))
        for rid in plan["create"]:
            try:
                # Stamp the mirror with the profile ONLY when the room is a member
                # of a SHARED profile (share=='share') — that is what groups a
                # person's threads on the master. A profile set to private/inherit
                # never mirrors via the profile level, so it never stamps.
                prof = profile_of.get(rid)
                stamp = ({"id": prof["id"], "displayName": prof["displayName"]}
                         if prof and prof.get("share") == "share" else None)
                self.create_mirror(rid, source_of.get(rid),
                                   self.room_name_from_sync(join.get(rid)), stamp)
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
    def create_mirror(self, local_room_id, source, name, profile=None):
        """Create a mirror room on MASTER, add to space, tag source, invite mgr.

        profile (when the room is a member of a SHARED contact profile) is
        {'id','displayName'} and is stamped as a com.jkali.profile state event so
        the master app can group this person's threads across platforms.
        """
        cfg = self.cfg
        initial_state = [
            {"type": SOURCE_TAG_TYPE, "state_key": "", "content": {"source": source or "unknown"}},
            {"type": "m.space.parent", "state_key": cfg.master_space,
             "content": {"via": [self._server_name(cfg.master_user)], "canonical": True}},
        ]
        if profile:
            initial_state.append({
                "type": PROFILE_TAG_TYPE, "state_key": "",
                "content": {"id": profile.get("id"),
                            "displayName": profile.get("displayName") or ""},
            })
        body = {
            "name": name or "conversation",
            "preset": "private_chat",
            "invite": [cfg.manager_mxid],
            "creation_content": {"com.jkali.mirror_of": local_room_id},
            "initial_state": initial_state,
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

    # -- media re-upload (v1.5) ---------------------------------------------
    @staticmethod
    def _http_bytes(base, token, method, path, query=None, data=None,
                    content_type=None, timeout=180, max_bytes=None):
        """A raw (non-JSON) client-server call for the media API.

        Returns (body_bytes, response_content_type). When max_bytes is set and the
        response body exceeds it, raises ValueError so the caller falls back to a
        placeholder instead of buffering an oversized blob into memory.
        """
        url = base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, method=method, data=data)
        req.add_header("Authorization", "Bearer " + token)
        if content_type:
            req.add_header("Content-Type", content_type)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if max_bytes is not None:
                body = r.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise ValueError("media body exceeds %d bytes" % max_bytes)
            else:
                body = r.read()
            return body, r.headers.get("Content-Type")

    def _reupload_media(self, content):
        """Download the media in `content` from LOCAL, re-upload to MASTER.

        Returns a NEW master mxc string on success, or None to signal the caller
        to fall back to the v1 placeholder. Any failure (bad/missing/encrypted
        mxc, over the size cap, download or upload error, or the master being
        unreachable) returns None; on a true master outage the subsequent message
        PUT will independently raise MasterUnreachable and roll the whole forward
        back, so a placeholder is never durably committed in that case.
        """
        url = content.get("url")
        if not isinstance(url, str):
            return None                      # encrypted media carries `file`, not `url`
        m = MXC_RE.match(url)
        if not m:
            return None
        server, media_id = m.group(1), m.group(2)
        info = content.get("info") if isinstance(content.get("info"), dict) else {}
        declared = info.get("size")
        if isinstance(declared, int) and declared > self.cfg.media_max:
            return None                      # size guard by declared size: skip download
        try:
            dl_path = ("/_matrix/client/v1/media/download/"
                       + urllib.parse.quote(server, safe="")
                       + "/" + urllib.parse.quote(media_id, safe=""))
            data, ctype = self._http_bytes(
                self.cfg.local_hs, self.cfg.local_token, "GET", dl_path,
                timeout=120, max_bytes=self.cfg.media_max)
            if not data:
                return None
            up_ctype = (info.get("mimetype") if isinstance(info.get("mimetype"), str)
                        else None) or ctype or "application/octet-stream"
            filename = content.get("body") if isinstance(content.get("body"), str) else "file"
            body, _ = self._http_bytes(
                self.cfg.master_hs, self.cfg.master_token, "POST",
                "/_matrix/media/v3/upload", query={"filename": filename},
                data=data, content_type=up_ctype, timeout=180)
            res = json.loads(body) if body else {}
            new_uri = res.get("content_uri")
            if isinstance(new_uri, str) and MXC_RE.match(new_uri):
                return new_uri
            return None
        except Exception as e:                # noqa: BLE001 — any failure -> placeholder
            log.warning("media re-upload failed (%s); falling back to placeholder", e)
            return None

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
        # Metadata the master app renders by (§8.2). from_me: the teammate's
        # own account OR one of their attested bridge ghosts (see
        # SELF_IDENTITIES_TYPE above) — phone-sent messages carry the ghost.
        content[FROM_ME_KEY] = (sender == self.cfg.local_user
                                or sender in self.self_mxids)
        content[ORIGIN_TS_KEY] = ev.get("origin_server_ts")
        content[SOURCE_KEY] = source or "unknown"
        content[ORIGIN_SENDER_KEY] = self._display_name(local_room_id, sender)
        # Media (v1.5): re-upload the blob from LOCAL to the MASTER media store and
        # post the NEW master mxc + preserved info/filename metadata. On ANY failure
        # (bad/encrypted mxc, over UPLINK_MEDIA_MAX, download/upload error) fall back
        # to the v1 placeholder — never drop or block the message.
        mt = content.get("msgtype")
        if mt in MEDIA_MSGTYPES:
            new_uri = self._reupload_media(content)
            if new_uri:
                content["url"] = new_uri            # swap local mxc -> master mxc
                content.pop("file", None)           # never carry a local encrypted ref
                content[MEDIA_PLACEHOLDER_KEY] = False
                # body / info / filename preserved as-is (metadata the renderer reads).
            else:
                content = {k: v for k, v in content.items() if k not in ("url", "file", "info")}
                content["body"] = MEDIA_LABELS[mt]
                content[MEDIA_PLACEHOLDER_KEY] = True
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

    # -- proposal channel (V2, master -> user, PLAN §2 v2 / §7) -------------
    # Direction is still teammate-initiated outbound: the uplink /syncs the
    # MASTER (as its own scoped account) for manager-authored com.jkali.proposal
    # events and writes each new one DOWN into a DEDICATED LOCAL proposals room.
    # HARD LIMITS enforced here:
    #   - the ONLY local write target is the recorded local proposals room;
    #     a mirror/conversation room is NEVER a target (checked before every PUT);
    #   - the ONLY event type written is com.jkali.proposal — NEVER m.room.message,
    #     so nothing here can ever look like, or become, an outgoing message;
    #   - nothing is auto-sent to any external network or bridge. The teammate
    #     reviews the proposal and sends it themselves via the guarded local path.
    def ensure_proposal_rooms(self):
        """Idempotently ensure both proposals rooms exist; record their ids.

        Master proposals room: created as the teammate's own scoped account,
        linked under the teammate's master space, marked com.jkali.proposals, and
        power-leveled so the manager may send ONLY com.jkali.proposal (an
        m.room.message would need events_default 100 the manager does not have) —
        defense in depth mirroring the read-only mirror-room power levels (§8.3).

        Local proposals room: created as the teammate's LOCAL account, marked
        com.jkali.proposals. It is NOT under any bridge source space, so the
        one-way mirror-up never considers it (desired_shared skips sourceless
        rooms) and it is never tailed for forwarding (not in mirror_rooms)."""
        cfg = self.cfg
        # Trust the recorded ids once created (no per-loop aliveness probe — that
        # is needless master/local request load every cycle). Rooms are durable;
        # a purged room would surface as a 404 on the next write and be handled.
        mpr = self.meta_get("master_proposals_room")
        if not mpr:
            body = {
                "name": "Proposals",
                "topic": "Manager suggestions for this teammate. Suggestions only — "
                         "nothing here is ever sent externally.",
                "preset": "private_chat",
                "invite": [cfg.manager_mxid],
                "initial_state": [
                    {"type": PROPOSALS_MARKER, "state_key": "", "content": {}},
                    {"type": "m.space.parent", "state_key": cfg.master_space,
                     "content": {"via": [self._server_name(cfg.master_user)], "canonical": True}},
                ],
                # Manager (PL 50) may send ONLY com.jkali.proposal (required 50).
                # events_default 100 => the manager cannot send m.room.message or
                # any other type. state_default 100 => cannot alter room state.
                "power_level_content_override": {
                    "users": {cfg.master_user: 100, cfg.manager_mxid: 50},
                    "events_default": 100,
                    "events": {PROPOSAL_TYPE: 50},
                    "state_default": 100,
                    "invite": 100, "kick": 100, "ban": 100, "redact": 100,
                },
            }
            res = self.master("POST", "/_matrix/client/v3/createRoom", body)
            mpr = res["room_id"]
            self.master("PUT", "/_matrix/client/v3/rooms/"
                        + urllib.parse.quote(cfg.master_space, safe="")
                        + "/state/m.space.child/" + urllib.parse.quote(mpr, safe=""),
                        {"via": [self._server_name(cfg.master_user)]})
            self.meta_set("master_proposals_room", mpr)
            # A fresh master room invalidates any old proposal watermark.
            self.db.execute("DELETE FROM meta WHERE k='proposal_sync_since'")
            self.db.commit()
            log.info("created master proposals room %s under space %s", mpr, cfg.master_space)

        lpr = self.meta_get("local_proposals_room")
        if not lpr:
            res = self.local("POST", "/_matrix/client/v3/createRoom", {
                "name": "Proposals from manager",
                "topic": "Suggested messages from the manager. Review each one and "
                         "send it yourself — nothing here is sent automatically.",
                "preset": "private_chat",
                "initial_state": [{"type": PROPOSALS_MARKER, "state_key": "", "content": {}}],
            })
            lpr = res["room_id"]
            self.meta_set("local_proposals_room", lpr)
            log.info("created local proposals room %s", lpr)

    def _sanitize_proposal(self, ev):
        """Whitelist a master proposal event into the local proposal content.

        Only known-safe fields are carried down; returns None for a malformed
        proposal (missing/invalid target_room or empty body) so it is recorded as
        handled and never retried. target_room is validated by SHAPE only (it is a
        teammate-LOCAL room id); the uplink never sends to it — the teammate's
        guarded local send path re-validates it against the live joined set."""
        c = ev.get("content") if isinstance(ev.get("content"), dict) else {}
        target = c.get("target_room")
        body = c.get("body")
        if not isinstance(target, str) or not ROOMID_RE.match(target):
            return None
        if not isinstance(body, str) or not body.strip():
            return None
        out = {
            "target_room": target,
            "body": body,
            "created_by": (c.get("created_by") if isinstance(c.get("created_by"), str)
                           else (ev.get("sender") or "")),
            "origin_ts": (c.get("origin_ts") if isinstance(c.get("origin_ts"), int)
                          else ev.get("origin_server_ts")),
            # Provenance back to the master event (audit; also the dedup txn seed).
            "com.jkali.proposal_source_event": ev.get("event_id"),
        }
        if c.get("template") is True:
            out["template"] = True
        return out

    def forward_proposals(self, master_room_id, local_proposals_room, events):
        """Write each NEW master proposal into the local proposals room, once.

        SAFETY: the write target is asserted to be exactly the recorded local
        proposals room (never a mirror/conversation room), and the event type is
        hardcoded com.jkali.proposal (never m.room.message). Idempotent via
        proposal_map. Returns the count actually posted."""
        recorded = self.meta_get("local_proposals_room")
        if not local_proposals_room or local_proposals_room != recorded:
            return 0
        # Never let the proposals target collide with a mirror room id.
        if self.db.execute("SELECT 1 FROM mirror_rooms WHERE master_room_id=?",
                            (local_proposals_room,)).fetchone():
            return 0
        mapped = {r[0] for r in self.db.execute(
            "SELECT master_event_id FROM proposal_map").fetchall()}
        posted = 0
        for ev in events:
            if not isinstance(ev, dict) or ev.get("type") != PROPOSAL_TYPE:
                continue
            meid = ev.get("event_id")
            if not meid or meid in mapped:
                continue
            clean = self._sanitize_proposal(ev)
            if clean is None:
                # Record as handled so a malformed proposal is not retried forever.
                self.db.execute("INSERT OR REPLACE INTO proposal_map "
                                "(master_event_id, local_event_id) VALUES (?,?)", (meid, None))
                self.db.commit()
                mapped.add(meid)
                continue
            txn = urllib.parse.quote(meid, safe="")
            res = self.local("PUT", "/_matrix/client/v3/rooms/"
                             + urllib.parse.quote(local_proposals_room, safe="")
                             + "/send/" + PROPOSAL_TYPE + "/proposal_" + txn, clean)
            self.db.execute("INSERT OR REPLACE INTO proposal_map "
                            "(master_event_id, local_event_id) VALUES (?,?)",
                            (meid, res.get("event_id")))
            self.db.commit()
            mapped.add(meid)
            posted += 1
        return posted

    def pull_proposals(self):
        """One non-blocking master /sync of the proposals room; forward new ones.

        Reads the MASTER as the teammate's own account (outbound-only preserved).
        The proposal watermark (proposal_sync_since) advances only after the batch
        is forwarded; a master transport failure raises MasterUnreachable and the
        watermark is left untouched (buffer + retry, like the mirror-up path)."""
        mpr = self.meta_get("master_proposals_room")
        lpr = self.meta_get("local_proposals_room")
        if not mpr or not lpr:
            return
        since = self.meta_get("proposal_sync_since")
        flt = json.dumps({
            "room": {"rooms": [mpr], "timeline": {"limit": 100},
                     "state": {"types": []}, "ephemeral": {"types": []},
                     "account_data": {"types": []}},
            "presence": {"types": []}, "account_data": {"types": []},
        })
        # Long-poll (not timeout=0): a manager proposal arriving mid-poll returns
        # immediately, so down-direction latency is ~the LOCAL tail's poll, not
        # a full loop period. Same duration as the local tail (see module doc).
        query = {"timeout": str(self.cfg.sync_timeout), "filter": flt}
        if since:
            query["since"] = since
        data = self.master("GET", "/_matrix/client/v3/sync", query=query,
                           timeout=(self.cfg.sync_timeout // 1000) + 30)
        room = (((data.get("rooms") or {}).get("join")) or {}).get(mpr) or {}
        events = ((room.get("timeline") or {}).get("events")) or []
        posted = self.forward_proposals(mpr, lpr, events)
        # Advance the watermark only after forward_proposals returned (no local
        # write raised); proposal_map still guards against any replayed overlap.
        self.meta_set("proposal_sync_since", data.get("next_batch") or since or "")
        if posted:
            log.info("proposals: pulled %d new -> local room %s", posted, lpr)

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

    MASTER_LINK_TYPE = "com.jkali.master_link"  # local hub account-data (set by the user app)

    def refresh_master_config(self):
        """Resolve the effective MASTER_* config. The local hub account-data
        com.jkali.master_link (written by the user app's Settings > Connect to
        organization, after it redeems an enrollment code) overrides the env
        fallback. Read outbound-only from the LOCAL hub with LOCAL_TOKEN.
        Returns True when a complete master config is available (connected)."""
        link = None
        try:
            data = self.local("GET", "/_matrix/client/v3/user/%s/account_data/%s"
                              % (urllib.parse.quote(self.cfg.local_user), self.MASTER_LINK_TYPE))
            if isinstance(data, dict) and data.get("master_token") and data.get("master_hs_url"):
                link = data
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log.debug("master_link read: HTTP %s", e.code)
        except Exception as e:
            log.debug("master_link read failed: %s", e)
        src = link or {}
        em = self.cfg.env_master
        self.cfg.master_hs    = (src.get("master_hs_url") or em["master_hs"] or "").rstrip("/")
        self.cfg.master_user  = src.get("master_user")  or em["master_user"]  or ""
        self.cfg.master_token = src.get("master_token") or em["master_token"] or ""
        self.cfg.manager_mxid = src.get("manager_mxid") or em["manager_mxid"] or ""
        self.cfg.master_space = src.get("master_space") or em["master_space"] or ""
        return bool(self.cfg.master_hs and self.cfg.master_user
                    and self.cfg.master_token and self.cfg.master_space)

    def run(self):
        log.info("uplink starting: local=%s (master resolved per-loop from env or "
                 "com.jkali.master_link account-data)", self.cfg.local_hs)
        while True:
            try:
                if not self.refresh_master_config():
                    if getattr(self, "_conn_state", None) is not False:
                        self._conn_state = False
                        log.info("not connected to a master — connect from the user app: "
                                 "Settings > Connect to organization")
                    time.sleep(30)
                    continue
                if getattr(self, "_conn_state", None) is not True:
                    self._conn_state = True
                    log.info("connected to master %s (space %s)",
                             self.cfg.master_user, self.cfg.master_space)
                # Reconcile (full initial /sync + consent resolution) is the
                # expensive pass — keep it on its own cadence while the two
                # cheap long-polls below spin the loop every few seconds.
                now = time.time()
                if now - self._last_reconcile >= self.cfg.reconcile_ms / 1000.0:
                    self.reconcile()
                    self._last_reconcile = now
                self.tail_once()
                # V2 proposal channel: ensure the dedicated proposals rooms exist,
                # then pull any new manager proposals DOWN into the local one. Both
                # steps are outbound-only and write com.jkali.proposal exclusively.
                self.ensure_proposal_rooms()
                self.pull_proposals()
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
