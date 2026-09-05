#!/usr/bin/env python3
"""iMessage <-> Matrix appservice daemon (Phase 1 MVP).

Security contract: PLAN-IMSG.md M-1..M-22 (GOVERNING). Highlights enforced
here: sender allowlist (M-1), mapped-chats-only destinations (M-2), fixed
port + fail-closed bind (M-3), attachment path validation both ways (M-4),
hardened stdlib HTTP listener (M-6), constant-time hs_token check (M-7),
txn idempotency + per-chat rate caps (M-8), untrusted-input clamps and
injective ghost localparts (M-9), body-free INFO logging (M-12), hash-only
echo ledger (M-13), list-argv/shell=False engine calls (M-20; the CLI takes `--` literally, so it is omitted — leading-dash text is tolerated as a positional, verified).
Python 3.9 stdlib only.
"""
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
# Importing the module is inert: tests and diagnostics never open the installed
# database/configuration or invoke the native executable. main() initializes it.
CFG = {}
PORT = 29350
HS = DOMAIN = USER_ID = BOT_ID = AS_TOKEN = HS_TOKEN = CLI = ""
STATE_DIR = BASE
_transport = None
_runner = subprocess.run
MAX_BODY = 8 * 1024 * 1024        # 8 MiB transaction cap (M-6)
MAX_TEXT = CFG.get("max_body_kb", 64) * 1024
HOST_ALLOW = set()
ATTACH_PREFIXES = []
MXC_RE = re.compile(r"(?!)")
# SC-P2: start-chat handle validation. PHONE_RE uses literal ASCII [0-9] (+
# re.ASCII) to match the hub's JS \d exactly; EMAIL_RE mirrors the hub regex.
# EMAIL_RE may admit bidi/zero-width (SC-P6 clean_text()s the echoed reply).
PHONE_RE = re.compile(r"^\+[1-9][0-9]{6,14}$", re.ASCII)
EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{2,24}$")
RATE_PER_MIN = int(CFG.get("rate_per_chat_per_min", 30))
BACKFILL_COUNT = 25  # configured during initialize()
BACKFILL_GLOBAL_CAP = 500          # per-poll throughput budget; unfinished work remains pending
MGMT_MARKER_TYPE = "com.jkali.bridge.mgmt"
MGMT_MARKER_KEY = "imessage"

# Engine reaction schema (verified against Reaction.swift / MessageReaction) ->
# fixed display emoji. R-2: known keys map to a constant here; the raw engine
# value is NEVER interpolated for a known key. Unknown/custom keys fall to the
# sanitized-and-capped default path (clean_reaction_key).
RXN_KEY_EMOJI = {
    "heart": "❤️", "like": "\U0001f44d", "dislike": "\U0001f44e",
    "laugh": "\U0001f602", "emphasize": "‼️", "question": "❓",
}
# Reverse map for outbound Matrix m.reaction key (emoji) -> engine standard key.
EMOJI_TO_KEY = {
    "❤": "heart", "❤️": "heart",
    "\U0001f44d": "like", "\U0001f44e": "dislike", "\U0001f602": "laugh",
    "‼": "emphasize", "‼️": "emphasize", "❓": "question",
}
# B-7: fixed allowlist of System Settings deep-link URLs. dict lookup with NO
# default (a missing grant key = no `open` invocation). The command word only
# selects which of these constants (or none) is opened; no Matrix-derived text
# is ever interpolated into the URL or the argv.
GRANT_PANE_URL = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "contacts":      "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts",
    "automation":    "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation",
    "fulldisk":      "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
}

log = logging.getLogger("imsgd")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# ---------------------------------------------------------------- sanitizing
_CTRL = re.compile("[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\u202A-\u202E\u2066-\u2069\u200B-\u200F\uFEFF]")

def clean_text(s, limit=None):
    if not isinstance(s, str):
        return ""
    return _CTRL.sub("", s)[:MAX_TEXT if limit is None else limit]

def clean_name(s):
    return clean_text(s, 96) or "Unknown"

def ghost_localpart(handle):
    """Deterministic injective encoding (M-9): [a-z0-9] pass, else =xx hex."""
    out = []
    for ch in str(handle):
        if ch.isascii() and (ch.islower() or ch.isdigit()):
            out.append(ch)
        elif ch.isascii() and ch.isupper():
            out.append("=%02x" % ord(ch))  # keep injective across case
        else:
            out.append("".join("=%02x" % b for b in ch.encode("utf-8")))
    return "imessage_" + "".join(out)

def sha(t):
    return hashlib.sha256(t.encode("utf-8", "replace")).hexdigest()

# ---------------------------------------------------------------- state (M-10)
DB = None
DBLOCK = threading.RLock()
EVENT_LOCK = threading.Lock()
_EVENT_CONTEXT = threading.local()
_tail_scan_success = {}
_tail_scan_attempt = {}


def initialize(config=None, state_dir=None, transport=None, runner=None):
    """Initialize one daemon before starting threads; injectable without live I/O."""
    global CFG, PORT, HS, DOMAIN, USER_ID, BOT_ID, AS_TOKEN, HS_TOKEN, CLI
    global MAX_TEXT, HOST_ALLOW, ATTACH_PREFIXES, MXC_RE, RATE_PER_MIN
    global BACKFILL_COUNT, STATE_DIR, DB, _transport, _runner
    install_root = os.environ.get("BEEPA_INSTALL_ROOT")
    default_state = os.path.join(install_root, "imessage") if install_root else BASE
    if config is None:
        path = os.environ.get("IMESSAGE_CONFIG", os.path.join(default_state, "daemon.json"))
        with open(path) as f:
            config = json.load(f)
    CFG = dict(config)
    PORT = int(CFG["port"])
    HS, DOMAIN = CFG["hs_url"].rstrip("/"), CFG["domain"]
    USER_ID, BOT_ID = CFG["user_id"], CFG["bot_id"]
    AS_TOKEN, HS_TOKEN, CLI = CFG["as_token"], CFG["hs_token"], CFG["cli_path"]
    MAX_TEXT = int(CFG.get("max_body_kb", 64)) * 1024
    HOST_ALLOW = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"host.docker.internal:{PORT}"}
    ATTACH_PREFIXES = [os.path.realpath(p) for p in CFG["attachment_allow_prefixes"]]
    MXC_RE = re.compile(r"^mxc://" + re.escape(DOMAIN) + r"/([A-Za-z0-9_-]{1,255})$")
    RATE_PER_MIN = int(CFG.get("rate_per_chat_per_min", 30))
    BACKFILL_COUNT = max(0, int(CFG.get("backfill_count", 25)))
    STATE_DIR = os.path.abspath(state_dir or os.environ.get("IMESSAGE_STATE_DIR", default_state))
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(STATE_DIR, "tmp"), mode=0o700, exist_ok=True)
    _transport, _runner = transport, runner or subprocess.run
    if DB is not None:
        DB.close()
    db_path = os.path.join(STATE_DIR, "state.db")
    DB = sqlite3.connect(db_path, check_same_thread=False)
    DB.execute("CREATE TABLE IF NOT EXISTS map (chat_id TEXT PRIMARY KEY, room_id TEXT UNIQUE)")
    DB.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    DB.execute("CREATE TABLE IF NOT EXISTS ledger (h TEXT PRIMARY KEY, ts REAL)")  # hashes only (M-13)
    DB.execute("CREATE TABLE IF NOT EXISTS seen_msg (chat_id TEXT, msg_id TEXT, PRIMARY KEY (chat_id, msg_id))")
    DB.execute("CREATE TABLE IF NOT EXISTS txns (txn_id TEXT PRIMARY KEY, ts REAL)")
    # SC-P5: persistent start-chat caps (survives launchd restart, unlike in-memory).
    # Append-only attempt log; global counts bound fan-out, handle_hash gives 60s dedup.
    DB.execute("CREATE TABLE IF NOT EXISTS startchat_log (ts REAL, handle_hash TEXT)")
    # (chat_id,msg_id) -> mapped Matrix event id (R-1 prereq). Ids only, no bodies
    # (M-13); body_hash is a sha256 hex used solely for inbound-edit detection.
    DB.execute("CREATE TABLE IF NOT EXISTS event_map "
               "(chat_id TEXT, msg_id TEXT, event_id TEXT, sender TEXT, body_hash TEXT, "
               "PRIMARY KEY (chat_id, msg_id))")
    DB.execute("CREATE INDEX IF NOT EXISTS event_map_ev ON event_map(event_id)")
    # Inbound reactions already relayed -> Matrix (dedupe + inbound-unreact redaction).
    DB.execute("CREATE TABLE IF NOT EXISTS rxn_in "
               "(chat_id TEXT, target_msg TEXT, rxn_id TEXT, event_id TEXT, "
               "PRIMARY KEY (chat_id, target_msg, rxn_id))")
    # Outbound m.reaction events the daemon itself mapped to an engine react (R-6:
    # only these events, redacted in the same room, ever reach engine unreact).
    DB.execute("CREATE TABLE IF NOT EXISTS rxn_out "
               "(event_id TEXT PRIMARY KEY, chat_id TEXT, target_msg TEXT, rkey TEXT)")
    DB.execute("CREATE TABLE IF NOT EXISTS inbound_pending (chat_id TEXT, msg_id TEXT, "
               "status TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(chat_id,msg_id))")
    DB.execute("CREATE TABLE IF NOT EXISTS inbound_component (room_id TEXT, msg_id TEXT, "
               "component TEXT, event_id TEXT, status TEXT, PRIMARY KEY(room_id,msg_id,component))")
    DB.execute("CREATE TABLE IF NOT EXISTS outbound_event (event_id TEXT PRIMARY KEY, room_id TEXT, "
               "state TEXT NOT NULL, updated REAL NOT NULL, reason TEXT, engine_id TEXT)")
    DB.execute("CREATE TABLE IF NOT EXISTS dispatch_rate (chat_hash TEXT, ts REAL)")
    # The only safe recovery after an interrupted native invocation is ambiguity.
    # A queued/preflight failure is replayable; a dispatch may already have sent.
    DB.execute("UPDATE outbound_event SET state='ambiguous', reason='restart_during_dispatch' "
               "WHERE state='dispatching'")
    DB.execute("UPDATE outbound_event SET state='retryable' WHERE state='processing'")
    DB.commit()
    os.chmod(db_path, 0o600)
    _rate.clear()
    _backfill_posted[0] = 0
    _portal_create_hist.clear()
    _tail_scan_success.clear()
    _tail_scan_attempt.clear()


def meta_get(k, default=None):
    with DBLOCK:
        r = DB.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default

def meta_set(k, v):
    with DBLOCK:
        DB.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)", (k, v))
        DB.commit()

def room_for_chat(chat_id):
    with DBLOCK:
        r = DB.execute("SELECT room_id FROM map WHERE chat_id=?", (chat_id,)).fetchone()
    return r[0] if r else None

def chat_for_room(room_id):
    with DBLOCK:
        r = DB.execute("SELECT chat_id FROM map WHERE room_id=?", (room_id,)).fetchone()
    return r[0] if r else None

def map_add(chat_id, room_id):
    with DBLOCK:
        DB.execute("INSERT OR REPLACE INTO map (chat_id, room_id) VALUES (?,?)", (chat_id, room_id))
        DB.commit()


def component_txn(room_id, msg_id, component):
    return "imsgd_" + sha(json.dumps([room_id, msg_id, component], separators=(",", ":")))


def component_get(room_id, msg_id, component):
    with DBLOCK:
        return DB.execute("SELECT event_id,status FROM inbound_component "
                          "WHERE room_id=? AND msg_id=? AND component=?",
                          (room_id, msg_id, component)).fetchone()


def component_put(room_id, msg_id, component, event_id, status="confirmed"):
    with DBLOCK:
        DB.execute("INSERT OR REPLACE INTO inbound_component VALUES (?,?,?,?,?)",
                   (room_id, msg_id, component, event_id, status))
        DB.commit()


def inbound_complete(chat_id, msg_id):
    with DBLOCK:
        DB.execute("INSERT OR IGNORE INTO seen_msg VALUES (?,?)", (chat_id, msg_id))
        DB.execute("DELETE FROM inbound_pending WHERE chat_id=? AND msg_id=?", (chat_id, msg_id))
        DB.commit()


def inbound_pending(chat_id, msg_id, status="pending"):
    with DBLOCK:
        DB.execute("INSERT OR REPLACE INTO inbound_pending VALUES (?,?,?)", (chat_id, msg_id, status))
        DB.commit()


def chat_pending(chat_id):
    with DBLOCK:
        return DB.execute("SELECT 1 FROM inbound_pending WHERE chat_id=? LIMIT 1", (chat_id,)).fetchone() is not None

def event_map_put(chat_id, msg_id, event_id, sender, body_hash):
    with DBLOCK:
        DB.execute("INSERT OR REPLACE INTO event_map (chat_id, msg_id, event_id, sender, body_hash) "
                   "VALUES (?,?,?,?,?)", (chat_id, msg_id, event_id, sender, body_hash))
        DB.commit()

def event_map_get(chat_id, msg_id):
    with DBLOCK:
        r = DB.execute("SELECT event_id, sender, body_hash FROM event_map WHERE chat_id=? AND msg_id=?",
                       (chat_id, msg_id)).fetchone()
    return r  # (event_id, sender, body_hash) or None

def event_map_by_event(event_id):
    with DBLOCK:
        r = DB.execute("SELECT chat_id, msg_id FROM event_map WHERE event_id=?", (event_id,)).fetchone()
    return r  # (chat_id, msg_id) or None

def rxn_in_seen(chat_id, target_msg, rxn_id):
    with DBLOCK:
        r = DB.execute("SELECT 1 FROM rxn_in WHERE chat_id=? AND target_msg=? AND rxn_id=?",
                       (chat_id, target_msg, rxn_id)).fetchone()
    return bool(r)

def rxn_in_add(chat_id, target_msg, rxn_id, event_id):
    with DBLOCK:
        DB.execute("INSERT OR REPLACE INTO rxn_in (chat_id, target_msg, rxn_id, event_id) "
                   "VALUES (?,?,?,?)", (chat_id, target_msg, rxn_id, event_id))
        DB.commit()

def rxn_out_add(event_id, chat_id, target_msg, rkey):
    with DBLOCK:
        DB.execute("INSERT OR REPLACE INTO rxn_out (event_id, chat_id, target_msg, rkey) "
                   "VALUES (?,?,?,?)", (event_id, chat_id, target_msg, rkey))
        DB.commit()

def rxn_out_get(event_id):
    with DBLOCK:
        r = DB.execute("SELECT chat_id, target_msg, rkey FROM rxn_out WHERE event_id=?",
                       (event_id,)).fetchone()
    return r  # (chat_id, target_msg, rkey) or None

def clean_reaction_key(raw):
    """R-2: reaction key is arbitrary remote text. Strip control/bidi, cap at 8
    codepoints, reject empty. Used for the default (custom-emoji/unknown) path;
    known engine keys never reach here (they map to a constant emoji)."""
    s = clean_text(raw, 64) if isinstance(raw, str) else ""
    cps = list(s)
    if len(cps) > 8:
        return ""
    s = "".join(cps).strip()
    return s  # may be "" -> caller drops

def engine_reaction_display(reaction_key, is_emoji):
    """Engine reactionKey -> Matrix annotation key. Known standard keys map to a
    fixed emoji constant (raw value never interpolated); anything else is treated
    as untrusted remote text and sanitized+capped. Returns "" to drop."""
    if isinstance(reaction_key, str) and reaction_key in RXN_KEY_EMOJI:
        return RXN_KEY_EMOJI[reaction_key]     # known key -> constant, never raw
    return clean_reaction_key(reaction_key)    # custom emoji / unknown -> sanitized

def matrix_reaction_to_engine_key(raw_key):
    """Outbound Matrix m.reaction key (usually an emoji) -> engine react key.
    Known emoji map to the standard engine key; otherwise the sanitized+capped
    emoji is passed through (engine accepts arbitrary emoji as a positional).
    Returns "" to drop."""
    if not isinstance(raw_key, str):
        return ""
    if raw_key in EMOJI_TO_KEY:
        return EMOJI_TO_KEY[raw_key]
    stripped = raw_key.replace("️", "")
    if stripped in EMOJI_TO_KEY:
        return EMOJI_TO_KEY[stripped]
    return clean_reaction_key(raw_key)

def txn_seen(txn_id):
    with DBLOCK:
        r = DB.execute("SELECT 1 FROM txns WHERE txn_id=?", (txn_id,)).fetchone()
    return bool(r)

def txn_mark(txn_id):
    with DBLOCK:
        DB.execute("INSERT OR IGNORE INTO txns (txn_id, ts) VALUES (?,?)", (txn_id, time.time()))
        # Keep receipt IDs across late homeserver replays. They contain no bodies.
        DB.commit()

def ledger_add(text):
    with DBLOCK:
        DB.execute("INSERT OR REPLACE INTO ledger (h, ts) VALUES (?,?)", (sha(text), time.time()))
        DB.execute("DELETE FROM ledger WHERE ts < ?", (time.time() - 120,))
        DB.commit()

def ledger_has(text):
    with DBLOCK:
        r = DB.execute("SELECT 1 FROM ledger WHERE h=? AND ts>?", (sha(text), time.time() - 60)).fetchone()
    return bool(r)

def startchat_gate(handle):
    """SC-P5: persistent start-chat caps. Prune, then deny if the GLOBAL count
    (all handles, to bound fan-out) is >=3 in the last 3600s OR >=10 in the last
    86400s, OR this handle was attempted within 60s (per-handle dedup by sha).
    On allow, records the attempt row atomically. Returns (ok, hour_ct, day_ct)."""
    now = time.time()
    hh = sha(handle)
    with DBLOCK:
        DB.execute("DELETE FROM startchat_log WHERE ts < ?", (now - 86400,))
        hour_ct = DB.execute("SELECT COUNT(*) FROM startchat_log WHERE ts > ?",
                             (now - 3600,)).fetchone()[0]
        day_ct = DB.execute("SELECT COUNT(*) FROM startchat_log WHERE ts > ?",
                            (now - 86400,)).fetchone()[0]
        dup = DB.execute("SELECT 1 FROM startchat_log WHERE handle_hash=? AND ts > ?",
                         (hh, now - 60)).fetchone()
        if hour_ct >= 3 or day_ct >= 10 or dup:
            DB.commit()
            return False, hour_ct, day_ct
        DB.execute("INSERT INTO startchat_log (ts, handle_hash) VALUES (?,?)", (now, hh))
        DB.commit()
    return True, hour_ct, day_ct

# ---------------------------------------------------------------- rate (M-8)
_rate = {}
_rate_lock = threading.Lock()

def rate_ok(chat_id):
    now = time.time()
    with _rate_lock, DBLOCK:
        DB.execute("DELETE FROM dispatch_rate WHERE ts < ?", (now - 60,))
        hist = [r[0] for r in DB.execute("SELECT ts FROM dispatch_rate WHERE chat_hash=? ORDER BY ts", (sha(chat_id),))]
        if hist and now - hist[-1] < 1.0:
            DB.commit()
            return False
        if len(hist) >= RATE_PER_MIN:
            DB.commit()
            return False
        DB.execute("INSERT INTO dispatch_rate VALUES (?,?)", (sha(chat_id), now))
        DB.commit()
        return True

# ---------------------------------------------------------------- matrix api
def mx(method, path, body=None, user=None, raw=False, content_type=None):
    if _transport is not None:
        return _transport(method, path, body, user=user, raw=raw, content_type=content_type)
    q = {"user_id": user} if user else {}
    url = HS + path + (("?" + urllib.parse.urlencode(q)) if q else "")
    data = body if raw else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", "Bearer " + AS_TOKEN)
    req.add_header("Content-Type", content_type or "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def ensure_user(localpart):
    try:
        mx("POST", "/_matrix/client/v3/register",
           {"type": "m.login.application_service", "username": localpart})
    except urllib.error.HTTPError as e:
        if e.code != 400:  # M_USER_IN_USE arrives as 400
            raise

def set_displayname(user_id, name):
    lp = urllib.parse.quote(user_id, safe="")
    try:
        mx("PUT", f"/_matrix/client/v3/profile/{lp}/displayname",
           {"displayname": name}, user=user_id)
    except Exception:
        log.info("displayname set failed user=%s", sha(user_id)[:8])

def send_text(room_id, sender, text, extra=None, txn=None):
    rid = urllib.parse.quote(room_id, safe="")
    txn = txn or "imsgd" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    content = {"msgtype": "m.text", "body": text}
    if extra:
        content.update(extra)
    return mx("PUT", f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}",
              content, user=sender)

def send_reaction(room_id, sender, target_event, key, txn=None):
    rid = urllib.parse.quote(room_id, safe="")
    txn = txn or "imsgd" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    return mx("PUT", f"/_matrix/client/v3/rooms/{rid}/send/m.reaction/{txn}",
              {"m.relates_to": {"rel_type": "m.annotation", "event_id": target_event, "key": key}},
              user=sender)

def send_replace(room_id, sender, target_event, text, extra=None, txn=None):
    rid = urllib.parse.quote(room_id, safe="")
    txn = txn or "imsgd" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    content = {"msgtype": "m.text", "body": "* " + text,
               "m.new_content": {"msgtype": "m.text", "body": text},
               "m.relates_to": {"rel_type": "m.replace", "event_id": target_event}}
    if extra:
        content.update(extra)
        content["m.new_content"].update(extra)
    return mx("PUT", f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}",
              content, user=sender)

def upload_media(path, mime):
    with open(path, "rb") as f:
        data = f.read()
    name = urllib.parse.quote(os.path.basename(path))
    return mx("POST", f"/_matrix/media/v3/upload?filename={name}", data,
              raw=True, content_type=mime)["content_uri"]

# ---------------------------------------------------------------- engine (M-20)
def _extract_json(raw):
    """Engine stdout carries log lines around the JSON payload; extract it."""
    lines = raw.split("\n")
    buf, started = [], False
    for l in lines:
        st = l.strip()
        if not started and st.startswith("{"):
            started = True
        if started:
            if st == "Exiting...":
                break
            buf.append(l)
    return json.loads("\n".join(buf))

def cli_json(*args, timeout=60):
    argv = [CLI, "--no-events", "--json", *args]
    p = _runner(argv, capture_output=True, timeout=timeout, shell=False)
    if p.returncode != 0:
        raise RuntimeError("engine rc=%d" % p.returncode)
    if len(p.stdout) > 32 * 1024 * 1024:
        raise RuntimeError("engine output too large")
    return _extract_json(p.stdout.decode("utf-8", "replace"))

class EngineOutcome:
    """Confirmed means CLI acceptance; only delivered=True is delivery evidence."""
    def __init__(self, state, reason, engine_id=None, delivered=False):
        self.state, self.reason = state, reason
        self.engine_id, self.delivered = engine_id, delivered

    def __bool__(self):
        return self.state == "confirmed"


def _engine_mut(args, timeout):
    """Keep the signed CLI's own authorization preflight; no Python TCC gate."""
    try:
        p = _runner([CLI, *args], capture_output=True, timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return EngineOutcome("ambiguous", "engine_timeout")
    except OSError:
        # subprocess failed to start, so no native send could have happened.
        return EngineOutcome("retryable", "engine_unavailable")
    out = (p.stdout or b"") + (p.stderr or b"")
    if p.returncode != 0:
        if b"Accessibility" in out:
            log.info("engine blocked on missing Accessibility grant; grant the configured CLI")
            cmd_open("accessibility")
            return EngineOutcome("refused", "accessibility_required")
        # An arbitrary native failure can occur after dispatch. No blind resend.
        return EngineOutcome("ambiguous", "engine_failed")
    result = {}
    try:
        result = _extract_json((p.stdout or b"").decode("utf-8", "replace"))
    except (ValueError, TypeError):
        pass
    if not isinstance(result, dict):
        result = {}
    if result.get("error") or result.get("success") is False:
        return EngineOutcome("ambiguous", "engine_reported_error")
    mid = result.get("id")
    delivered = result.get("delivered") is True
    return EngineOutcome("confirmed", "engine_delivered" if delivered else "engine_accepted",
                         mid if isinstance(mid, str) else None, delivered)


def engine_send(chat_id, text):
    return _engine_mut(["--no-events", "send", chat_id, text], 60)

def engine_send_file(chat_id, path):
    return _engine_mut(["--no-events", "send-file", chat_id, path], 120)

def engine_react(msg_id, reaction):
    return _engine_mut(["--no-events", "react", msg_id, reaction], 60)

def engine_unreact(msg_id, reaction):
    return _engine_mut(["--no-events", "unreact", msg_id, reaction], 60)

def engine_edit(msg_id, text):
    return _engine_mut(["--no-events", "edit", msg_id, text], 60)

def engine_create_chat(handle, message):
    """SC-4: create a new iMessage chat. `handle` is a separate positional
    (already regex-validated + leading-dash-guarded); `message` is ONE
    "--message="-prefixed token so a leading-dash message cannot bind as a flag.
    No "--" terminator (it would break --message). list-argv, shell=False (M-20)."""
    return _engine_mut(["--no-events", "create-chat", handle, "--message=" + message], 60)

# ---------------------------------------------------------------- attachments (M-4)
def decode_asset_url(src):
    """Engine srcURL: asset://$accountID/<hex-encoded absolute path>."""
    m = re.match(r"^asset://[^/]+/([0-9a-fA-F]+)$", str(src))
    if not m:
        return None
    try:
        return bytes.fromhex(m.group(1)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None

def safe_engine_path(path):
    rp = os.path.realpath(str(path))
    return any(rp == p or rp.startswith(p + os.sep) for p in ATTACH_PREFIXES) and os.path.isfile(rp)


class AttachmentRefused(Exception):
    pass

def download_mxc_to_tmp(mxc, filename_hint):
    m = MXC_RE.match(str(mxc or ""))
    if not m:
        return None
    ext = ""
    hint = os.path.splitext(str(filename_hint or ""))[1]
    if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", hint or ""):
        ext = hint.lower()
    fd, tmp = tempfile.mkstemp(dir=os.path.join(STATE_DIR, "tmp"), suffix=ext)
    try:
        url = f"{HS}/_matrix/client/v1/media/download/{urllib.parse.quote(DOMAIN, safe='')}/{urllib.parse.quote(m.group(1))}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + AS_TOKEN)
        with urllib.request.urlopen(req, timeout=120) as r:
            size = 0
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 200 * 1024 * 1024:
                    raise AttachmentRefused("attachment too large")
                os.write(fd, chunk)
        return tmp
    except Exception as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(exc, AttachmentRefused):
            raise
        return None
    finally:
        os.close(fd)

# ---------------------------------------------------------------- portals
SPACE_KEY = "space_id"


def create_or_recover_room(payload, key):
    """A deterministic alias resolves a lost createRoom response before retry."""
    alias_localpart = "imessage_" + sha(BOT_ID + "\0" + key)
    payload = dict(payload, room_alias_name=alias_localpart)
    try:
        return mx("POST", "/_matrix/client/v3/createRoom", payload, user=BOT_ID)["room_id"]
    except Exception:
        alias = urllib.parse.quote("#" + alias_localpart + ":" + DOMAIN, safe="")
        room = mx("GET", "/_matrix/client/v3/directory/room/" + alias, user=BOT_ID)["room_id"]
        state = mx("GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(room, safe="") + "/state", user=BOT_ID)
        expected_type = (payload.get("creation_content") or {}).get("type")
        if not isinstance(state, list) or not any(
                e.get("type") == "m.room.create" and e.get("sender") == BOT_ID
                and (e.get("content") or {}).get("type") == expected_type for e in state):
            raise ValueError("room alias ownership mismatch")
        for expected in payload.get("initial_state", []):
            if expected["type"] == "uk.half-shot.bridge" and not any(
                    e.get("type") == expected["type"] and e.get("state_key") == expected["state_key"]
                    and e.get("sender") == BOT_ID and e.get("content") == expected["content"] for e in state):
                raise ValueError("room alias source mismatch")
        return room

def ensure_space():
    sid = meta_get(SPACE_KEY)
    if sid:
        return sid
    sid = create_or_recover_room({
        "name": "iMessage", "preset": "private_chat", "visibility": "private",
        "creation_content": {"type": "m.space"},
        "invite": [USER_ID],
    }, "space")
    meta_set(SPACE_KEY, sid)
    log.info("space created")
    return sid

def ensure_portal(chat_id, chat_name, is_group):
    room = room_for_chat(chat_id)
    if room:
        ensure_portal_link(chat_id, room)
        return room
    space = ensure_space()
    room = create_or_recover_room({
        "name": clean_name(chat_name or chat_id),
        "preset": "private_chat", "visibility": "private", "is_direct": not is_group,
        "invite": [USER_ID],
        "initial_state": [
            {"type": "uk.half-shot.bridge", "state_key": f"{DOMAIN}/imessage",
             "content": {"bridgebot": BOT_ID, "protocol": {"id": "imessage", "displayname": "iMessage"},
                         "channel": {"id": chat_id}}},
            {"type": "m.space.parent", "state_key": space,
             "content": {"via": [DOMAIN], "canonical": True}},
        ],
    }, "portal:" + chat_id)
    # Allocation is durable before the independent space-link operation.
    map_add(chat_id, room)
    ensure_portal_link(chat_id, room)
    log.info("portal created chat=%s", sha(chat_id)[:8])
    try:
        maybe_backfill(chat_id, room, chat_name, is_group)
    except Exception:
        log.info("backfill error chat=%s", sha(chat_id)[:8])
    return room


def ensure_portal_link(chat_id, room):
    flag = "portal_linked:" + room
    if meta_get(flag) == "1":
        return
    sp = urllib.parse.quote(ensure_space(), safe="")
    mx("PUT", f"/_matrix/client/v3/rooms/{sp}/state/m.space.child/{urllib.parse.quote(room, safe='')}",
       {"via": [DOMAIN]}, user=BOT_ID)
    meta_set(flag, "1")

# ---------------------------------------------------------------- backfill (R-4)
_backfill_posted = [0]              # reset each poll; completion is durable per message
_portal_create_hist = []           # portal-creation rate cap (backfill trigger)
_backfill_lock = threading.Lock()

def _portal_backfill_ok():
    now = time.time()
    with _backfill_lock:
        recent = [t for t in _portal_create_hist if now - t < 60]
        recent.append(now)
        _portal_create_hist[:] = recent
        return len(recent) <= 5        # <=5 portal backfills per rolling minute

def maybe_backfill(chat_id, room, chat_name, is_group):
    if BACKFILL_COUNT <= 0:
        return True
    flag = "backfill:%s:%s" % (chat_id, room)
    if meta_get(flag) == "1":
        return True
    if not _portal_backfill_ok():
        return False
    msgs = cli_json("messages", chat_id).get("items", [])
    if not isinstance(msgs, list):
        raise ValueError("invalid source history")
    complete = True
    for m in msgs[-BACKFILL_COUNT:]:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        with DBLOCK:
            seen = DB.execute("SELECT 1 FROM seen_msg WHERE chat_id=? AND msg_id=?", (chat_id, mid)).fetchone()
        if seen:
            continue
        inbound_pending(chat_id, mid)
        with _backfill_lock:
            if _backfill_posted[0] >= BACKFILL_GLOBAL_CAP:
                complete = False
                continue
            _backfill_posted[0] += 1
        try:
            _relay_message(chat_id, room, chat_name, is_group, m)
            reconcile_reactions(chat_id, m)
            inbound_complete(chat_id, mid)
        except Exception:
            complete = False
            log.info("backfill post pending chat=%s", sha(chat_id)[:8])
    if complete:
        meta_set(flag, "1")
    return complete


def ensure_ghost(handle, name):
    lp = ghost_localpart(handle)
    uid = f"@{lp}:{DOMAIN}"
    if meta_get("ghost:" + uid) != "1":
        ensure_user(lp)
        set_displayname(uid, clean_name(name or handle))
        meta_set("ghost:" + uid, "1")
    return uid

def ghost_join(uid, room_id):
    rid = urllib.parse.quote(room_id, safe="")
    try:
        mx("POST", f"/_matrix/client/v3/rooms/{rid}/invite", {"user_id": uid}, user=BOT_ID)
    except Exception:
        pass
    try:
        mx("POST", f"/_matrix/client/v3/rooms/{rid}/join", {}, user=uid)
    except Exception:
        pass

# ---------------------------------------------------------------- inbound poll
# Engine JSON field mapping is finalized in slice I3 against real output.
def sync_portal_name(chat, chat_id):
    """Keep an existing portal's room name in step with the engine's resolved
    chat title. Contact names can arrive AFTER portal creation (the CLI only
    resolves Contacts once granted access), so a portal created early would
    otherwise show the raw handle forever. Cheap: one meta compare per poll;
    a Matrix PUT only when the name actually changed. Names are never logged
    (M-12) — hash only."""
    room = room_for_chat(chat_id)
    if not room:
        return
    name = clean_name(chat_display_name(chat))
    if not name:
        return
    key = "cname:" + chat_id
    if meta_get(key) == name:
        return
    try:
        rid = urllib.parse.quote(room, safe="")
        mx("PUT", f"/_matrix/client/v3/rooms/{rid}/state/m.room.name",
           {"name": name}, user=BOT_ID)
        meta_set(key, name)
        log.info("portal renamed chat=%s", sha(chat_id)[:8])
    except Exception:
        log.info("portal rename failed chat=%s", sha(chat_id)[:8])

def poll_once():
    _backfill_posted[0] = 0  # throughput budget per pass, never a lifetime ceiling
    data = cli_json("chats")
    items = data.get("items", []) if isinstance(data, dict) else []
    now = time.monotonic()
    interval = max(1.0, float(CFG.get("tail_rescan_seconds", 30)))
    budget = max(1, int(CFG.get("tail_rescan_chats_per_poll", 8)))
    unchanged = []

    def scan(chat, chat_id, marker):
        _tail_scan_attempt[chat_id] = time.monotonic()
        try:
            if handle_chat_delta(chat, chat_id):
                if marker:
                    meta_set("cursor:" + chat_id, marker)
                # Advance only after the complete returned tail was handled.
                # A restart intentionally makes each unchanged tail due again.
                _tail_scan_success[chat_id] = time.monotonic()
        except Exception as e:
            log.info("chat pending chat=%s error=%s", sha(chat_id)[:8], type(e).__name__)

    for c in items if isinstance(items, list) else []:
        if not isinstance(c, dict):
            continue
        chat_id = str(c.get("id") or "")
        if not chat_id:
            continue
        sync_portal_name(c, chat_id)
        marker = str(c.get("timestamp") or "")
        if marker and meta_get("cursor:" + chat_id) == marker and not chat_pending(chat_id):
            # Chat timestamps are hints, not complete change tokens: an inbound
            # self-chat row (or an edit/reaction) may arrive behind that marker.
            last = _tail_scan_success.get(chat_id)
            if last is None or now - last >= interval:
                unchanged.append((c, chat_id, marker))
            continue
        scan(c, chat_id, marker)
    # Bound extra native reads and give failed tails their turn without letting
    # the first failing source starve later unchanged chats.
    unchanged.sort(key=lambda item: _tail_scan_attempt.get(item[1], float("-inf")))
    for chat, chat_id, marker in unchanged[:budget]:
        scan(chat, chat_id, marker)

CONTACTS_DB = os.path.realpath(os.path.join(BASE, "..", "agents", "contacts", "contacts.db"))

def local_contact_name(handle):
    """display_name from the teammate's own contacts store (agents/contacts —
    imported hourly from macOS Contacts by import_macos.py). Used when the
    engine cannot resolve a name itself: its CNContactStore access follows the
    TCC responsible process, which under launchd is this python — normally
    ungranted, so engine titles arrive empty. Read-only, fail-soft: any error
    (store absent, schema drift) returns None and the raw handle is shown."""
    if not handle:
        return None
    try:
        db = sqlite3.connect("file:%s?mode=ro" % CONTACTS_DB, uri=True)
        try:
            r = db.execute(
                "SELECT display_name FROM contacts WHERE network_id=? AND deleted=0",
                (handle.lower() if "@" in handle else handle,)).fetchone()
        finally:
            db.close()
        return r[0] if r and r[0] else None
    except Exception:
        return None

def chat_display_name(chat):
    title = chat.get("title") or ""
    if title:
        return title
    parts = (chat.get("participants") or {}).get("items", [])
    others = [p.get("phoneNumber") or p.get("email") or p.get("id", "")
              for p in parts if not p.get("isSelf")]
    if len(others) == 1:
        name = local_contact_name(others[0])
        if name:
            return name
    return ", ".join(x for x in others if x) or "Note to self"

def handle_chat_delta(chat, chat_id):
    # Never fall back to the embedded last-message snapshot on a failed history
    # read: advancing the chat marker from that snapshot loses the missing gap.
    msgs = cli_json("messages", chat_id).get("items", [])
    if not isinstance(msgs, list):
        raise ValueError("invalid source history")
    name = chat_display_name(chat)
    is_group = (chat.get("type") or "single") != "single"
    available = {str(m.get("id")) for m in msgs if isinstance(m, dict) and m.get("id")}
    with DBLOCK:
        for mid, in DB.execute("SELECT msg_id FROM inbound_pending WHERE chat_id=?", (chat_id,)).fetchall():
            if mid not in available:
                DB.execute("UPDATE inbound_pending SET status='source_unavailable' WHERE chat_id=? AND msg_id=?", (chat_id, mid))
        DB.commit()
    budget = max(1, int(CFG.get("poll_message_budget", 200)))
    processed = 0
    for m in msgs:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "")
        if not mid:
            continue
        with DBLOCK:
            seen = DB.execute("SELECT 1 FROM seen_msg WHERE chat_id=? AND msg_id=?", (chat_id, mid)).fetchone()
        inbound_pending(chat_id, mid)
        if not seen and processed >= budget:
            continue
        if not seen:
            processed += 1
        try:
            if not seen:
                deliver_inbound(chat_id, name, is_group, m)
                # Component receipts make retries safe even if this commit fails.
                with DBLOCK:
                    DB.execute("INSERT OR IGNORE INTO seen_msg VALUES (?,?)", (chat_id, mid))
                    DB.commit()
            reconcile_edit(chat_id, m)
            reconcile_reactions(chat_id, m)
            inbound_complete(chat_id, mid)
        except Exception as e:
            log.info("inbound pending chat=%s error=%s", sha(chat_id)[:8], type(e).__name__)
    return not chat_pending(chat_id)


def deliver_inbound(chat_id, chat_name, is_group, m):
    text = clean_text(m.get("text") or "")
    from_me = bool(m.get("isSender"))
    # R-5: echo ledger is chat-scoped (sha(chat_id\0payload)), not global.
    if from_me and text and ledger_has(chat_id + "\0" + text):
        log.info("echo suppressed chat=%s", sha(chat_id)[:8])
        return
    room = ensure_portal(chat_id, chat_name, is_group)
    _relay_message(chat_id, room, chat_name, is_group, m)

def _relay_message(chat_id, room, chat_name, is_group, m):
    """Deliver each source component once; persist no message bodies or paths."""
    text = clean_text(m.get("text") or "")
    from_me = m.get("isSender") is True
    sender_handle = str(m.get("senderID") or chat_id.rsplit(";", 1)[-1])
    mid = str(m.get("id") or "")
    if not mid:
        raise ValueError("source message ID required")
    extra = {"com.jkali.from_me": True} if from_me else None
    sender = BOT_ID if from_me else ensure_ghost(sender_handle, m.get("senderName") or sender_handle)
    if not from_me:
        ghost_join(sender, room)
    primary_event = None
    relayed_text_hash = sha(text)
    failures = []
    attachments = m.get("attachments") or []
    for index, att in enumerate(attachments):
        # Engine IDs when available; the source array index is the stable
        # fallback, never an on-disk path that may change after redownload.
        key = "attachment:" + str(att.get("id") or index)
        previous = component_get(room, mid, key)
        if previous:
            primary_event = previous[0] or primary_event
            continue
        path = decode_asset_url(att.get("srcURL") or "")
        if not path:
            component_put(room, mid, key, "", "refused_invalid_path")
            continue
        rp = os.path.realpath(path)
        allowed = any(rp == p or rp.startswith(p + os.sep) for p in ATTACH_PREFIXES)
        if not allowed:
            component_put(room, mid, key, "", "refused_path")
            continue
        if not safe_engine_path(rp):
            failures.append("attachment_unavailable")
            continue
        try:
            mime = mimetypes.guess_type(rp)[0] or "application/octet-stream"
            mxc = upload_media(rp, mime)
            content = {"msgtype": "m.image" if mime.startswith("image/") else "m.file",
                       "body": clean_name(os.path.basename(rp)), "url": mxc,
                       "info": {"mimetype": mime, "size": os.path.getsize(rp)}}
            if extra:
                content.update(extra)
            rid = urllib.parse.quote(room, safe="")
            txn = component_txn(room, mid, key)
            result = mx("PUT", f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}", content, user=sender)
            event_id = result["event_id"]
            if not event_id:
                raise ValueError("missing event receipt")
            component_put(room, mid, key, event_id)
            primary_event = event_id
        except Exception as e:
            failures.append(type(e).__name__)
    if text:
        previous = component_get(room, mid, "text")
        if previous:
            primary_event = previous[0] or primary_event
            if previous[1].startswith("confirmed:"):
                relayed_text_hash = previous[1].split(":", 1)[1]
        else:
            try:
                result = send_text(room, sender, text, extra, txn=component_txn(room, mid, "text"))
                event_id = result["event_id"]
                if not event_id:
                    raise ValueError("missing event receipt")
                component_put(room, mid, "text", event_id, "confirmed:" + sha(text))
                primary_event = event_id
            except Exception as e:
                failures.append(type(e).__name__)
    if primary_event:
        event_map_put(chat_id, mid, primary_event, sender, relayed_text_hash)
    if failures:
        raise OSError("source components still pending")
    return primary_event


def reconcile_reactions(chat_id, m):
    """Relay engine reactions on message `m` to Matrix m.reaction, once each
    (R-1 room scoping, R-2 key sanitization, R-5 echo ledger)."""
    reactions = m.get("reactions")
    if not isinstance(reactions, list) or not reactions:
        return
    target_msg = str(m.get("id") or "")
    if not target_msg:
        return
    em = event_map_get(chat_id, target_msg)
    if not em:
        return  # target not relayed (yet) -> nothing to annotate
    target_event, _sender, _bh = em
    room = room_for_chat(chat_id)
    if not room:
        return
    for rx in reactions:
        if not isinstance(rx, dict):
            continue
        rxn_id = str(rx.get("id") or "")
        if not rxn_id or rxn_in_seen(chat_id, target_msg, rxn_id):
            continue
        raw_key = rx.get("reactionKey")
        key = engine_reaction_display(raw_key, bool(rx.get("emoji")))
        if not key:
            continue
        # R-5: suppress our own outbound reactions echoing back.
        if ledger_has(chat_id + "\0" + target_msg + "\0" + key):
            rxn_in_add(chat_id, target_msg, rxn_id, "")
            continue
        pid = str(rx.get("participantID") or "")
        ghost = ensure_ghost(pid, pid) if pid else BOT_ID
        ghost_join(ghost, room)
        try:
            r = send_reaction(room, ghost, target_event, key,
                              txn=component_txn(room, target_msg, "reaction:" + rxn_id))
            if not (r or {}).get("event_id"):
                raise ValueError("missing reaction receipt")
            rxn_in_add(chat_id, target_msg, rxn_id, r["event_id"])
            log.info("inbound reaction chat=%s", sha(chat_id)[:8])
        except Exception:
            log.info("inbound reaction failed chat=%s", sha(chat_id)[:8])
            raise

def reconcile_edit(chat_id, m):
    """Relay an inbound edit to Matrix m.replace on the original event (R-1)."""
    if not m.get("editedTimestamp") and not m.get("editHistory"):
        return
    target_msg = str(m.get("id") or "")
    if not target_msg:
        return
    em = event_map_get(chat_id, target_msg)
    if not em:
        return
    target_event, sender, prev_hash = em
    text = clean_text(m.get("text") or "")
    if not text:
        return
    if sha(text) == (prev_hash or ""):
        return  # unchanged since last relay
    from_me = bool(m.get("isSender"))
    if from_me and ledger_has(chat_id + "\0" + text):
        # our own outbound edit echoing back; record new hash, do not repost
        event_map_put(chat_id, target_msg, target_event, sender, sha(text))
        return
    room = room_for_chat(chat_id)
    if not room:
        return
    # Same from_me treatment as _relay_message: body unmutated, and the trusted
    # com.jkali.from_me marker on the m.replace content so the edited own-message
    # renders right-aligned as "You". from_me here comes from the engine's
    # isSender and the stored sender is BOT_ID (@imessagebot) for own messages, so
    # the marker is never emitted on a ghost-authored edit (M-22 intact).
    from_me_extra = {"com.jkali.from_me": True} if from_me else None
    try:
        result = send_replace(room, sender, target_event, text, from_me_extra,
                              txn=component_txn(room, target_msg, "edit:" + sha(text)))
        if not (result or {}).get("event_id"):
            raise ValueError("missing edit receipt")
        event_map_put(chat_id, target_msg, target_event, sender, sha(text))
        log.info("inbound edit chat=%s", sha(chat_id)[:8])
    except Exception:
        log.info("inbound edit failed chat=%s", sha(chat_id)[:8])
        raise

def poll_loop():
    while True:
        try:
            poll_once()
        except Exception as e:
            log.info("poll error: %s", type(e).__name__)
        time.sleep(int(CFG.get("poll_interval", 3)))

# ---------------------------------------------------------------- mgmt room (B-2)
def ensure_mgmt_room():
    mid = meta_get("mgmt_room")
    if mid:
        return mid
    try:
        r = mx("POST", "/_matrix/client/v3/createRoom", {
            "name": "iMessage Commands",
            "preset": "private_chat", "visibility": "private", "is_direct": True,
            "invite": [USER_ID],
            # Marker only (B-2). NEVER uk.half-shot.bridge -> never a portal.
            "initial_state": [
                {"type": MGMT_MARKER_TYPE, "state_key": MGMT_MARKER_KEY,
                 "content": {"bridge": "imessage", "role": "management"}},
            ],
        }, user=BOT_ID)
        rid = r["room_id"]
        meta_set("mgmt_room", rid)   # persisted id is the ONLY dispatch key (B-2)
        log.info("mgmt room created")
        return rid
    except Exception:
        log.info("mgmt room create failed")
        return None

# ---------------------------------------------------------------- command limiter (B-1)
_open_hist = []
_open_url_last = {}
_open_lock = threading.Lock()

def cmd_open_ok(url):
    """B-1: <=1 open/10s, <=5/min, <=20/hour; dedup identical URL within 60s."""
    now = time.time()
    with _open_lock:
        _open_hist[:] = [t for t in _open_hist if now - t < 3600]
        if len([t for t in _open_hist if now - t < 10]) >= 1:
            return False
        if len([t for t in _open_hist if now - t < 60]) >= 5:
            return False
        if len(_open_hist) >= 20:
            return False
        if now - _open_url_last.get(url, 0) < 60:
            return False
        _open_hist.append(now)
        _open_url_last[url] = now
        return True

def cmd_open(grant):
    url = GRANT_PANE_URL.get(grant)      # B-7: NO default; missing key = no open
    if not url:
        return False
    if not cmd_open_ok(url):
        log.info("open rate-capped")
        return False
    try:                                  # B-7: failure non-fatal, non-retried
        subprocess.run(["/usr/bin/open", url], capture_output=True, timeout=10, shell=False)
        log.info("open pane invoked")
        return True
    except Exception:
        log.info("open failed (non-fatal)")
        return False

# ---------------------------------------------------------------- probes (B-5/B-6)
def probe_grants():
    """Read-only probes (B-5): current-user + chats only. Anything not probeable
    without a side effect is 'unknown' (B-6: never echoes the Apple ID)."""
    try:
        cu = cli_json("current-user")
        fulldisk = "ok" if isinstance(cu, dict) and cu.get("id") else "no"
    except Exception:
        fulldisk = "no"
    contacts = "unknown"
    try:
        ch = cli_json("chats")
        for c in (ch.get("items", []) if isinstance(ch, dict) else []):
            for p in (c.get("participants") or {}).get("items", []):
                if p.get("isSelf"):
                    continue
                nm = p.get("name") or p.get("displayName") or p.get("fullName")
                if isinstance(nm, str) and nm.strip():
                    contacts = "ok"
                    break
            if contacts == "ok":
                break
    except Exception:
        contacts = "unknown"
    return [
        ("Messages Data (Full Disk Access)", "fulldisk", fulldisk),
        ("Contacts", "contacts", contacts),
        ("Accessibility", "accessibility", "unknown"),
        ("Automation", "automation", "unknown"),
    ]

def _status_text(grants):
    mark = {"ok": "[ok]", "no": "[--]", "unknown": "[??]"}
    lines = ["iMessage bridge - permission status:"]
    for label, key, st in grants:
        suffix = ""
        if st == "unknown":
            suffix = (" (unknown - grant it if sending fails)"
                      if key in ("accessibility", "automation") else " (unknown)")
        lines.append("%s %s%s" % (mark.get(st, "[??]"), label, suffix))
    return "\n".join(lines)

def cmd_status(room_id):
    try:
        status = delivery_status()
        summary = "\nDelivery journal: " + json.dumps(status, sort_keys=True)
        send_text(room_id, BOT_ID, _status_text(probe_grants()) + summary)
    except Exception:
        log.info("status reply failed")

def cmd_setup(room_id):
    grants = probe_grants()
    text = _status_text(grants)
    # Only auto-open a pane for a grant we can PROVE is missing ("no"). Contacts /
    # Accessibility / Automation probe as "unknown" (they can't be checked without
    # a side effect), and "unknown" is not "missing": auto-opening the Contacts
    # pane just because it sorts first among the unknowns was the reported
    # "it opens into Contacts" bug when Full Disk Access is already granted.
    # Unknowns stay listed with "grant it if sending fails" guidance instead.
    first_missing = next(((label, key) for label, key, st in grants if st == "no"), None)
    if first_missing:
        label, key = first_missing
        cmd_open(key)                    # opens ONE pane (the first PROVEN-missing grant)
        text += "\nOpening System Settings for: %s." % label
    text += ("\nManual path: System Settings > Privacy & Security, "
             "then the relevant pane (Accessibility / Contacts / Automation / Full Disk Access).")
    try:
        send_text(room_id, BOT_ID, text)
    except Exception:
        log.info("setup reply failed")

def cmd_help(room_id):
    try:
        send_text(room_id, BOT_ID, "Commands: status, setup, help, start-chat")
    except Exception:
        log.info("help reply failed")

def _startchat_reject(room_id, why, handle_hash=""):
    # SC-P6: generic validation error; SC-5: log outcome + sha8(handle) only.
    try:
        send_text(room_id, BOT_ID, "Invalid number or email.")
    except Exception:
        log.info("start-chat reject reply failed")
    log.info("start-chat rejected reason=%s handle=%s", why, handle_hash)

def cmd_start_chat(room_id, rest):
    # SC-P3: split on the FIRST pipe only; no pipe => reject, zero engine call.
    handle_part, sep, message = rest.partition("|")
    if sep == "":
        return _startchat_reject(room_id, "no-sep")
    handle = handle_part.strip()
    # SC-P3: first message is untrusted text — clean_text (strips control/bidi/
    # zero-width, clamps MAX_TEXT) then strip; empty => reject, zero engine call.
    message = clean_text(message).strip()
    if not message:
        return _startchat_reject(room_id, "empty-msg", sha(handle)[:8])
    # SC-P2: strict phone/email validation, THEN explicit leading-dash guard
    # (an email local-part like --message@x.com would otherwise look option-like).
    if not (PHONE_RE.match(handle) or EMAIL_RE.match(handle)):
        return _startchat_reject(room_id, "bad-handle", sha(handle)[:8])
    if handle.startswith("-"):
        return _startchat_reject(room_id, "leading-dash", sha(handle)[:8])
    # SC-P5: persistent global + per-handle caps (bounds a forged-event fan-out).
    ok, hour_ct, day_ct = startchat_gate(handle)
    if not ok:
        outbound_set("retryable", "startchat_rate_limited")
        try:
            send_text(room_id, BOT_ID, "rate limited, try later.")
        except Exception:
            log.info("start-chat ratelimit reply failed")
        log.info("start-chat rate-capped handle=%s hour=%d day=%d",
                 sha(handle)[:8], hour_ct, day_ct)
        return
    # SC-4: handle is a separate positional; message is one "--message=" token.
    result = dispatch_native(engine_create_chat, handle, message)
    try:
        # SC-P6: clean_text the WHOLE reply (handle may carry admitted bidi).
        # Report success/failure per engine result; never leak engine stderr.
        if result:
            reply = clean_text("Started chat with " + handle + ".")
        else:
            reply = clean_text("Couldn't start that chat (it may already "
                               "exist, or the number/email isn't reachable).")
        send_text(room_id, BOT_ID, reply)
    except Exception:
        log.info("start-chat reply failed")
    # SC-5: never log handle/message text — sha8 + outcome + counts only.
    log.info("start-chat done handle=%s ok=%s hour=%d day=%d",
             sha(handle)[:8], result, hour_ct, day_ct)

def handle_command(ev, room_id, content):
    # B-2: dispatch ONLY in the persisted mgmt room (never by member count).
    if room_id != meta_get("mgmt_room"):
        return
    # SC-P1/B-3: the msgtype / no-relates_to / 120s-freshness gate runs BEFORE
    # dispatch for ALL commands (plain text, relation-free, fresh).
    if content.get("msgtype") != "m.text":
        return
    if content.get("m.relates_to") is not None:
        return
    body = content.get("body")
    if not isinstance(body, str):
        return
    # B-1: drop commands whose origin_server_ts is >120s old (replay defense).
    ots = ev.get("origin_server_ts")
    if not isinstance(ots, (int, float)) or (time.time() * 1000 - ots) > 120000:
        log.info("stale command dropped")
        return
    # SC-P1: do NOT lowercase the whole body or startswith-match. Split into the
    # first whitespace-delimited word (lowercased) and the untrusted remainder
    # (NEVER lowercased, NEVER logged).
    raw = body.strip()
    parts = raw.split(None, 1)
    if not parts:
        return
    word = parts[0].lower()
    rest = parts[1] if len(parts) == 2 else ""
    if word in ("status", "setup", "help") and rest == "":
        log.info("command %s", word)          # SC-P1: log only `word`, never `rest`
        if word == "status":
            cmd_status(room_id)
        elif word == "setup":
            cmd_setup(room_id)
        else:
            cmd_help(room_id)
    elif word == "start-chat" and rest != "":
        log.info("command %s", word)
        cmd_start_chat(room_id, rest)
    # else: unknown word, or an argless start-chat, or "status foo" -> ignore
    # (zero engine call, nothing logged from the body).

# ---------------------------------------------------------------- durable outbound receipts
TERMINAL_OUTCOMES = {"confirmed", "refused", "ambiguous"}


def delivery_status():
    with DBLOCK:
        pending = dict(DB.execute("SELECT status,COUNT(*) FROM inbound_pending GROUP BY status"))
        outbound = dict(DB.execute("SELECT state,COUNT(*) FROM outbound_event GROUP BY state"))
        refused = DB.execute("SELECT COUNT(*) FROM inbound_component WHERE status LIKE 'refused%' ").fetchone()[0]
    return {"inbound": pending, "inbound_refused_components": refused, "outbound": outbound,
            "confirmed_means": "engine acceptance; not recipient delivery"}


def outbound_get(event_id):
    with DBLOCK:
        return DB.execute("SELECT state,reason FROM outbound_event WHERE event_id=?", (event_id,)).fetchone()


def outbound_set(state, reason, engine_id=None):
    event_id = getattr(_EVENT_CONTEXT, "event_id", None)
    if event_id is None:
        return
    with DBLOCK:
        DB.execute("UPDATE outbound_event SET state=?,reason=?,engine_id=?,updated=? WHERE event_id=?",
                   (state, reason, engine_id, time.time(), event_id))
        DB.commit()


def dispatch_native(function, *args):
    # This commit is the boundary after which recovery must assume a send may
    # have happened. No body is stored; Synapse retains retryable payloads.
    outbound_set("dispatching", "native_dispatch")
    try:
        result = function(*args)
    except Exception:
        outbound_set("ambiguous", "dispatch_exception")
        raise
    if isinstance(result, EngineOutcome):
        outcome = result
    else:
        # Compatibility for explicit injected transports returning booleans.
        outcome = EngineOutcome("confirmed" if result else "retryable",
                                "engine_accepted" if result else "pre_dispatch_failure")
    outbound_set(outcome.state, outcome.reason, outcome.engine_id)
    return bool(outcome)


def process_outbound_event(ev):
    """True permits txn ACK; retryable events stay with the homeserver for replay."""
    if not isinstance(ev, dict):
        return True
    event_id = ev.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return True  # malformed events must never invoke the CLI
    if not EVENT_LOCK.acquire(blocking=False):
        return False  # serialize native dispatch, including concurrent txn POSTs
    try:
        previous = outbound_get(event_id)
        if previous and previous[0] in TERMINAL_OUTCOMES:
            return True
        if previous and previous[0] == "dispatching":
            # Normally only possible after a persistence error in this process.
            with DBLOCK:
                DB.execute("UPDATE outbound_event SET state='ambiguous',reason='unresolved_dispatch' WHERE event_id=?", (event_id,))
                DB.commit()
            return True
        with DBLOCK:
            DB.execute("INSERT OR REPLACE INTO outbound_event VALUES (?,?,?,?,?,?)",
                       (event_id, str(ev.get("room_id") or ""), "processing", time.time(), "received", None))
            DB.commit()
        _EVENT_CONTEXT.event_id = event_id
        try:
            handle_event(ev)
            state = outbound_get(event_id)[0]
            if state == "processing":
                outbound_set("refused", "ignored_or_invalid")
        except Exception as e:
            previous = outbound_get(event_id)
            if previous[0] not in TERMINAL_OUTCOMES:
                outbound_set("ambiguous" if previous[0] == "dispatching" else "retryable",
                             "handler_" + type(e).__name__)
            log.info("event pending id=%s error=%s", sha(event_id)[:8], type(e).__name__)
        return outbound_get(event_id)[0] in TERMINAL_OUTCOMES
    finally:
        _EVENT_CONTEXT.event_id = None
        EVENT_LOCK.release()


# ---------------------------------------------------------------- transactions
def handle_event(ev):
    if ev.get("sender") != USER_ID:           # M-1: exact allowlist, ALL types
        return
    t = ev.get("type")
    if t == "m.reaction":
        return handle_out_reaction(ev)
    if t == "m.room.redaction":
        return handle_out_redaction(ev)
    if t != "m.room.message":
        return
    room_id = ev.get("room_id") or ""
    chat_id = chat_for_room(room_id)          # M-2 / B-2: portal branch FIRST
    content = ev.get("content") or {}
    if chat_id is None:
        return handle_command(ev, room_id, content)   # mgmt room XOR portal
    relates = content.get("m.relates_to")
    if isinstance(relates, dict) and relates.get("rel_type") == "m.replace":
        return handle_out_edit(ev, room_id, chat_id, content, relates)
    body = content.get("body")
    if not isinstance(body, str) or not body:
        return
    body = body[:MAX_TEXT]
    if not rate_ok(chat_id):                  # M-8 cap
        log.info("rate-capped chat=%s", sha(chat_id)[:8])
        outbound_set("retryable", "rate_limited")
        return
    msgtype = content.get("msgtype")
    if msgtype in ("m.image", "m.file", "m.video", "m.audio"):
        try:
            tmp = download_mxc_to_tmp(content.get("url"), body)
        except AttachmentRefused:
            outbound_set("refused", "attachment_too_large")
            return
        if not tmp:
            log.info("outbound attachment rejected chat=%s", sha(chat_id)[:8])
            outbound_set("refused" if not MXC_RE.match(str(content.get("url") or "")) else "retryable", "attachment_unavailable")
            return
        try:
            ok = dispatch_native(engine_send_file, chat_id, tmp)
            log.info("outbound file chat=%s ok=%s", sha(chat_id)[:8], ok)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    elif msgtype == "m.text":
        ok = dispatch_native(engine_send, chat_id, body)
        if ok:
            ledger_add(chat_id + "\0" + body)
        log.info("outbound text chat=%s ok=%s bytes=%d", sha(chat_id)[:8], ok, len(body))

def _resolve_target(room_id, chat_id, target_event):
    """R-1: resolve a Matrix target event back to (chat,msg) and assert it lives
    in THIS room's chat. Returns the engine msg_id, or None to DROP."""
    if not target_event:
        return None
    row = event_map_by_event(target_event)
    if not row:
        return None                           # no cross-portal / unknown target
    tgt_chat, tgt_msg = row
    if tgt_chat != chat_id or chat_for_room(room_id) != tgt_chat:
        log.info("cross-room target dropped chat=%s", sha(chat_id)[:8])
        return None
    return tgt_msg

def handle_out_reaction(ev):
    content = ev.get("content") or {}
    relates = content.get("m.relates_to")
    if not isinstance(relates, dict) or relates.get("rel_type") != "m.annotation":
        return
    room_id = ev.get("room_id") or ""
    chat_id = chat_for_room(room_id)
    if chat_id is None:                       # reactions only in portals
        return
    target_event = relates.get("event_id")
    msg_id = _resolve_target(room_id, chat_id, target_event)   # R-1
    if not msg_id:
        return
    engine_key = matrix_reaction_to_engine_key(relates.get("key"))   # R-2
    if not engine_key:
        return
    if not rate_ok(chat_id):                  # R-3
        log.info("rate-capped chat=%s", sha(chat_id)[:8])
        outbound_set("retryable", "rate_limited")
        return
    display = RXN_KEY_EMOJI.get(engine_key, engine_key)
    ok = dispatch_native(engine_react, msg_id, engine_key)
    if ok:
        ledger_add(chat_id + "\0" + msg_id + "\0" + display)
        ev_id = ev.get("event_id")
        if ev_id:                             # R-6: remember mapped reaction event
            rxn_out_add(ev_id, chat_id, msg_id, engine_key)
    log.info("outbound reaction chat=%s ok=%s", sha(chat_id)[:8], ok)

def handle_out_edit(ev, room_id, chat_id, content, relates):
    target_event = relates.get("event_id")
    msg_id = _resolve_target(room_id, chat_id, target_event)   # R-1
    if not msg_id:
        return
    new_content = content.get("m.new_content") or {}
    text = new_content.get("body")
    if not isinstance(text, str) or not text:
        return
    text = text[:MAX_TEXT]
    if not rate_ok(chat_id):                  # R-3
        log.info("rate-capped chat=%s", sha(chat_id)[:8])
        outbound_set("retryable", "rate_limited")
        return
    ok = dispatch_native(engine_edit, msg_id, text)
    if ok:
        ledger_add(chat_id + "\0" + text)
    log.info("outbound edit chat=%s ok=%s bytes=%d", sha(chat_id)[:8], ok, len(text))

def handle_out_redaction(ev):
    # R-6: ONLY reaction events the daemon itself mapped, same room, reach the
    # engine (unreact). A redaction of a message event never does.
    redacts = ev.get("redacts") or (ev.get("content") or {}).get("redacts")
    if not redacts:
        return
    row = rxn_out_get(redacts)
    if not row:
        return                                # not a daemon-mapped reaction -> ignore
    chat_id, msg_id, engine_key = row
    room_id = ev.get("room_id") or ""
    if chat_for_room(room_id) != chat_id:     # same-room assertion
        log.info("cross-room redaction dropped chat=%s", sha(chat_id)[:8])
        return
    if not rate_ok(chat_id):                  # R-3
        log.info("rate-capped chat=%s", sha(chat_id)[:8])
        outbound_set("retryable", "rate_limited")
        return
    ok = dispatch_native(engine_unreact, msg_id, engine_key)
    log.info("outbound unreact chat=%s ok=%s", sha(chat_id)[:8], ok)

class Handler(BaseHTTPRequestHandler):
    server_version = "imsgd"
    sys_version = ""
    protocol_version = "HTTP/1.1"   # Twisted needs Content-Length'd responses

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def _reply(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):        # M-6: strip query strings
        log.info("http %s", self.path.split("?", 1)[0])

    def _deny(self, code):
        self._reply(code, b'{"errcode":"DENIED"}')

    def _authed(self):                        # M-7
        auth = self.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else \
            urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("access_token", [""])[0]
        return hmac.compare_digest(tok, HS_TOKEN)

    def _host_ok(self):                       # M-6 host allowlist
        return self.headers.get("Host", "") in HOST_ALLOW

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path == "/health" and self._host_ok():
            self._reply(200, json.dumps(delivery_status()).encode())
        else:
            self._deny(404)

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._host_ok():
            return self._deny(403)
        te = self.headers.get("Transfer-Encoding", "")
        if te and te.lower() != "chunked":
            return self._deny(400)
        if te:
            log.info("chunked transaction (Synapse uses TE:chunked)")
        if not (path.startswith("/_matrix/app/v1/transactions/") or path.startswith("/transactions/")):
            return self._deny(404)
        if not self._authed():
            return self._deny(403)
        txn_id = path.rsplit("/", 1)[-1][:255]
        if te:
            chunks = []
            total = 0
            try:
                while True:
                    size_line = self.rfile.readline(32).strip()
                    size = int(size_line.split(b";")[0], 16)
                    if size < 0:
                        return self._deny(400)
                    if size == 0:
                        self.rfile.readline(4)
                        break
                    total += size
                    if total > MAX_BODY:
                        return self._deny(413)
                    chunk = self.rfile.read(size)
                    if len(chunk) != size or self.rfile.readline(4) != b"\r\n":
                        return self._deny(400)
                    chunks.append(chunk)
            except (ValueError, OSError):
                return self._deny(400)
            body = b"".join(chunks)
        else:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._deny(400)
            if length < 0:
                return self._deny(400)
            if length > MAX_BODY:
                return self._deny(413)
            body = self.rfile.read(length)
            if len(body) != length:
                return self._deny(400)
        if txn_seen(txn_id):                   # M-8 idempotency (set-based)
            self._reply(200, b"{}")
            return
        try:
            events = json.loads(body.decode("utf-8", "replace")).get("events", [])
            if not isinstance(events, list):
                return self._deny(400)
        except Exception:
            return self._deny(400)
        complete = True
        for ev in events:
            try:
                if not process_outbound_event(ev):
                    complete = False
            except Exception as e:
                complete = False
                log.info("event error: %s", type(e).__name__)
        if not complete:
            self._reply(503, b'{"errcode":"M_UNAVAILABLE","error":"Retryable events remain"}')
            return
        txn_mark(txn_id)
        self._reply(200, b"{}")

    do_POST = do_PUT


def main():
    os.umask(0o077)
    initialize()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)  # M-3 fixed port
    except OSError as e:
        log.info("bind failed (%s) — failing closed", e)
        sys.exit(1)
    srv.daemon_threads = True
    srv.timeout = 30
    ensure_user(BOT_ID.split(":", 1)[0].lstrip("@"))
    set_displayname(BOT_ID, "iMessage bridge bot")
    try:
        ensure_mgmt_room()                    # B-2: pinned mgmt room + marker
    except Exception:
        log.info("mgmt room init skipped")
    if os.path.exists(CLI):
        threading.Thread(target=poll_loop, daemon=True).start()
    else:
        log.info("engine binary missing; transactions-only mode")
    log.info("listening on 127.0.0.1:%d", PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
