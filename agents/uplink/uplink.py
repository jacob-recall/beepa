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
import hashlib
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

# The durable address-book store (agents/contacts/contacts_store.py, Task 1) is
# the SOURCE for the contact mirror. It is a sibling package; add its dir to the
# path and import the module directly (stdlib-only, no side effects at import).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "contacts"))
import contacts_store     # noqa: E402

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

# §12 phase 5 contact mirror (Task 6). A DEDICATED per-teammate contacts room on
# the master carries one com.jkali.contact STATE event per SHARED address-book
# handle (keyed by sha1(source|network_id)); the room is marked com.jkali.contacts
# and power-leveled so the manager can only READ. The uplink pushes only handles
# whose SOURCE resolves to shared under com.jkali.contact_share_policy — a
# not-shared handle never leaves the machine.
CONTACT_STATE_TYPE = "com.jkali.contact"    # per-handle state event on the contacts room
CONTACTS_MARKER = "com.jkali.contacts"      # state marker on the contacts room

# Byte-parity with shared/matrix/client.js MXC_RE (server / media-id charset).
MXC_RE = re.compile(r"^mxc://([A-Za-z0-9.\-:]+)/([A-Za-z0-9_-]+)$")
# Generic Matrix room-id shape (any server_name) — the proposal's target_room is
# a teammate-LOCAL room id, so it is validated by shape, not by a server-pinned
# regex. The uplink never SENDS to target_room; it only records it inside the
# forwarded com.jkali.proposal event for the teammate's guarded send path to
# re-validate later.
ROOMID_RE = re.compile(r"^![^:]+:[A-Za-z0-9.\-:]+$")
# Person-targeted proposal handle gates. Byte-parity with the master app's
# buildIdentifierProposalContent (apps/master/main.js E164_RE/EMAIL_RE) and the
# teammate inbox's parseProposal (apps/user/proposals.js). A person-targeted
# proposal carries a handle instead of a target_room; the uplink only whitelists
# it as inert data — the teammate's guarded start-chat path re-validates it
# authoritatively before anything is sent.
PROPOSAL_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
PROPOSAL_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# Short lowercase source id (e.g. "imessage", "wa"); tight gate so a hostile
# master cannot smuggle an arbitrary string into the carried-down target_source.
PROPOSAL_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# Mxid shape gate for self_identities entries. shared/ui/account-data.js pins
# its MXID_RE to ':localhost'; here the same-server constraint is enforced in
# read_self_mxids() against cfg.local_user's own server name instead of a
# hardcoded literal, so the daemon stays deployable off-localhost.
MXID_RE = re.compile(r"^@[^:]+:[A-Za-z0-9.\-:]+$")
MEDIA_MSGTYPES = ("m.image", "m.video", "m.audio", "m.file")
MEDIA_LABELS = {"m.image": "Photo", "m.video": "Video", "m.audio": "Audio", "m.file": "File"}
DEFAULT_MEDIA_MAX = 25 * 1024 * 1024        # 25 MB re-upload cap (§8.2, v1.5)

log = logging.getLogger("uplink")


def sanitize_proposal_content(content, sender, event_id, origin_ts):
    """Whitelist a master com.jkali.proposal content dict into the local shape.

    Pure (no I/O, no self) so it is unit-testable. Accepts BOTH proposal shapes
    and returns None (fail-closed) for anything malformed, so the caller records
    it as handled and never retries:

    - ROOM proposal: `target_room` is a valid room id (SHAPE only — it is a
      teammate-LOCAL room id the uplink never sends to; the teammate's guarded
      send path re-validates it against the live joined set). Carries down
      target_room/body/created_by/origin_ts/proposal_source_event/template.
    - PERSON-TARGETED proposal: no target_room, but a `target_identifier` that
      is a valid E.164 handle OR strict email, a `target_source` that is a short
      lowercase source id, and a non-empty body. The handle is inert data here —
      re-validated authoritatively at the teammate before any start-chat. Carries
      down target_source/target_identifier/target_display?/body/created_by/
      origin_ts/proposal_source_event/template. NO target_room.

    target_room takes precedence: if it is present and valid, the room shape wins
    and no identifier keys leak into the output. body must be a non-empty string
    for either shape."""
    c = content if isinstance(content, dict) else {}
    body = c.get("body")
    if not isinstance(body, str) or not body.strip():
        return None

    created_by = (c.get("created_by") if isinstance(c.get("created_by"), str)
                  else (sender or ""))
    ots = c.get("origin_ts") if isinstance(c.get("origin_ts"), int) else origin_ts

    target = c.get("target_room")
    if isinstance(target, str) and ROOMID_RE.match(target):
        # ---- room proposal (unchanged behavior) ----
        out = {
            "target_room": target,
            "body": body,
            "created_by": created_by,
            "origin_ts": ots,
            # Provenance back to the master event (audit; also the dedup txn seed).
            "com.jkali.proposal_source_event": event_id,
        }
        if c.get("template") is True:
            out["template"] = True
        return out

    # ---- person-targeted proposal ----
    identifier = c.get("target_identifier")
    source = c.get("target_source")
    if (isinstance(identifier, str)
            and (PROPOSAL_E164_RE.match(identifier) or PROPOSAL_EMAIL_RE.match(identifier))
            and isinstance(source, str) and PROPOSAL_SOURCE_RE.match(source)):
        out = {
            "target_source": source,
            "target_identifier": identifier,
            "body": body,
            "created_by": created_by,
            "origin_ts": ots,
            "com.jkali.proposal_source_event": event_id,
        }
        # target_display is cosmetic: carry only if a string, clamped.
        display = c.get("target_display")
        if isinstance(display, str) and display:
            out["target_display"] = display[:128]
        if c.get("template") is True:
            out["template"] = True
        return out

    # ---- neither shape valid: fail closed ----
    return None


class MasterUnreachable(Exception):
    """Raised when the MASTER homeserver cannot be reached (buffer + backoff)."""


# --------------------------------------------------------------- handle owner
# Byte-parity Python port of the handle-grouping half of
# shared/model/contacts.js (normalizeProfiles + handleOwner). It exists so the
# uplink derives person_id from the AUTHORITATIVE account-data grouping exactly
# as the JS does, and the master therefore groups a handle under the same person.
# Composite key is `source + '|' + network_id`; a handle belongs to AT MOST ONE
# profile and the FIRST profile that claims it wins (re-normalized here so a
# malformed stored profile can never smuggle a handle into two profiles).
def _normalize_profiles_handles(profiles):
    """The handle-relevant subset of normalizeProfiles(): dedup profile ids
    (first wins), coerce displayName to a string, and claim each well-formed
    handle for the first profile that lists it. Returns an ordered list of
    {'id', 'displayName', 'handleIds': [(source, network_id), ...]}. roomIds do
    not affect handle ownership and are intentionally not processed here."""
    raw = profiles if isinstance(profiles, list) else []
    seen_ids = set()
    claimed = set()
    out = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if not isinstance(pid, str) or not pid or pid in seen_ids:
            continue
        seen_ids.add(pid)
        dn = p.get("displayName")
        dn = dn if isinstance(dn, str) else ""
        handles = []
        raw_handles = p.get("handleIds")
        if isinstance(raw_handles, list):
            for h in raw_handles:
                if not isinstance(h, dict):
                    continue
                source = h.get("source")
                source = source if isinstance(source, str) else ""
                network_id = h.get("network_id")
                network_id = network_id if isinstance(network_id, str) else ""
                if not source or not network_id:
                    continue
                key = source + "|" + network_id
                if key in claimed:
                    continue
                claimed.add(key)
                handles.append((source, network_id))
        out.append({"id": pid, "displayName": dn, "handleIds": handles})
    return out


def handle_owner(profiles, source, network_id):
    """The id of the profile that owns (source, network_id), or None.

    Byte-parity with handleOwner() in shared/model/contacts.js: same composite
    key, same first-profile-wins rule, same re-normalization. Given the raw
    `profiles` array from com.jkali.contact_profiles."""
    for p in _normalize_profiles_handles(profiles):
        for (src, nid) in p["handleIds"]:
            if src == source and nid == network_id:
                return p["id"]
    return None


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
        # The address-book store the contact mirror reads (Task 1). Defaults to
        # the importer's sibling store; the integration harness repoints it.
        self.contacts_db = env.get("UPLINK_CONTACTS_DB") or os.path.normpath(
            os.path.join(BASE, "..", "contacts", "contacts.db"))
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
        # §12 phase 5 contact mirror: the per-handle up-direction record — which
        # store version was last mirrored for a handle and under which master
        # state_key. This IS the mirror's memory: each pass diffs the desired
        # shared-and-live set against it (reconcile.plan_contact_mirror); rows
        # are written/deleted only after the master's 2xx. (com.jkali.contact
        # state events are idempotent by state_key, so a replay overwrites
        # rather than duplicates.)
        db.execute(
            "CREATE TABLE IF NOT EXISTS contact_mirror ("
            "source TEXT, network_id TEXT, mirrored_version INTEGER, "
            "master_state_key TEXT, PRIMARY KEY(source, network_id))")
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

        Thin ev-shaped wrapper around the pure sanitize_proposal_content() so the
        validation logic stays unit-testable without an event envelope. Returns
        None for a malformed proposal (see the helper) so it is recorded as
        handled and never retried."""
        c = ev.get("content") if isinstance(ev.get("content"), dict) else {}
        return sanitize_proposal_content(
            c,
            sender=(ev.get("sender") or ""),
            event_id=ev.get("event_id"),
            origin_ts=ev.get("origin_server_ts"),
        )

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

    # -- contact mirror (§12 phase 5, Task 6, LOCAL contacts.db -> MASTER) ---
    # Address-book contacts (PII) leave the machine ONLY when consent says so.
    # HARD LIMITS enforced here:
    #   - each pass is a DIFF of the desired set (rows that are live AND whose
    #     SOURCE resolves shared under consent.resolve_contact_share over
    #     com.jkali.contact_share_policy) against contact_mirror (what the master
    #     already holds), planned by the pure reconcile.plan_contact_mirror. A
    #     handle that resolves NOT shared is in neither leg of the plan and never
    #     reaches a network call; enabling a source therefore BACKFILLS its
    #     already-imported contacts, and disabling it tombstones them;
    #   - tombstones are applied BEFORE pushes: revocation is never queued
    #     behind a backfill;
    #   - a contact_mirror row is written (push) or deleted (tombstone) ONLY
    #     after the master PUT returns 2xx (self.master raises MasterUnreachable
    #     / HTTPError otherwise, before any local write) — the next pass simply
    #     re-plans the same handle: exactly-once, no loss on outage, no
    #     duplicate (the state PUT keys on sha1(source|network_id));
    #   - the contacts room is created ONCE, power-leveled so the manager can only
    #     READ (state_default 100, manager at 0 -> no com.jkali.contact write);
    #   - never log a contact value (network_id / display_name) — counts only.
    def read_contact_profiles(self):
        """The raw profiles list from com.jkali.contact_profiles.

        handle_owner() re-normalizes it, so this stays a thin read.
        Absent (404) -> [] (no groupings, relink normally).
        Any OTHER error -> None: the caller must NOT treat that as "no
        groupings" — doing so would unlink every handle and version-bump the
        whole address book on a transient local-homeserver blip, then re-link
        and bump it all again once the read recovers (a double full re-push)."""
        path = ("/_matrix/client/v3/user/" + urllib.parse.quote(self.cfg.local_user, safe="")
                + "/account_data/" + CONTACT_PROFILES_TYPE)
        try:
            data = self.local("GET", path)
        except urllib.error.HTTPError as e:
            return [] if e.code == 404 else None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            return None
        profiles = data.get("profiles") if isinstance(data, dict) else None
        return profiles if isinstance(profiles, list) else []

    def read_contact_policy(self):
        """Normalized contact-share policy from com.jkali.contact_share_policy.

        Absent/error -> the safe default (global 'private', no sources), matching
        the conversation policy read's fail-safe: nothing shared unless a level
        explicitly says so."""
        path = ("/_matrix/client/v3/user/" + urllib.parse.quote(self.cfg.local_user, safe="")
                + "/account_data/" + consent.CONTACT_SHARE_POLICY_TYPE)
        try:
            return consent.normalize_contact_policy(self.local("GET", path))
        except urllib.error.HTTPError:
            return {"global": "private", "sources": {}}

    def ensure_contacts_room(self):
        """Idempotently ensure the per-teammate master contacts room; cache its id.

        Created as the teammate's own scoped account, linked under the teammate's
        master space, marked com.jkali.contacts, and power-leveled so the manager
        can only READ: state_default 100 with the manager pinned to 0 means the
        manager cannot write a com.jkali.contact state event (there is no
        per-event lower power for it), and events_default 50 keeps the manager
        from posting timeline events too. Mirrors the read-only PL-pinning of
        create_mirror() / ensure_proposal_rooms() (§8.3)."""
        cfg = self.cfg
        mcr = self.meta_get("master_contacts_room")
        if mcr:
            return mcr
        body = {
            "name": "Contacts",
            "topic": "Shared address-book contacts for this teammate. Read-only — "
                     "only contacts the teammate has consented to share appear here.",
            "preset": "private_chat",
            "invite": [cfg.manager_mxid],
            "initial_state": [
                {"type": CONTACTS_MARKER, "state_key": "", "content": {}},
                {"type": "m.space.parent", "state_key": cfg.master_space,
                 "content": {"via": [self._server_name(cfg.master_user)], "canonical": True}},
            ],
            # Read-only enforcement: owner 100, manager 0, state_default 100 =>
            # the manager cannot write com.jkali.contact (or any state); no entry
            # in `events` lowers com.jkali.contact's required power below that.
            "power_level_content_override": {
                "users": {cfg.master_user: 100, cfg.manager_mxid: 0},
                "events_default": 50,
                "state_default": 100,
                "invite": 100, "kick": 100, "ban": 100, "redact": 100,
            },
        }
        res = self.master("POST", "/_matrix/client/v3/createRoom", body)
        mcr = res["room_id"]
        self.master("PUT", "/_matrix/client/v3/rooms/"
                    + urllib.parse.quote(cfg.master_space, safe="")
                    + "/state/m.space.child/" + urllib.parse.quote(mcr, safe=""),
                    {"via": [self._server_name(cfg.master_user)]})
        self.meta_set("master_contacts_room", mcr)
        # A fresh master room means nothing is mirrored yet: clear the per-handle
        # mirror record so the next pass's diff re-pushes everything shared into
        # the new room. (contact_cursor is a legacy meta key from the old
        # forward-only mirror; deleting it is intentional cleanup, nothing reads it.)
        self.db.execute("DELETE FROM meta WHERE k='contact_cursor'")
        self.db.execute("DELETE FROM contact_mirror")
        self.db.commit()
        log.info("created master contacts room %s under space %s", mcr, cfg.master_space)
        return mcr

    def _put_contact(self, room, row, id_to_dn):
        """Upsert one com.jkali.contact STATE event; return its state_key.

        state_key = sha1(source + '|' + network_id): re-PUTting the same handle
        overwrites its own state event, so a replay is idempotent. A soft-deleted
        (deleted=1) row pushes a tombstone ({deleted: true}) so the master drops
        it; otherwise the grouping fields ride along (person_id/person_display are
        null when the handle is unlinked). Raises MasterUnreachable (via
        self.master) on a master outage WITHOUT recording anything, so the caller
        leaves the cursor unadvanced and retries next pass."""
        source = row["source"]
        network_id = row["network_id"]
        state_key = hashlib.sha1((source + "|" + network_id).encode("utf-8")).hexdigest()
        if row["deleted"]:
            content = {"deleted": True}
        else:
            pid = row.get("person_id")
            content = {
                "source": source,
                "network_id": network_id,
                "kind": row.get("kind"),
                "display_name": row.get("display_name"),
                "person_id": pid,
                "person_display": id_to_dn.get(pid) if pid else None,
                "deleted": False,
            }
        self.master("PUT", "/_matrix/client/v3/rooms/"
                    + urllib.parse.quote(room, safe="")
                    + "/state/" + CONTACT_STATE_TYPE + "/"
                    + urllib.parse.quote(state_key, safe=""), content)
        return state_key

    def mirror_contacts(self):
        """Mirror shared address-book contacts up to the master as a per-pass
        DIFF of desired-shared-and-live vs mirrored (pm_mng-q5u.2).

        Called each reconcile pass AFTER the conversation reconcile.
          (a) Recompute contacts.db's derived person_id cache from the
              authoritative account-data grouping (com.jkali.contact_profiles via
              handle_owner). A re-link becomes a version bump that then flows to
              the master as a normal update; set_person_id only bumps when the
              link actually changed. If the profiles read fails for any reason
              other than "absent", this step AND the push leg are skipped this
              pass (a push without profiles would stamp person_display null and
              never be corrected); tombstones still run.
          (b) Plan: reconcile.plan_contact_mirror over every row of every known
              source (SOURCE_ID_TO_LABEL) against the COMPLETE contact_mirror
              table. A row whose source resolves NOT shared is in neither leg
              and never leaves the machine. A mirrored handle that is no longer
              live-and-shared (source flipped private, row soft-deleted, source
              renamed/removed) is tombstoned. A live-and-shared row that is not
              mirrored, or is mirrored at a DIFFERENT version, is pushed — so
              enabling a source backfills its already-imported contacts, and a
              rebuilt contacts.db (versions restart at 1) re-syncs rather than
              leaving stale PII on the master. Pushes are capped per pass
              (reconcile.PUSH_CAP); the remainder is re-planned next pass.
          (c) Apply tombstones FIRST (revocation never waits behind a backfill),
              then pushes. Each contact_mirror write happens only after the
              master's 2xx; MasterUnreachable / HTTPError propagate before it,
              so the next pass re-plans the same handle — no loss, no
              duplicate (state_key = sha1(source|network_id))."""
        room = self.ensure_contacts_room()
        if not room:
            return
        if not os.path.exists(self.cfg.contacts_db):
            return  # the importer has not produced a store yet -> nothing to mirror
        profiles = self.read_contact_profiles()
        profiles_ok = profiles is not None
        if not profiles_ok:
            log.warning("contacts: profiles read failed (not 404) — skipping relink "
                        "and pushes this pass; tombstones still apply")
            profiles = []
        id_to_dn = {p["id"]: p["displayName"] for p in _normalize_profiles_handles(profiles)}
        policy = self.read_contact_policy()
        conn = contacts_store.open_store(self.cfg.contacts_db)
        try:
            # (a) Recompute the derived person_id cache for every known-source row.
            # Only touch the store when the link actually changed (keeps the
            # two-writer contacts.db writes short).
            relinked = 0
            if profiles_ok:
                for source in SOURCE_ID_TO_LABEL:
                    for row in contacts_store.shared_since(conn, source, 0):
                        owner = handle_owner(profiles, source, row["network_id"])
                        if row["person_id"] != owner:
                            if contacts_store.set_person_id(conn, source, row["network_id"], owner):
                                relinked += 1
            # (b) Plan the diff over fresh rows (relink may have bumped versions)
            # and the complete mirror table.
            rows = []
            for source in SOURCE_ID_TO_LABEL:
                rows.extend(contacts_store.shared_since(conn, source, 0))
            mirrored = {(r[0], r[1]): r[2] for r in self.db.execute(
                "SELECT source, network_id, mirrored_version FROM contact_mirror").fetchall()}
            plan = reconcile.plan_contact_mirror(rows, mirrored, policy, SOURCE_ID_TO_LABEL.keys())
            # (c) Tombstones first. _put_contact's deleted branch raises before
            # returning on an outage, so the mirror row is dropped ONLY after
            # the master's 2xx; a dropped row leaves `mirrored` and is never
            # re-tombstoned, and a later re-share simply re-pushes it.
            tombstoned = pushed = 0
            for (source, network_id) in plan["tombstone"]:
                self._put_contact(room, {"source": source, "network_id": network_id,
                                         "deleted": 1}, id_to_dn)
                self.db.execute(
                    "DELETE FROM contact_mirror WHERE source=? AND network_id=?",
                    (source, network_id))
                self.db.commit()
                tombstoned += 1
            pending = plan["pending"]
            if profiles_ok:
                for row in plan["push"]:
                    sk = self._put_contact(room, row, id_to_dn)
                    self.db.execute(
                        "INSERT OR REPLACE INTO contact_mirror "
                        "(source, network_id, mirrored_version, master_state_key) "
                        "VALUES (?,?,?,?)",
                        (row["source"], row["network_id"], row["version"], sk))
                    self.db.commit()
                    pushed += 1
            else:
                pending += len(plan["push"])
            if relinked or pushed or tombstoned or plan["not_shared"] or pending:
                log.info("contacts: relinked=%d pushed=%d tombstoned=%d not_shared=%d "
                         "pending=%d", relinked, pushed, tombstoned, plan["not_shared"],
                         pending)
        finally:
            conn.close()

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
                    # §12 phase 5: mirror shared address-book contacts up AFTER
                    # the conversation reconcile. Consent-gated + exactly-once;
                    # a master outage raises MasterUnreachable and is buffered
                    # by the handler below (cursor left unadvanced).
                    self.mirror_contacts()
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
            except sqlite3.OperationalError:
                # Transient contacts.db lock (e.g. the hourly importer holds a
                # RESERVED write while mirror_contacts tries to write) that the
                # busy_timeout could not clear. This is recoverable — the next
                # reconcile pass retries. Log count-only (never the exception
                # text, which could name a handle) and continue; the
                # exactly-once cursor logic is untouched because a failed
                # contacts write never advanced it.
                log.warning("transient contacts.db lock; retrying next pass")
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
