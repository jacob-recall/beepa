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
CFG = json.load(open(os.path.join(BASE, "daemon.json")))
PORT = int(CFG["port"])
HS = CFG["hs_url"]
DOMAIN = CFG["domain"]
USER_ID = CFG["user_id"]          # the ONLY allowed command sender (M-1)
BOT_ID = CFG["bot_id"]
AS_TOKEN = CFG["as_token"]
HS_TOKEN = CFG["hs_token"]
CLI = CFG["cli_path"]
MAX_BODY = 8 * 1024 * 1024        # 8 MiB transaction cap (M-6)
MAX_TEXT = CFG.get("max_body_kb", 64) * 1024
HOST_ALLOW = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"host.docker.internal:{PORT}"}
ATTACH_PREFIXES = [os.path.realpath(p) for p in CFG["attachment_allow_prefixes"]]
MXC_RE = re.compile(r"^mxc://localhost/([A-Za-z0-9_-]{1,255})$")
# SC-P2: start-chat handle validation. PHONE_RE uses literal ASCII [0-9] (+
# re.ASCII) to match the hub's JS \d exactly; EMAIL_RE mirrors the hub regex.
# EMAIL_RE may admit bidi/zero-width (SC-P6 clean_text()s the echoed reply).
PHONE_RE = re.compile(r"^\+[1-9][0-9]{6,14}$", re.ASCII)
EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{2,24}$")
RATE_PER_MIN = int(CFG.get("rate_per_chat_per_min", 30))
BACKFILL_COUNT = max(0, min(int(CFG.get("backfill_count", 25)), 50))  # R-4 hard cap 50
BACKFILL_GLOBAL_CAP = 500          # R-4: per-daemon-start total backfilled posts
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

def clean_text(s, limit=MAX_TEXT):
    if not isinstance(s, str):
        return ""
    return _CTRL.sub("", s)[:limit]

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
DB = sqlite3.connect(os.path.join(BASE, "state.db"), check_same_thread=False)
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
DB.commit()
os.chmod(os.path.join(BASE, "state.db"), 0o600)  # M-10, regardless of creator umask
DBLOCK = threading.Lock()

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
        DB.execute("DELETE FROM txns WHERE ts < ?", (time.time() - 3600,))
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
    with _rate_lock:
        hist = [t for t in _rate.get(chat_id, []) if now - t < 60]
        if hist and now - hist[-1] < 1.0:
            _rate[chat_id] = hist
            return False
        if len(hist) >= RATE_PER_MIN:
            _rate[chat_id] = hist
            return False
        hist.append(now)
        _rate[chat_id] = hist
        return True

# ---------------------------------------------------------------- matrix api
def mx(method, path, body=None, user=None, raw=False, content_type=None):
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

def send_text(room_id, sender, text, extra=None):
    rid = urllib.parse.quote(room_id, safe="")
    txn = "imsgd" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    content = {"msgtype": "m.text", "body": text}
    if extra:
        content.update(extra)
    return mx("PUT", f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}",
              content, user=sender)

def send_reaction(room_id, sender, target_event, key):
    rid = urllib.parse.quote(room_id, safe="")
    txn = "imsgd" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    return mx("PUT", f"/_matrix/client/v3/rooms/{rid}/send/m.reaction/{txn}",
              {"m.relates_to": {"rel_type": "m.annotation", "event_id": target_event, "key": key}},
              user=sender)

def send_replace(room_id, sender, target_event, text, extra=None):
    rid = urllib.parse.quote(room_id, safe="")
    txn = "imsgd" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    content = {"msgtype": "m.text", "body": "* " + text,
               "m.new_content": {"msgtype": "m.text", "body": text},
               "m.relates_to": {"rel_type": "m.replace", "event_id": target_event}}
    if extra:
        content.update(extra)
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
    p = subprocess.run(argv, capture_output=True, timeout=timeout, shell=False)
    if p.returncode != 0:
        raise RuntimeError("engine rc=%d" % p.returncode)
    if len(p.stdout) > 32 * 1024 * 1024:
        raise RuntimeError("engine output too large")
    return _extract_json(p.stdout.decode("utf-8", "replace"))

def engine_send(chat_id, text):
    p = subprocess.run([CLI, "--no-events", "send", chat_id, text],
                       capture_output=True, timeout=60, shell=False)
    return p.returncode == 0

def engine_send_file(chat_id, path):
    p = subprocess.run([CLI, "--no-events", "send-file", chat_id, path],
                       capture_output=True, timeout=120, shell=False)
    return p.returncode == 0

def engine_react(msg_id, reaction):
    p = subprocess.run([CLI, "--no-events", "react", msg_id, reaction],
                       capture_output=True, timeout=60, shell=False)
    return p.returncode == 0

def engine_unreact(msg_id, reaction):
    p = subprocess.run([CLI, "--no-events", "unreact", msg_id, reaction],
                       capture_output=True, timeout=60, shell=False)
    return p.returncode == 0

def engine_edit(msg_id, text):
    p = subprocess.run([CLI, "--no-events", "edit", msg_id, text],
                       capture_output=True, timeout=60, shell=False)
    return p.returncode == 0

def engine_create_chat(handle, message):
    """SC-4: create a new iMessage chat. `handle` is a separate positional
    (already regex-validated + leading-dash-guarded); `message` is ONE
    "--message="-prefixed token so a leading-dash message cannot bind as a flag.
    No "--" terminator (it would break --message). list-argv, shell=False (M-20)."""
    p = subprocess.run([CLI, "--no-events", "create-chat", handle, "--message=" + message],
                       capture_output=True, timeout=60, shell=False)
    return p.returncode == 0

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

def download_mxc_to_tmp(mxc, filename_hint):
    m = MXC_RE.match(str(mxc or ""))
    if not m:
        return None
    ext = ""
    hint = os.path.splitext(str(filename_hint or ""))[1]
    if re.fullmatch(r"\.[A-Za-z0-9]{1,8}", hint or ""):
        ext = hint.lower()
    fd, tmp = tempfile.mkstemp(dir=os.path.join(BASE, "tmp"), suffix=ext)
    try:
        url = f"{HS}/_matrix/client/v1/media/download/localhost/{urllib.parse.quote(m.group(1))}"
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
                    raise RuntimeError("attachment too large")
                os.write(fd, chunk)
        return tmp
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    finally:
        os.close(fd)

# ---------------------------------------------------------------- portals
SPACE_KEY = "space_id"

def ensure_space():
    sid = meta_get(SPACE_KEY)
    if sid:
        return sid
    r = mx("POST", "/_matrix/client/v3/createRoom", {
        "name": "iMessage", "preset": "private_chat", "visibility": "private",
        "creation_content": {"type": "m.space"},
        "invite": [USER_ID],
    }, user=BOT_ID)
    meta_set(SPACE_KEY, r["room_id"])
    log.info("space created")
    return r["room_id"]

def ensure_portal(chat_id, chat_name, is_group):
    room = room_for_chat(chat_id)
    if room:
        return room
    space = ensure_space()
    r = mx("POST", "/_matrix/client/v3/createRoom", {
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
    }, user=BOT_ID)
    room = r["room_id"]
    sp = urllib.parse.quote(space, safe="")
    mx("PUT", f"/_matrix/client/v3/rooms/{sp}/state/m.space.child/{urllib.parse.quote(room, safe='')}",
       {"via": [DOMAIN]}, user=BOT_ID)
    map_add(chat_id, room)
    log.info("portal created chat=%s", sha(chat_id)[:8])
    try:
        maybe_backfill(chat_id, room, chat_name, is_group)
    except Exception:
        log.info("backfill error chat=%s", sha(chat_id)[:8])
    return room

# ---------------------------------------------------------------- backfill (R-4)
_backfill_posted = [0]              # per-daemon-start global post counter
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
        return
    flag = "backfill:%s:%s" % (chat_id, room)
    if meta_get(flag) == "1":
        return
    meta_set(flag, "1")                 # written BEFORE posting (R-4), idempotent
    if not _portal_backfill_ok():
        log.info("backfill rate-capped chat=%s", sha(chat_id)[:8])
        return
    try:
        msgs = cli_json("messages", chat_id).get("items", [])
    except Exception:
        return
    if not isinstance(msgs, list):
        return
    for m in msgs[-BACKFILL_COUNT:]:   # oldest-first within the window
        mid = str(m.get("id") or "")
        if not mid:
            continue
        with DBLOCK:
            dup = DB.execute("SELECT 1 FROM seen_msg WHERE chat_id=? AND msg_id=?",
                             (chat_id, mid)).fetchone()
        if dup:
            continue                    # never double-post (R-4 idempotency)
        with _backfill_lock:
            if _backfill_posted[0] >= BACKFILL_GLOBAL_CAP:
                log.info("backfill global cap reached")
                return
            _backfill_posted[0] += 1
        with DBLOCK:
            DB.execute("INSERT OR IGNORE INTO seen_msg (chat_id, msg_id) VALUES (?,?)", (chat_id, mid))
            DB.commit()
        try:
            _relay_message(chat_id, room, chat_name, is_group, m)
            reconcile_reactions(chat_id, m)
        except Exception:
            log.info("backfill post failed chat=%s", sha(chat_id)[:8])
    log.info("backfill done chat=%s", sha(chat_id)[:8])

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
def poll_once():
    data = cli_json("chats")
    items = data.get("items", []) if isinstance(data, dict) else []
    for c in items if isinstance(items, list) else []:
        chat_id = str(c.get("id") or "")
        if not chat_id:
            continue
        marker = str(c.get("timestamp") or "")
        key = "cursor:" + chat_id
        if marker and meta_get(key) == marker:
            continue
        handle_chat_delta(c, chat_id)
        if marker:
            meta_set(key, marker)

def chat_display_name(chat):
    title = chat.get("title") or ""
    if title:
        return title
    parts = (chat.get("participants") or {}).get("items", [])
    others = [p.get("phoneNumber") or p.get("email") or p.get("id", "")
              for p in parts if not p.get("isSelf")]
    return ", ".join(x for x in others if x) or "Note to self"

def handle_chat_delta(chat, chat_id):
    # `chats` embeds only the latest message; fetch the full recent list so
    # bursts between polls are not dropped.
    try:
        msgs = cli_json("messages", chat_id).get("items", [])
    except Exception:
        msgs = (chat.get("messages") or {}).get("items", [])
    if not isinstance(msgs, list):
        return
    name = chat_display_name(chat)
    is_group = (chat.get("type") or "single") != "single"
    for m in msgs[-25:]:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        with DBLOCK:
            dup = DB.execute("SELECT 1 FROM seen_msg WHERE chat_id=? AND msg_id=?",
                             (chat_id, mid)).fetchone()
        if not dup:
            with DBLOCK:
                DB.execute("INSERT OR IGNORE INTO seen_msg (chat_id, msg_id) VALUES (?,?)", (chat_id, mid))
                DB.commit()
            deliver_inbound(chat_id, name, is_group, m)
        # Reactions/edits can land on already-seen messages, so reconcile every
        # windowed message regardless of the seen-set (dedup is internal).
        try:
            reconcile_edit(chat_id, m)
            reconcile_reactions(chat_id, m)
        except Exception:
            log.info("reconcile error chat=%s", sha(chat_id)[:8])

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
    """Post one inbound message's attachments + text into `room`, recording the
    (chat,msg)->event_id map (R-1 prereq) for the primary event."""
    text = clean_text(m.get("text") or "")
    from_me = bool(m.get("isSender"))
    sender_handle = str(m.get("senderID") or chat_id.rsplit(";", 1)[-1])
    mid = str(m.get("id") or "")
    # from_me is the daemon's trustworthy own-message signal: derived from the
    # ENGINE's isSender (a remote contact cannot make the engine report their
    # message as from-me) and posted ONLY as @imessagebot (BOT_ID). The hub UI
    # renders it right-aligned as "You" via the com.jkali.from_me content field.
    # NEVER stamp this field on a ghost-authored (received) message.
    from_me_extra = {"com.jkali.from_me": True} if from_me else None
    if from_me:
        sender = BOT_ID
        disp = text
    else:
        sender = ensure_ghost(sender_handle, m.get("senderName") or sender_handle)
        ghost_join(sender, room)
        disp = text
    primary_event = None
    for att in (m.get("attachments") or []):
        path = decode_asset_url(att.get("srcURL") or "")
        if not path or not safe_engine_path(path):
            log.info("attachment path rejected chat=%s", sha(chat_id)[:8])
            continue
        rp = os.path.realpath(path)
        mime = mimetypes.guess_type(rp)[0] or "application/octet-stream"
        try:
            mxc = upload_media(rp, mime)
            msgtype = "m.image" if mime.startswith("image/") else "m.file"
            rid = urllib.parse.quote(room, safe="")
            txn = "imsgd" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
            content = {"msgtype": msgtype, "body": clean_name(os.path.basename(rp)), "url": mxc,
                       "info": {"mimetype": mime, "size": os.path.getsize(rp)}}
            if from_me:
                content["com.jkali.from_me"] = True
            r = mx("PUT", f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}",
                   content, user=sender)
            primary_event = (r or {}).get("event_id") or primary_event
        except Exception:
            log.info("attachment relay failed chat=%s", sha(chat_id)[:8])
    if disp:
        try:
            r = send_text(room, sender, disp, from_me_extra)
            primary_event = (r or {}).get("event_id") or primary_event
            log.info("inbound relayed chat=%s bytes=%d", sha(chat_id)[:8], len(disp))
        except Exception:
            log.info("inbound relay failed chat=%s", sha(chat_id)[:8])
    if mid and primary_event:
        # body_hash tracks the engine text (== disp; the body is no longer
        # mutated) so inbound-edit detection compares like with like.
        event_map_put(chat_id, mid, primary_event, sender, sha(text))
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
            r = send_reaction(room, ghost, target_event, key)
            rxn_in_add(chat_id, target_msg, rxn_id, (r or {}).get("event_id") or "")
            log.info("inbound reaction chat=%s", sha(chat_id)[:8])
        except Exception:
            log.info("inbound reaction failed chat=%s", sha(chat_id)[:8])

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
        send_replace(room, sender, target_event, text, from_me_extra)
        event_map_put(chat_id, target_msg, target_event, sender, sha(text))
        log.info("inbound edit chat=%s", sha(chat_id)[:8])
    except Exception:
        log.info("inbound edit failed chat=%s", sha(chat_id)[:8])

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
        send_text(room_id, BOT_ID, _status_text(probe_grants()))
    except Exception:
        log.info("status reply failed")

def cmd_setup(room_id):
    grants = probe_grants()
    text = _status_text(grants)
    first_missing = next(((label, key) for label, key, st in grants if st != "ok"), None)
    if first_missing:
        label, key = first_missing
        cmd_open(key)                    # opens ONE pane (the first missing grant)
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
        try:
            send_text(room_id, BOT_ID, "rate limited, try later.")
        except Exception:
            log.info("start-chat ratelimit reply failed")
        log.info("start-chat rate-capped handle=%s hour=%d day=%d",
                 sha(handle)[:8], hour_ct, day_ct)
        return
    # SC-4: handle is a separate positional; message is one "--message=" token.
    result = engine_create_chat(handle, message)
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
        return
    msgtype = content.get("msgtype")
    if msgtype in ("m.image", "m.file", "m.video", "m.audio"):
        tmp = download_mxc_to_tmp(content.get("url"), body)
        if not tmp:
            log.info("outbound attachment rejected chat=%s", sha(chat_id)[:8])
            return
        try:
            ok = engine_send_file(chat_id, tmp)
            log.info("outbound file chat=%s ok=%s", sha(chat_id)[:8], ok)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    elif msgtype == "m.text":
        ledger_add(chat_id + "\0" + body)     # R-5: chat-scoped; M-13 hash only
        ok = engine_send(chat_id, body)
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
        return
    display = RXN_KEY_EMOJI.get(engine_key, engine_key)
    ledger_add(chat_id + "\0" + msg_id + "\0" + display)   # R-5 echo suppress
    ok = engine_react(msg_id, engine_key)
    if ok:
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
        return
    ledger_add(chat_id + "\0" + text)         # R-5 echo suppress
    ok = engine_edit(msg_id, text)
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
        return
    ok = engine_unreact(msg_id, engine_key)
    log.info("outbound unreact chat=%s ok=%s", sha(chat_id)[:8], ok)

class Handler(BaseHTTPRequestHandler):
    server_version = "imsgd"
    sys_version = ""
    protocol_version = "HTTP/1.1"   # Twisted needs Content-Length'd responses

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
            self._reply(200, b"OK")
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
                    if size == 0:
                        self.rfile.readline(4)
                        break
                    total += size
                    if total > MAX_BODY:
                        return self._deny(413)
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline(4)  # trailing CRLF
            except (ValueError, OSError):
                return self._deny(400)
            body = b"".join(chunks)
        else:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._deny(400)
            if length > MAX_BODY:
                return self._deny(413)
            body = self.rfile.read(length)
        if txn_seen(txn_id):                   # M-8 idempotency (set-based)
            self._reply(200, b"{}")
            return
        try:
            events = json.loads(body.decode("utf-8", "replace")).get("events", [])
        except Exception:
            return self._deny(400)
        for ev in events if isinstance(events, list) else []:
            try:
                handle_event(ev)
            except Exception as e:
                log.info("event error: %s", type(e).__name__)
        txn_mark(txn_id)
        self._reply(200, b"{}")

    do_POST = do_PUT


def main():
    os.umask(0o077)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)  # M-3 fixed port
    except OSError as e:
        log.info("bind failed (%s) — failing closed", e)
        sys.exit(1)
    srv.daemon_threads = True
    srv.timeout = 30
    ensure_user("imessagebot")
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
