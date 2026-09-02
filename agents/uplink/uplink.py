#!/usr/bin/env python3
"""Uplink daemon — mirror shared local conversations up to the MASTER homeserver.

PLAN-MASTER-SYNC.md §5.4 / §7 / §8. A headless, OUTBOUND-ONLY Matrix client. It
authenticates to the teammate's LOCAL homeserver (read) and to the MASTER
homeserver (write, as the teammate's own scoped account). It never listens on a
socket and never holds a bridge session or external send-capability.

It sends into a conversation in exactly ONE case (direct-share-level plan, D2):
a conversation the teammate has explicitly set to the 'direct' level auto-sends
manager proposals, with no review click, through _auto_send(). That path is
gated by the eleven checks in _direct_send_gate()/_auto_send() — server-stamped
manager sender, send-grade sanitization, freshness, mirrored-target membership,
a FRESH consent point-read at send time, a persisted per-room hourly cap,
intent-recorded-before-dispatch, exactly one non-actionable inbox record either
way, a pre/post-dispatch failure split, a durable audit table, and
master-identity binding that suspends auto-send on any rebinding until the
teammate re-acks. Any gate failing, and every other level, still lands in the
teammate's proposal inbox as an ordinary draft they send themselves.

Security invariants enforced here:
  - Outbound only: two urllib clients, no server. Master is written strictly as
    the teammate's scoped account (MASTER_TOKEN); the manager is invited at PL 0
    with events_default 50 so the manager can read but never send (§8.3).
  - One-way: events flow LOCAL -> MASTER only. Nothing is read from MASTER and
    applied to LOCAL.
  - Consent boundary: only rooms whose effective state resolves to shared
    (agents/uplink/consent.py, a byte-parity port of shared/model/consent.js)
    are mirrored. Sharing is EXPLICIT-ONLY: a per-conversation level of
    'share' or 'direct' mirrors, and absent-or-unrecognized is private —
    nothing is inherited from a contact profile or a standing policy any more.
    Flip-to-not-shared DELETES the master mirror (revocation). The one-time
    migration that materializes previously-inherited shares as explicit
    overrides is migrate_explicit_levels() (D0); it runs before the first
    reconcile can evaluate a deletion under the new rules.
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
  UPLINK_DIRECT_SEND_ROOM_HOURLY  D2.6 rolling per-room auto-send cap
                       (default: 20). Persisted in state.db, so it survives
                       KeepAlive restarts; 0 disables auto-send entirely.

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
# event. Proposals NEVER live in a mirror/conversation room. A proposal is an
# ordinary draft the teammate reviews and sends from their own guarded local
# path, EXCEPT for a conversation explicitly set to the 'direct' level, where
# the D2 auto-send below sends it and files a non-actionable record instead.
PROPOSAL_TYPE = "com.jkali.proposal"        # timeline event carrying a suggestion
PROPOSALS_MARKER = "com.jkali.proposals"    # state marker on a proposals room

# ---- D2 (direct-share-level plan): the auto-send exception ----------------
# Wire contract pinned by the plan's "Wire contract (S2/S3)" section — apps/user
# (S2) already renders exactly these fields, so none of them may be renamed or
# reshaped on one side alone.
AUTO_SENT_KEY = "com.jkali.auto_sent"          # true on the non-actionable record (D2.8)
SENT_EVENT_ID_KEY = "sent_event_id"            # the LOCAL event id the send produced
SEND_AMBIGUOUS_KEY = "com.jkali.send_ambiguous"  # post-dispatch, outcome unknown (D2.9)
# COSMETIC ONLY (F14): stamped on the auto-sent m.room.message so apps/user can
# badge the bubble and the mirror carries it up. It is forgeable by anything
# holding the teammate token and MUST NEVER feed the from_me gate or any other
# trust decision — nothing in this daemon or the apps reads it back.
AUTO_SENT_FROM_PROPOSAL_KEY = "com.jkali.auto_sent_from_proposal"
# D2.11 master-identity binding. Both are LOCAL user account-data on the
# teammate's own homeserver (never proposal-room events); apps/user's
# suspensionAffordance()/ackDirectSendSuspension() are the other half.
DIRECT_SEND_SUSPENDED_TYPE = "com.jkali.direct_send_suspended"
DIRECT_SEND_ACK_TYPE = "com.jkali.direct_send_ack"
# D2b: per-mirror share level, read by apps/master to label its draft
# affordance ("Send" only for 'direct'). Cosmetic on the master side — the
# authorization is the teammate-side consent point-read in D2.5.
SHARE_LEVEL_TYPE = "com.jkali.share_level"
DIRECT_SEND_BODY_MAX = 8000                 # D2.2 send-grade clamp (= sendConvoMessage's)
DIRECT_SEND_FRESH_MS = 10 * 60 * 1000       # D2.3 replay bound: 10 minutes
DIRECT_SEND_ROOM_HOURLY = 20                # D2.6 default per-room rolling cap
DIRECT_SEND_WINDOW_S = 3600                 # D2.6 rolling window
# D2.2 send-grade sanitization: the SAME character classes as
# shared/ui/el.js sanitize() — C0 controls except \n (so \t IS stripped),
# DEL, bidi overrides/isolates, zero-width + directional marks, BOM. Kept as
# one literal class so a reader can diff it against the JS by eye.
SEND_STRIP_RE = re.compile(
    "[\x00-\x09\x0b-\x1f\x7f‪-‮⁦-⁩​-‏﻿]")

# §12 phase 5 contact mirror (Task 6). A DEDICATED per-teammate contacts room on
# the master carries one com.jkali.contact STATE event per SHARED address-book
# handle (keyed by sha1(source|network_id)); the room is marked com.jkali.contacts
# and power-leveled so the manager can only READ. The uplink pushes only handles
# whose SOURCE resolves to shared under com.jkali.contact_share_policy — a
# not-shared handle never leaves the machine.
CONTACT_STATE_TYPE = "com.jkali.contact"    # per-handle state event on the contacts room
CONTACTS_MARKER = "com.jkali.contacts"      # state marker on the contacts room

# D0 (direct-share-level plan): the one-time migration to EXPLICIT per-conversation
# levels. consent.py no longer inherits from profile/per-source/global policy, so
# every conversation that was mirrored purely because of a standing policy must be
# materialized as an explicit 'share' override BEFORE the new resolver is allowed
# to evaluate deletions — otherwise the first pass after the upgrade would revoke
# every standing-policy share. The flag lives in state.db `meta`; the marker tells
# the teammate UI the daemon has stopped honoring standing policies (F7).
MIGRATED_FLAG = "migrated_explicit_levels"  # meta key, "1" once the pass completed

# state.db schema version (sqlite PRAGMA user_version). Bump it and add a block
# to Uplink._migrate_db() for any change to an EXISTING table's shape — the
# CREATE-IF-NOT-EXISTS init cannot evolve a teammate's already-created db.
#   0 = pre-S3 (mirror_rooms/event_map/proposal_map/contact_mirror/meta)
#   1 = D2/D2b: proposal_map.outcome, mirror_rooms.stamped_level,
#       direct_send_log, direct_send_audit
SCHEMA_VERSION = 1
# One-time re-PUT of the two proposal-room topics after the D2 copy change
# (the old strings asserted an absolute that auto-send breaks, and a topic is
# only written at room creation).
TOPIC_COPY_FLAG = "proposal_topics_direct_copy"
# The room topics BOTH proposals rooms carry. Before D2 each asserted an
# absolute — that no proposal is ever dispatched without the teammate's click —
# which the direct level breaks, so they now state the exception instead. A
# topic is only written at room creation, hence the one-time re-PUT above for
# installs whose rooms already exist.
MASTER_PROPOSALS_TOPIC = (
    "Manager suggestions for this teammate. The teammate reviews each one and "
    "sends it themselves — EXCEPT in conversations they have set to Direct, "
    "where their own uplink sends it into the conversation without review.")
LOCAL_PROPOSALS_TOPIC = (
    "Suggested messages from the manager. Review each one and send it yourself "
    "— EXCEPT in conversations you have set to Direct, where your uplink "
    "already sent it and files the record here after the fact.")

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


def legacy_effective_shared(convo, policy, override, profile=None):
    """The PRE-D1 (inherit-semantics) share decision — kept for D0 ONLY.

    This is a frozen copy of the boolean the old four-level resolver returned:
      1. per-conversation override, EXACT 'share' / 'private' only
      2. contact-profile share-state 'share' / 'private'
      3. per-source policy 'share-all' / 'private-all'
      4. global standing policy 'share-all'
      5. safe default: not shared
    It exists so migrate_explicit_levels() can ask "was this room shared under
    the OLD rules?" and materialize that answer as an explicit override. It is
    NEVER consulted for a live mirroring decision — consent.effective_shared()
    is the only authorization path — and nothing new may call it.

    Note the F8 compat fact this pins: the old code recognized only the exact
    strings 'share'/'private', so a stored 'direct' override behaves here as
    INHERIT (falls through to profile/source/global), not as private. That is
    what a partial rollback to old code would do, and tests/unit/
    uplink_migration.test.py pins it.
    """
    if override == "share":
        return True
    if override == "private":
        return False

    prof = profile if isinstance(profile, dict) else None
    if prof:
        if prof.get("share") == "share":
            return True
        if prof.get("share") == "private":
            return False

    pol = policy if isinstance(policy, dict) else {}
    c = convo if isinstance(convo, dict) else {}
    sid = c.get("sourceId")
    sid = sid if isinstance(sid, str) and sid else None
    # Same gated per-source lookup the old resolver used (valid key, own entry,
    # exact value) — reuse consent's gate rather than re-deriving it here.
    src = consent._source_rule(pol.get("sources"), sid)
    if src == "share-all":
        return True
    if src == "private-all":
        return False

    return pol.get("global") == "share-all"


def sanitize_proposal_content(content, sender, event_id, origin_ts):
    """Whitelist a master com.jkali.proposal content dict into the local shape.

    Pure (no I/O, no self) so it is unit-testable. Accepts BOTH proposal shapes
    and returns None (fail-closed) for anything malformed, so the caller records
    it as handled and never retries:

    - ROOM proposal: `target_room` is a valid room id (SHAPE only — a
      teammate-LOCAL room id, re-validated against the live joined set by
      whoever eventually sends: the teammate's guarded send path, or — for a
      'direct' conversation — D2's positive mirror-set membership check, which
      is the only thing here that lets this daemon send to it). Carries down
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

    # D2-1 (F16): created_by is COSMETIC and is pinned to the SERVER-STAMPED
    # sender, never taken from content. A manager-authored proposal is
    # indistinguishable from one whose content claimed a different author, so a
    # self-declared created_by must not survive into the teammate's inbox where
    # it would read as provenance. Nothing anywhere makes a trust decision on
    # this field — the auto-send gate compares ev["sender"] itself (D2.1).
    created_by = sender or ""
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


def sanitize_send_body(body):
    """D2-2: send-grade sanitization of a proposal body, or None to refuse.

    Pure (no I/O, no self) so it is unit-testable. This runs IN ADDITION to
    sanitize_proposal_content()'s field whitelist, and only on the auto-send
    path — text that is about to leave the machine as a real message on the
    teammate's own account, where the master is the untrusted author:

      - strips the SAME character class as shared/ui/el.js sanitize()
        (SEND_STRIP_RE: C0 controls except newline, DEL, bidi overrides and
        isolates, zero-width/directional marks, BOM) so a proposal cannot
        smuggle invisible or direction-flipping text past the teammate;
      - clamps to DIRECT_SEND_BODY_MAX (8000), the same clamp the teammate's
        own guarded send path applies;
      - REFUSES (returns None) any body whose first non-whitespace character
        is '!' — bridge-command injection defense. mautrix bridges take
        '!wa …'-style commands from the user's own account in some scopes;
        rather than depend on how that resolves, no auto-sent message may
        ever begin with the command sigil. A refusal is not a silent drop:
        the caller falls back to the ordinary actionable draft, so the
        teammate still sees the proposal and can send it deliberately.

    Refuses an empty/blank result too (nothing to send after stripping).
    """
    if not isinstance(body, str):
        return None
    clean = SEND_STRIP_RE.sub("", body)[:DIRECT_SEND_BODY_MAX]
    stripped = clean.lstrip()
    if not stripped.strip():
        return None
    if stripped.startswith("!"):
        return None
    return clean


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
        # D2.6: rolling per-room hourly auto-send cap. Clamped at 0 (never
        # negative), so a hostile/typo'd value can only make auto-send rarer,
        # never unbounded; 0 disables auto-send entirely.
        self.direct_send_cap = max(0, int(
            env.get("UPLINK_DIRECT_SEND_ROOM_HOURLY", str(DIRECT_SEND_ROOM_HOURLY))))


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
        # D2-11: auto-send is suspended until refresh_direct_send_binding()
        # (called at the top of ensure_proposal_rooms) says otherwise.
        self._direct_suspended = True

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
        Uplink._migrate_db(db)
        try:
            os.chmod(path, 0o600)  # secrets/state 600 regardless of umask
        except OSError:
            pass
        return db

    @staticmethod
    def _migrate_db(db):
        """Versioned schema migration (sqlite user_version), idempotent.

        The CREATE-IF-NOT-EXISTS block above only ever ADDS tables, so it
        cannot evolve a table that already exists on a teammate's machine.
        Everything that changes an existing shape lives here, guarded by
        `PRAGMA user_version`, and must be safe to run against BOTH a brand-new
        db (just created above) and a pre-existing pre-S3 one.

        v1 (D2/D2b, direct-share-level plan):
          - proposal_map.outcome  — the per-proposal outcome state
            (NULL/'fallback' = ordinary actionable draft in the inbox,
            'attempted' = auto-send dispatched with the result still unknown,
            'sent' = auto-sent + non-actionable record filed, 'ambiguous' =
            post-dispatch failure + labelled record filed). This is what makes
            the intent-before-send ordering (D2.7) durable across a crash.
          - mirror_rooms.stamped_level — the last com.jkali.share_level stamped
            on a mirror, so D2b re-stamps on a change instead of every pass.
          - direct_send_log   — the persisted rolling rate-cap counter (D2.6).
          - direct_send_audit — the durable auto-send audit trail (D2.10).
        Both new tables are hash-only: a room id is stored as room_hash, never
        in the clear, and no message body is ever recorded.
        """
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        # Column adds are guarded individually: a db created by a LATER
        # schema-init that already has the column must not fail the ALTER.
        for table, column, decl in (("proposal_map", "outcome", "TEXT"),
                                    ("mirror_rooms", "stamped_level", "TEXT")):
            cols = {r[1] for r in db.execute("PRAGMA table_info(%s)" % table).fetchall()}
            if column not in cols:
                db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
        db.execute(
            "CREATE TABLE IF NOT EXISTS direct_send_log ("
            "ts INTEGER NOT NULL, room_hash TEXT NOT NULL)")
        db.execute(
            "CREATE INDEX IF NOT EXISTS direct_send_log_room "
            "ON direct_send_log (room_hash, ts)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS direct_send_audit ("
            "ts INTEGER NOT NULL, master_event_id TEXT, room_hash TEXT, outcome TEXT)")
        db.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)  # int constant, not input
        db.commit()

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

    # -- D0: one-time migration to explicit per-conversation levels ----------
    def write_share_override(self, local_room_id, state, migrated=False):
        """PUT one room's explicit share level into the teammate's OWN room
        account-data on the LOCAL homeserver (the same event the teammate UI
        writes). `migrated` stamps the content so apps/user can show the
        one-time "review migrated shares" list — converting a revocable
        standing policy into explicit overrides is surfaced, never silent.
        """
        if not isinstance(local_room_id, str) or not ROOMID_RE.match(local_room_id):
            raise ValueError("invalid room id")
        if state not in consent.OVERRIDE_STATES:
            raise ValueError("invalid share level")
        content = {"state": state}
        if migrated:
            content["migrated"] = True
        path = ("/_matrix/client/v3/user/" + urllib.parse.quote(self.cfg.local_user, safe="")
                + "/rooms/" + urllib.parse.quote(local_room_id, safe="")
                + "/account_data/" + consent.SHARE_OVERRIDE_TYPE)
        self.local("PUT", path, content)

    def write_consent_model_marker(self):
        """Record the consent MODEL VERSION in local user account-data (F7).

        Its presence is what tells apps/user that this daemon no longer honors
        the per-source / global standing policies, so the UI can stop offering
        controls it would not honor. Written only after the migration pass has
        materialized every inherited share.
        """
        path = ("/_matrix/client/v3/user/" + urllib.parse.quote(self.cfg.local_user, safe="")
                + "/account_data/" + consent.CONSENT_MODEL_TYPE)
        self.local("PUT", path, {"version": consent.CONSENT_MODEL_EXPLICIT})

    def migrate_explicit_levels(self):
        """Materialize standing-policy shares as explicit 'share' overrides.

        Runs at most once per state.db (guarded by the MIGRATED_FLAG meta key)
        and returns {room_id: 'share'} for what it wrote this pass ({} on every
        later pass). ORDERING IS A HARD REQUIREMENT: reconcile() calls this
        BEFORE it resolves anything under the new (explicit-only) resolver, so
        a room kept alive by a standing policy gets its explicit override
        before any deletion is evaluated. This pass itself performs no master
        calls at all and therefore cannot delete a mirror.

        A room is migrated iff ALL of:
          - it is CURRENTLY mirrored (a row in mirror_rooms), and
          - it is still joined and attributable to a known source, and
          - it has no explicit per-room override already (an existing override
            is the teammate's own decision and is never rewritten), and
          - the OLD inherit-semantics resolver says it was shared.

        A failed write aborts the pass WITHOUT setting the flag (the exception
        propagates out of reconcile before any delete_mirror can run), so the
        next pass retries; already-written overrides are simply skipped then.
        """
        if self.meta_get(MIGRATED_FLAG) == "1":
            return {}
        sync_data = self.full_sync()
        policy = self.read_policy()
        overrides = consent.overrides_from_sync(sync_data)
        source_of = self.sources_from_sync(sync_data)
        profile_of = self.read_profiles()
        join = (((sync_data or {}).get("rooms") or {}).get("join")) or {}
        written = {}
        for rid in sorted(self.existing_mirror_ids()):
            if rid in overrides:
                continue          # already an explicit level — never rewritten
            if rid not in join:
                continue          # no longer joined: nothing to preserve
            src = source_of.get(rid)
            if not src:
                continue          # unattributable: the old resolver skipped it too
            convo = {"id": rid, "sourceId": src,
                     "sourceLabel": SOURCE_ID_TO_LABEL.get(src, src)}
            prof = profile_of.get(rid)
            profile_arg = ({"displayName": prof["displayName"], "share": prof["share"]}
                           if prof else None)
            if not legacy_effective_shared(convo, policy, None, profile_arg):
                continue
            self.write_share_override(rid, "share", migrated=True)
            written[rid] = "share"
        self.write_consent_model_marker()
        self.meta_set(MIGRATED_FLAG, "1")   # flag LAST: the pass is idempotent
        log.info("consent model 2 (explicit levels): materialized %d inherited "
                 "share(s) as explicit overrides", len(written))
        return written

    def desired_shared(self, sync_data, extra_overrides=None):
        """Return (desired {room_id: level}, source_of, join, profile_of).

        Sharing is EXPLICIT-ONLY (D1): only the per-conversation override
        decides. D2b: the map carries the per-room LEVEL ('private'|'share'|
        'direct') rather than a bool, so reconcile() can stamp
        com.jkali.share_level on the mirror; the mirroring decision itself is
        unchanged and still goes through consent — level_is_shared(level) is
        exactly consent.effective_shared()'s answer for the same override.
        profile_of is still returned because create_mirror stamps a shared
        contact profile onto the mirror room for grouping on the master — it no
        longer affects the share decision.

        extra_overrides is the map D0's migration just wrote; merging it makes
        this pass independent of whether the /sync snapshot below already
        reflects those brand-new account-data events.
        """
        policy = self.read_policy()
        overrides = consent.overrides_from_sync(sync_data)
        if extra_overrides:
            overrides.update(extra_overrides)
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
            override = overrides.get(rid)
            # The authorization answer stays consent.effective_shared()'s; the
            # level is only carried alongside it for the D2b stamp. If the two
            # ever disagreed we keep the RESOLVER's answer and record private,
            # so a stamp can never widen what is mirrored.
            level = consent.effective_level(override)
            if not consent.effective_shared(convo, policy, override, profile_arg):
                level = "private"
            desired[rid] = level
        # Visibility (pm_mng-es1): a per-room share level the source detector
        # cannot attribute is a consent decision this daemon cannot honor — say
        # so instead of silently skipping. Only on change, so the loop does not
        # repeat itself every pass.
        sourceless = sorted(rid for rid, st in overrides.items()
                            if st in ("share", "direct") and rid in join
                            and not source_of.get(rid))
        if sourceless != self._last_sourceless:
            self._last_sourceless = sourceless
            if sourceless:
                log.warning("shared-but-sourceless (will NOT mirror): %s",
                            ", ".join(sourceless))
        return desired, source_of, join, profile_of

    def reconcile(self):
        # Refresh once per pass; _forward_message (backfill + tail) reads it.
        self.self_mxids = self.read_self_mxids()
        # D0 ordering invariant: the explicit-levels migration completes (and
        # sets its flag) BEFORE anything is resolved under the new resolver, so
        # no delete_mirror can ever run against a not-yet-materialized standing
        # share. No-op on every pass after the first.
        migrated = self.migrate_explicit_levels()
        sync_data = self.full_sync()
        desired, source_of, join, profile_of = self.desired_shared(sync_data, migrated)
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
                                   self.room_name_from_sync(join.get(rid)), stamp,
                                   desired.get(rid))
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
        # D2b: re-stamp com.jkali.share_level on KEPT mirrors whose level
        # flipped in either direction (share <-> direct), plus the one-time
        # backfill of mirrors created before this stamp existed. A pure diff
        # against the last stamped level in state.db, so a steady state writes
        # nothing. The stamp is cosmetic on the master (it only picks the
        # console's affordance label); the authorization for an auto-send is
        # the teammate-side point-read in D2.5, never this state event.
        for rid, level in reconcile.plan_level_restamp(
                desired, self.stamped_levels(), plan["keep"]):
            try:
                self.stamp_share_level(rid, level)
            except MasterUnreachable:
                raise
            except urllib.error.HTTPError as e:
                log.warning("share_level re-stamp %s failed: %s", rid, e)
        # Backfill/tail every kept + freshly-created room.
        for rid in sorted(set(plan["create"]) | set(plan["keep"])):
            self.sync_room(rid)

    # -- mirror lifecycle ---------------------------------------------------
    def stamped_levels(self):
        """{local_room_id: last-stamped com.jkali.share_level} from state.db."""
        return {r[0]: r[1] for r in self.db.execute(
            "SELECT local_room_id, stamped_level FROM mirror_rooms").fetchall()}

    def stamp_share_level(self, local_room_id, level):
        """PUT the D2b com.jkali.share_level state event on this room's mirror.

        Refuses anything but the two sharing levels (a 'private' room has no
        mirror to stamp) and records the new level in state.db only AFTER the
        master's 2xx — an outage raises MasterUnreachable first, so the next
        pass simply re-plans the same re-stamp.
        """
        if level not in reconcile.SHARE_LEVELS:
            raise ValueError("invalid share level")
        row = self.mirror_for(local_room_id)
        if not row:
            return
        master_room_id = row[0]
        self.master("PUT", "/_matrix/client/v3/rooms/"
                    + urllib.parse.quote(master_room_id, safe="")
                    # empty state_key: the CS-API spells that as the two-segment
                    # form (no trailing slash), same as an m.room.topic PUT.
                    + "/state/" + SHARE_LEVEL_TYPE, {"level": level})
        self.db.execute("UPDATE mirror_rooms SET stamped_level=? WHERE local_room_id=?",
                        (level, local_room_id))
        self.db.commit()
        log.info("share_level stamp %s -> %s", local_room_id, level)

    def create_mirror(self, local_room_id, source, name, profile=None, level=None):
        """Create a mirror room on MASTER, add to space, tag source, invite mgr.

        profile (when the room is a member of a SHARED contact profile) is
        {'id','displayName'} and is stamped as a com.jkali.profile state event so
        the master app can group this person's threads across platforms.
        level (D2b) is the room's share level ('share'|'direct'); it is stamped
        as com.jkali.share_level at creation and recorded in state.db so
        reconcile only re-PUTs it when it actually changes. An unrecognized
        level degrades to 'share' — under-promise: the master console then
        labels its affordance "Propose", never "Send".
        """
        cfg = self.cfg
        level = level if level in reconcile.SHARE_LEVELS else "share"
        initial_state = [
            {"type": SOURCE_TAG_TYPE, "state_key": "", "content": {"source": source or "unknown"}},
            {"type": SHARE_LEVEL_TYPE, "state_key": "", "content": {"level": level}},
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
            "(local_room_id, master_room_id, source, last_synced_pos, stamped_level) "
            "VALUES (?,?,?,?,?)",
            (local_room_id, master_room_id, source, None, level))
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
    #   - a proposal is written into the local proposals room as an ordinary
    #     draft the teammate reviews and sends themselves, with ONE exception:
    #     a conversation the teammate has explicitly set to the 'direct' level
    #     auto-sends it (D2) behind _direct_send_gate()'s eleven checks, and
    #     the artifact filed here is then a NON-ACTIONABLE record of that send.
    #     Person-targeted proposals are never auto-sent, and any gate failure
    #     falls back to the ordinary draft.
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
        # D2-11 FIRST: a master-identity rebinding invalidates the recorded
        # master proposals room (and the watermark + dedup map) before anything
        # below reads them, so a new master/manager gets a fresh room and a
        # cold-start batch rather than inheriting the old identity's state.
        self._direct_suspended = self.refresh_direct_send_binding()
        # Trust the recorded ids once created (no per-loop aliveness probe — that
        # is needless master/local request load every cycle). Rooms are durable;
        # a purged room would surface as a 404 on the next write and be handled.
        mpr = self.meta_get("master_proposals_room")
        if not mpr:
            body = {
                "name": "Proposals",
                "topic": MASTER_PROPOSALS_TOPIC,
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
                "topic": LOCAL_PROPOSALS_TOPIC,
                "preset": "private_chat",
                "initial_state": [{"type": PROPOSALS_MARKER, "state_key": "", "content": {}}],
            })
            lpr = res["room_id"]
            self.meta_set("local_proposals_room", lpr)
            log.info("created local proposals room %s", lpr)

        self.refresh_proposal_topics(mpr, lpr)

    def refresh_proposal_topics(self, master_room, local_room):
        """One-time re-PUT of both proposals-room topics after the D2 copy change.

        Topics are only written at createRoom, so an install whose rooms already
        exist would keep asserting the pre-D2 absolute ("nothing here is sent
        automatically") while auto-send is live — the room's own description
        would be lying to the teammate. Guarded by a state.db flag so it happens
        exactly once, and a no-op for freshly created rooms (which already carry
        the new copy).

        An HTTPError is logged and does NOT block the flag: a permission/API
        refusal must not re-attempt forever. MasterUnreachable propagates, so a
        transport outage leaves the flag unset and retries on a later pass.
        """
        if self.meta_get(TOPIC_COPY_FLAG) == "1":
            return
        try:
            self.master("PUT", "/_matrix/client/v3/rooms/"
                        + urllib.parse.quote(master_room, safe="")
                        + "/state/m.room.topic", {"topic": MASTER_PROPOSALS_TOPIC})
        except urllib.error.HTTPError as e:
            log.warning("master proposals topic update failed: %s", e)
        try:
            self.local("PUT", "/_matrix/client/v3/rooms/"
                       + urllib.parse.quote(local_room, safe="")
                       + "/state/m.room.topic", {"topic": LOCAL_PROPOSALS_TOPIC})
        except urllib.error.HTTPError as e:
            log.warning("local proposals topic update failed: %s", e)
        self.meta_set(TOPIC_COPY_FLAG, "1")

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

    # -- D2: the auto-send exception (direct-share-level plan) ---------------
    # Every gate below is a REFUSAL point: failing any one of them does not drop
    # the proposal, it routes it to the teammate's inbox as the ordinary
    # actionable draft it has always been. The gates are applied in the plan's
    # order (D2.1 .. D2.6, with D2.11's suspension first) so the cheapest and
    # most fundamental identity check runs before any network read.
    IDENTITY_META = "proposal_identity"          # meta: the bound master identity
    SUSPENDED_META = "direct_send_suspended_ts"  # meta: set => auto-send suspended
    # Gates whose failure is the ORDINARY case (a share-level room, a
    # person-targeted proposal, a cold start, a suspended or disabled
    # auto-send). They log at DEBUG; every other gate is a would-be auto-send
    # actively refused and logs at WARNING + writes a durable audit row.
    QUIET_GATES = ("suspended", "disabled", "cold_start", "target", "consent")

    @staticmethod
    def _room_hash(room_id):
        """Pseudonymous room identifier for logs + the rate/audit tables.

        The rate cap and the audit trail must be per-room, but a room id names
        a conversation; state.db and the log therefore only ever hold this
        hash. Never log or store the room id itself on this path.
        """
        return hashlib.sha256((room_id or "").encode("utf-8")).hexdigest()

    def direct_send_identity(self):
        """D2-11: the master identity the proposal state is bound to."""
        return (self.cfg.master_hs or "", self.cfg.master_user or "",
                self.cfg.manager_mxid or "")

    def _write_suspension(self, identity, ts):
        """Write com.jkali.direct_send_suspended into LOCAL user account-data.

        This is the event apps/user's suspensionAffordance() renders and whose
        four fields ackDirectSendSuspension() echoes back verbatim. A failed
        write is logged, not raised: the suspension itself lives in state.db
        (meta), so auto-send stays off whether or not the UI could be told.
        """
        content = {"master_hs": identity[0], "master_user": identity[1],
                   "manager_mxid": identity[2], "ts": ts}
        path = ("/_matrix/client/v3/user/" + urllib.parse.quote(self.cfg.local_user, safe="")
                + "/account_data/" + DIRECT_SEND_SUSPENDED_TYPE)
        try:
            self.local("PUT", path, content)
        except Exception as e:                    # noqa: BLE001 — never blocks the suspension
            log.warning("could not surface the auto-send suspension to the app: %s", e)

    def _direct_send_ack_matches(self, identity, ts):
        """True iff com.jkali.direct_send_ack matches the CURRENT identity+ts.

        All four fields must match the suspension this daemon currently holds —
        an ack for a previous (or attacker-chosen) identity must not resume
        auto-send for a different one. Any read error, malformed content, or
        mismatch => False (stay suspended).
        """
        path = ("/_matrix/client/v3/user/" + urllib.parse.quote(self.cfg.local_user, safe="")
                + "/account_data/" + DIRECT_SEND_ACK_TYPE)
        try:
            data = self.local("GET", path)
        except Exception:                         # noqa: BLE001 — fail closed
            return False
        if not isinstance(data, dict):
            return False
        return (data.get("master_hs") == identity[0]
                and data.get("master_user") == identity[1]
                and data.get("manager_mxid") == identity[2]
                and data.get("ts") == ts)

    def refresh_direct_send_binding(self):
        """D2-11: bind proposal state to the master identity; return "suspended?".

        master_proposals_room / proposal_sync_since / proposal_map are only
        meaningful for ONE (master_hs, master_user, manager_mxid) tuple: a
        different master or a different manager mxid is a different authority,
        and reusing the watermark/dedup state across a rebinding would let a
        replacement identity inherit the old one's trust. So any change:
          - invalidates that state (the room id, the watermark and the dedup
            map are dropped, which puts the next pull under D2.3's cold-start
            rule -> the whole first batch routes to the inbox), and
          - SUSPENDS auto-send, writing com.jkali.direct_send_suspended for
            apps/user to surface. Auto-send resumes only once the teammate's
            com.jkali.direct_send_ack matches this exact tuple.
        The FIRST bind on an already-running install (no stored identity) is
        adoption, not a rebinding: nothing changed, so nothing is suspended.
        """
        identity = self.direct_send_identity()
        key = "\n".join(identity)
        stored = self.meta_get(self.IDENTITY_META)
        if stored is None:
            self.meta_set(self.IDENTITY_META, key)
        elif stored != key:
            self.db.execute("DELETE FROM meta WHERE k IN "
                            "('master_proposals_room','proposal_sync_since')")
            self.db.execute("DELETE FROM proposal_map")
            self.db.commit()
            ts = int(time.time() * 1000)
            self.meta_set(self.IDENTITY_META, key)
            self.meta_set(self.SUSPENDED_META, str(ts))
            self._write_suspension(identity, ts)
            log.warning("master identity rebinding: proposal state invalidated and "
                        "auto-send SUSPENDED until the teammate re-confirms in the app")
        raw = self.meta_get(self.SUSPENDED_META)
        if not raw:
            return False
        try:
            ts = int(raw)
        except (TypeError, ValueError):
            return True                            # unreadable marker => stay suspended
        if self._direct_send_ack_matches(identity, ts):
            self.db.execute("DELETE FROM meta WHERE k=?", (self.SUSPENDED_META,))
            self.db.commit()
            log.info("master identity re-confirmed by the teammate; auto-send resumed")
            return False
        return True

    def read_room_level(self, local_room_id):
        """D2-5: a FRESH point-read of one room's explicit share level.

        The reconcile pass's cached view can be minutes old, and the whole
        point of `direct` is that it authorizes a send with no human in the
        loop — so the level is re-read from the teammate's own account-data at
        the moment of sending, through the SAME resolver
        (consent.effective_level) the rest of the system uses. ANY failure —
        bad room id, HTTP error, transport error, junk content — resolves
        'private', never 'direct'.
        """
        if not isinstance(local_room_id, str) or not ROOMID_RE.match(local_room_id):
            return "private"
        path = ("/_matrix/client/v3/user/" + urllib.parse.quote(self.cfg.local_user, safe="")
                + "/rooms/" + urllib.parse.quote(local_room_id, safe="")
                + "/account_data/" + consent.SHARE_OVERRIDE_TYPE)
        try:
            return consent.effective_level(self.local("GET", path))
        except Exception:                          # noqa: BLE001 — fail closed
            return "private"

    def direct_send_under_cap(self, room_hash, now=None):
        """D2-6: is this room under the rolling per-hour auto-send cap?

        The counter lives in state.db (direct_send_log, hash-only), so the cap
        survives KeepAlive restarts — an attacker who can crash the daemon
        cannot reset the budget. Expired ticks are pruned on read, so the table
        stays bounded by the cap itself.
        """
        now = int(time.time() if now is None else now)
        cutoff = now - DIRECT_SEND_WINDOW_S
        self.db.execute("DELETE FROM direct_send_log WHERE ts < ?", (cutoff,))
        self.db.commit()
        used = self.db.execute(
            "SELECT COUNT(*) FROM direct_send_log WHERE room_hash=? AND ts>=?",
            (room_hash, cutoff)).fetchone()[0]
        return used < self.cfg.direct_send_cap

    def _audit_direct_send(self, master_event_id, room_hash, outcome):
        """D2-10: durable, hash-only auto-send audit row (no body, no room id)."""
        self.db.execute(
            "INSERT INTO direct_send_audit (ts, master_event_id, room_hash, outcome) "
            "VALUES (?,?,?,?)", (int(time.time()), master_event_id, room_hash, outcome))
        self.db.commit()

    def _direct_send_gate(self, ev, clean, cold_start, suspended):
        """Every D2 gate, in order. Returns (body_to_send, failed_gate).

        body_to_send is the SANITIZED text and is non-None only when ALL gates
        passed; otherwise it is None and failed_gate names the first refusal
        (used for the hash-only log line and the audit row). No gate here has a
        side effect except D2-6's prune, so a refusal costs nothing.
        """
        # D2-11 first, before anything is read: a rebound (or never
        # re-confirmed) master identity cannot send, whatever else is true.
        if suspended:
            return None, "suspended"
        cfg = self.cfg
        if cfg.direct_send_cap <= 0:
            return None, "disabled"               # cap 0 = auto-send switched off
        # D2-1: SERVER-STAMPED sender only. The manager mxid must be configured
        # and must be exactly who the homeserver says authored the event.
        # cfg.master_user — the teammate's OWN scoped master account, which
        # holds PL 100 in that room and could therefore author a proposal — is
        # explicitly refused: it is a mirroring credential, never a send oracle.
        # content's created_by plays no part here (it is pinned cosmetic, F16).
        sender = ev.get("sender")
        if not isinstance(cfg.manager_mxid, str) or not cfg.manager_mxid:
            return None, "sender"
        if not isinstance(sender, str) or sender != cfg.manager_mxid:
            return None, "sender"
        # Reachable only if manager_mxid were ever configured AS the teammate's
        # own master account — a misconfiguration that would otherwise turn the
        # mirroring credential into a send oracle. Refuse it explicitly rather
        # than relying on the check above to be the only thing standing there.
        if cfg.master_user and sender == cfg.master_user:
            return None, "sender"
        # D2-2: send-grade sanitization on top of the field whitelist.
        body = sanitize_send_body(clean.get("body"))
        if body is None:
            return None, "sanitize"
        # D2-3: freshness / replay bound. A cold start (no watermark, or an
        # empty dedup map — e.g. a restored or lost state.db) would otherwise
        # replay up to 100 historical proposals as real sends, so the WHOLE
        # first batch goes to the inbox.
        if cold_start:
            return None, "cold_start"
        ots = ev.get("origin_server_ts")
        if not isinstance(ots, int) or isinstance(ots, bool):
            return None, "freshness"
        now_ms = int(time.time() * 1000)
        if ots > now_ms + 60000 or (now_ms - ots) > DIRECT_SEND_FRESH_MS:
            return None, "freshness"              # stale, or implausibly future-dated
        # D2-4: POSITIVE target check — the proposal must be room-targeted AND
        # that room must be a member of the mirrored set. A person-targeted
        # (start-new-chat) proposal has no target_room and is NEVER auto-sent;
        # a room outside mirror_rooms (a bridge management room, a random room
        # id from a hostile master) fails membership rather than a denylist.
        target = clean.get("target_room")
        if not isinstance(target, str) or not target:
            return None, "target"
        if not self.mirror_for(target):
            return None, "target"
        # D2-5: fresh consent point-read at send time.
        if self.read_room_level(target) != "direct":
            return None, "consent"
        # D2-6: persisted rolling per-room cap.
        if not self.direct_send_under_cap(self._room_hash(target)):
            return None, "cap"
        return body, None

    def _auto_send(self, master_event_id, local_room_id, body):
        """D2-7/9/10: send one proposal into the conversation. (outcome, event_id).

        Ordering is the safety property: the 'attempted' outcome and the rate-cap
        tick are COMMITTED BEFORE the PUT, so a crash anywhere after this point
        is recoverable as "may already have been sent" instead of replayable as
        a second send. The transaction id is deterministic
        (autosend_<master_event_id>), so even a retry the homeserver did receive
        is de-duplicated server-side.

        Outcomes:
          'sent'      — the homeserver confirmed; caller files the auto_sent record.
          'ambiguous' — dispatched, result unknown (transport failure, or a 5xx
                        that may have been applied): caller files the labelled
                        "may already have been sent" record, never a plain draft.
          'fallback'  — the homeserver answered 4xx: it refused, nothing was
                        sent, so the caller files the ordinary actionable draft.
        """
        room_hash = self._room_hash(local_room_id)
        self.db.execute("INSERT OR REPLACE INTO proposal_map "
                        "(master_event_id, local_event_id, outcome) VALUES (?,?,?)",
                        (master_event_id, None, "attempted"))
        self.db.execute("INSERT INTO direct_send_log (ts, room_hash) VALUES (?,?)",
                        (int(time.time()), room_hash))
        self.db.commit()
        txn = "autosend_" + urllib.parse.quote(master_event_id, safe="")
        content = {"msgtype": "m.text", "body": body,
                   # cosmetic provenance only (F14) — never read by trust logic
                   AUTO_SENT_FROM_PROPOSAL_KEY: master_event_id}
        try:
            res = self.local("PUT", "/_matrix/client/v3/rooms/"
                             + urllib.parse.quote(local_room_id, safe="")
                             + "/send/m.room.message/" + txn, content)
        except urllib.error.HTTPError as e:
            if isinstance(e.code, int) and 400 <= e.code < 500:
                self._audit_direct_send(master_event_id, room_hash, "failed")
                log.warning("direct send refused by the local hs (HTTP %s) room=%s",
                            e.code, room_hash)
                return "fallback", None
            self._audit_direct_send(master_event_id, room_hash, "ambiguous")
            log.warning("direct send outcome UNKNOWN (HTTP %s) room=%s", e.code, room_hash)
            return "ambiguous", None
        except Exception as e:                     # noqa: BLE001 — dispatched, unknown
            self._audit_direct_send(master_event_id, room_hash, "ambiguous")
            log.warning("direct send outcome UNKNOWN (%s) room=%s", type(e).__name__, room_hash)
            return "ambiguous", None
        self._audit_direct_send(master_event_id, room_hash, "sent")
        log.info("direct send: 1 proposal auto-sent room=%s", room_hash)
        sent_id = res.get("event_id") if isinstance(res, dict) else None
        return "sent", (sent_id if isinstance(sent_id, str) and sent_id else None)

    def _file_proposal_record(self, local_proposals_room, master_event_id, content, outcome):
        """Write THE one inbox artifact for a proposal and record its outcome.

        Exactly one of these runs per proposal on every path (ordinary draft,
        auto-sent record, ambiguous record), which is what makes "exactly one
        inbox artifact per proposal" true. The event type is still the hardcoded
        com.jkali.proposal literal and the target is still the recorded local
        proposals room — an auto-sent MESSAGE goes to the conversation, never
        here, and this record is never an m.room.message. The txn id is
        unchanged (proposal_<master_event_id>) so a replay is HS-idempotent.
        """
        res = self.local("PUT", "/_matrix/client/v3/rooms/"
                         + urllib.parse.quote(local_proposals_room, safe="")
                         + "/send/" + PROPOSAL_TYPE + "/proposal_"
                         + urllib.parse.quote(master_event_id, safe=""), content)
        self.db.execute("INSERT OR REPLACE INTO proposal_map "
                        "(master_event_id, local_event_id, outcome) VALUES (?,?,?)",
                        (master_event_id, (res or {}).get("event_id"), outcome))
        self.db.commit()

    def forward_proposals(self, master_room_id, local_proposals_room, events,
                          cold_start=True, suspended=True):
        """Write each NEW master proposal into the local proposals room, once.

        SAFETY: the write target is asserted to be exactly the recorded local
        proposals room (never a mirror/conversation room), and the event type is
        hardcoded com.jkali.proposal (never m.room.message). Idempotent via
        proposal_map. Returns the count of inbox artifacts written.

        D2: a proposal whose target conversation is at the 'direct' level and
        that passes every gate is ALSO auto-sent into that conversation first
        (_direct_send_gate + _auto_send); the artifact filed here is then the
        non-actionable record of that send, not a draft. cold_start and
        suspended default to True — fail closed, so any caller that has not
        established D2.3's freshness precondition and D2.11's identity binding
        gets the pre-D2 behavior (inbox only).
        """
        recorded = self.meta_get("local_proposals_room")
        if not local_proposals_room or local_proposals_room != recorded:
            return 0
        # Never let the proposals target collide with a mirror room id.
        if self.db.execute("SELECT 1 FROM mirror_rooms WHERE master_room_id=?",
                            (local_proposals_room,)).fetchone():
            return 0
        handled = {r[0]: r[1] for r in self.db.execute(
            "SELECT master_event_id, outcome FROM proposal_map").fetchall()}
        posted = 0
        for ev in events:
            if not isinstance(ev, dict) or ev.get("type") != PROPOSAL_TYPE:
                continue
            meid = ev.get("event_id")
            if not meid:
                continue
            if meid in handled:
                if handled[meid] != "attempted":
                    continue                       # already has its one artifact
                # D2-9 crash recovery: intent was recorded, the dispatch result
                # is unknown, and no artifact exists yet. Never re-send — file
                # the labelled "may already have been sent" record instead.
                clean = self._sanitize_proposal(ev)
                if clean is None:
                    self.db.execute("UPDATE proposal_map SET outcome='fallback' "
                                    "WHERE master_event_id=?", (meid,))
                    self.db.commit()
                    handled[meid] = "fallback"
                    continue
                content = dict(clean)
                content[SEND_AMBIGUOUS_KEY] = True
                self._file_proposal_record(local_proposals_room, meid, content, "ambiguous")
                self._audit_direct_send(meid, self._room_hash(clean.get("target_room")),
                                        "ambiguous_recovered")
                log.warning("recovered an interrupted direct send as AMBIGUOUS "
                            "(no re-send): %s", meid)
                handled[meid] = "ambiguous"
                posted += 1
                continue
            clean = self._sanitize_proposal(ev)
            if clean is None:
                # Record as handled so a malformed proposal is not retried forever.
                self.db.execute("INSERT OR REPLACE INTO proposal_map "
                                "(master_event_id, local_event_id, outcome) VALUES (?,?,?)",
                                (meid, None, "fallback"))
                self.db.commit()
                handled[meid] = "fallback"
                continue
            body, gate = self._direct_send_gate(ev, clean, cold_start, suspended)
            if body is None:
                # Refused: the ordinary actionable draft the teammate sends
                # themselves. Log/audit hash-only, naming the gate, never the
                # body — loudly when a would-be auto-send was actively refused.
                if gate in self.QUIET_GATES:
                    log.debug("proposal not auto-sent (%s)", gate)
                else:
                    room_hash = self._room_hash(clean.get("target_room"))
                    log.warning("direct send REFUSED at gate '%s' room=%s", gate, room_hash)
                    self._audit_direct_send(meid, room_hash, "refused:" + gate)
                self._file_proposal_record(local_proposals_room, meid, clean, "fallback")
                handled[meid] = "fallback"
                posted += 1
                continue
            outcome, sent_id = self._auto_send(meid, clean["target_room"], body)
            content = dict(clean)
            if outcome == "sent":
                content[AUTO_SENT_KEY] = True
                if sent_id:
                    content[SENT_EVENT_ID_KEY] = sent_id
            elif outcome == "ambiguous":
                content[SEND_AMBIGUOUS_KEY] = True
            # 'fallback' (the hs refused): the plain actionable draft, unflagged.
            self._file_proposal_record(local_proposals_room, meid, content, outcome)
            handled[meid] = outcome
            posted += 1
        return posted

    def pull_proposals(self):
        """One non-blocking master /sync of the proposals room; forward new ones.

        Reads the MASTER as the teammate's own account (outbound-only preserved).
        The proposal watermark (proposal_sync_since) advances only after the batch
        is forwarded; a master transport failure raises MasterUnreachable and the
        watermark is left untouched (buffer + retry, like the mirror-up path).

        This is also where D2.3's two auto-send preconditions are established:
        whether this batch arrived on an INCREMENTAL sync with a populated dedup
        map (otherwise the whole batch is a cold start and goes to the inbox),
        and whether auto-send is currently suspended by D2.11's identity
        binding. Both default to the refusing value if anything is unclear."""
        mpr = self.meta_get("master_proposals_room")
        lpr = self.meta_get("local_proposals_room")
        if not mpr or not lpr:
            return
        since = self.meta_get("proposal_sync_since")
        # D2-3 cold start: no watermark, or an empty dedup map (a fresh, lost or
        # restored state.db). Either way this batch may be historical, so the
        # WHOLE of it routes to the inbox — auto-send needs an INCREMENTAL sync.
        cold_start = not since or not self.db.execute(
            "SELECT 1 FROM proposal_map LIMIT 1").fetchone()
        # Fail closed if ensure_proposal_rooms() has not resolved the binding.
        suspended = getattr(self, "_direct_suspended", True)
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
        posted = self.forward_proposals(mpr, lpr, events,
                                        cold_start=bool(cold_start), suspended=suspended)
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
