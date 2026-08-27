#!/usr/bin/env python3
"""Integration harness for the uplink one-way sync (PLAN-MASTER-SYNC §13, P2.4/P2.5).

Drives the eight edge-case scenarios end-to-end against TWO real running
homeservers:

  * TEST-USER hub  — an ISOLATED throwaway Synapse (server_name: localhost) on
    127.0.0.1:8028, brought up by tests/integration/docker-compose.test.yml
    (compose project matrix-synctest). This models ONE teammate's LOCAL stack.
    It is fully separate from the live matrix-wa stack (8008/8009/8010) — this
    harness NEVER touches matrix-wa.
  * MASTER hub     — the already-running matrix-master Synapse on 127.0.0.1:8018.
    The uplink writes here as @alice:master (a scoped teammate account). We
    assert on master state.

The harness registers a local test user + a synthetic local "contact" per
scenario (so from_me is real: test-user-authored => com.jkali.from_me true,
contact-authored => false); builds bridge SOURCE spaces named exactly like the
real bridges ("iMessage", "LinkedIn") with synthetic DM rooms linked under them
via m.space.child (so the uplink's sources_from_sync derives the source the same
way shared/ui/sources.js does); sets consent via account-data
(com.jkali.share_policy + per-room com.jkali.share_override); runs the real
agents/uplink/uplink.py as a subprocess; and asserts on the MASTER homeserver.

Pure python3 stdlib. Run:  python3 tests/integration/harness.py
Requires the two stacks up (see the module docstring of the compose file and
master/docker-compose.master.yml). Prints a JSON summary of all scenarios.
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
UPLINK_PY = os.path.join(REPO, "agents", "uplink", "uplink.py")

TEST_HS = "http://127.0.0.1:8028"
MASTER_HS = "http://127.0.0.1:8018"
LOCAL_SERVER = "localhost"
SHARED_SECRET = "synctest7vLFrjd2mgbLeXhlbrTufckryNEgivyCqioYmTwsTlVQ6rkXd8"

# Scratch dir for uplink state DBs + logs (outside the repo).
STATE_DIR = os.environ.get(
    "SYNCTEST_STATE_DIR",
    "/private/tmp/claude-501/-Users-jkali-work-pm-mng/"
    "736e7f1b-4fc4-40e9-a74a-c28f77e7200f/scratchpad/uplink-state")


# ------------------------------------------------------------------ master creds
def load_master_tokens():
    path = os.path.join(REPO, "master", "tokens.local")
    with open(path) as f:
        txt = f.read()
    out = {}
    for m in re.finditer(r"^(\w+)='([^']*)'", txt, re.M):
        out[m.group(1)] = m.group(2)
    return out


MASTER = load_master_tokens()
MASTER_ALICE_USER = MASTER["MASTER_ALICE_USER"]
MASTER_ALICE_TOKEN = MASTER["MASTER_ALICE_TOKEN"]
MASTER_BOB_TOKEN = MASTER["MASTER_BOB_TOKEN"]
MASTER_MANAGER_USER = MASTER["MASTER_MANAGER_USER"]
MASTER_MANAGER_TOKEN = MASTER["MASTER_MANAGER_TOKEN"]
MASTER_SPACE_ALICE = MASTER["MASTER_SPACE_ALICE"]
MASTER_SPACE_BOB = MASTER["MASTER_SPACE_BOB"]


# ------------------------------------------------------------------ matrix client
class MxError(Exception):
    def __init__(self, code, body):
        super().__init__("HTTP %s: %s" % (code, body))
        self.code = code
        self.body = body


def mx(base, token, method, path, body=None, query=None, timeout=60):
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, method=method, data=data)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise MxError(e.code, e.read().decode(errors="replace"))


def local(token, method, path, body=None, query=None, timeout=60):
    return mx(TEST_HS, token, method, path, body, query, timeout)


def master(token, method, path, body=None, query=None, timeout=60):
    return mx(MASTER_HS, token, method, path, body, query, timeout)


# ------------------------------------------------------------------ registration
def register_user(localpart, password="synctest-pw-123", admin=False):
    """Create a local user via the Synapse shared-secret admin endpoint."""
    nonce = mx(TEST_HS, None, "GET", "/_synapse/admin/v1/register")["nonce"]
    mac = hmac.new(SHARED_SECRET.encode(), digestmod=hashlib.sha1)
    mac.update(nonce.encode())
    mac.update(b"\x00" + localpart.encode())
    mac.update(b"\x00" + password.encode())
    mac.update(b"\x00" + (b"admin" if admin else b"notadmin"))
    body = {"nonce": nonce, "username": localpart, "password": password,
            "admin": admin, "mac": mac.hexdigest()}
    try:
        res = mx(TEST_HS, None, "POST", "/_synapse/admin/v1/register", body)
        return res["user_id"], res["access_token"]
    except MxError as e:
        if "M_USER_IN_USE" in e.body:
            # already registered — log in for a token
            r = mx(TEST_HS, None, "POST", "/_matrix/client/v3/login",
                   {"type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": localpart},
                    "password": password})
            return r["user_id"], r["access_token"]
        raise


# ------------------------------------------------------------------ local world
_uid_counter = [0]


def uniq(prefix):
    _uid_counter[0] += 1
    return "%s%d_%d" % (prefix, int(time.time()) % 100000, _uid_counter[0])


def create_space(token, name):
    r = local(token, "POST", "/_matrix/client/v3/createRoom",
              {"name": name, "preset": "private_chat",
               "creation_content": {"type": "m.space"}})
    return r["room_id"]


def create_dm(token, name, invite):
    r = local(token, "POST", "/_matrix/client/v3/createRoom",
              {"name": name, "preset": "private_chat", "invite": invite})
    return r["room_id"]


def link_child(space_token, space_id, child_id):
    local(space_token, "PUT",
          "/_matrix/client/v3/rooms/" + urllib.parse.quote(space_id, safe="")
          + "/state/m.space.child/" + urllib.parse.quote(child_id, safe=""),
          {"via": [LOCAL_SERVER]})
    # Seal the space with a trailing timeline event. The uplink's reconcile does
    # an initial /sync with timeline{limit:1}; on an initial sync the `state`
    # block holds state up to the START of the returned timeline, so if an
    # m.space.child were the newest event it would fall into the timeline and be
    # invisible to sources_from_sync (which reads room.state.events only). A real
    # teammate hub's bridge space always has later activity; we model that by
    # posting one trailing message so every child link is in the state block.
    local(space_token, "PUT",
          "/_matrix/client/v3/rooms/" + urllib.parse.quote(space_id, safe="")
          + "/send/m.room.message/" + uniq("seal"),
          {"msgtype": "m.notice", "body": "space activity"})


def join_room(token, room_id):
    local(token, "POST",
          "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="")
          + "/join", {})


def post_msg(token, room_id, body):
    tx = uniq("m")
    r = local(token, "PUT",
              "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="")
              + "/send/m.room.message/" + tx,
              {"msgtype": "m.text", "body": body})
    return r["event_id"]


def set_policy(token, user_id, policy):
    local(token, "PUT",
          "/_matrix/client/v3/user/" + urllib.parse.quote(user_id, safe="")
          + "/account_data/com.jkali.share_policy", policy)


def set_override(token, user_id, room_id, state):
    """state in {'share','private'} or None to clear (inherit).

    Room account-data lives under the user path
    /user/{userId}/rooms/{roomId}/account_data/{type} (this is exactly what
    /sync surfaces as rooms.join[room].account_data, which the uplink reads)."""
    content = {"state": state} if state else {}
    local(token, "PUT",
          "/_matrix/client/v3/user/" + urllib.parse.quote(user_id, safe="")
          + "/rooms/" + urllib.parse.quote(room_id, safe="")
          + "/account_data/com.jkali.share_override", content)


# ------------------------------------------------------------------ uplink runner
def start_uplink(e, extra_env=None):
    """Launch the real uplink daemon for a scenario env bundle `e`."""
    db_path, log_path = e["db_path"], e["log_path"]
    env = dict(os.environ)
    env.update({
        "LOCAL_HS_URL": TEST_HS,
        "LOCAL_USER": e["tuser_id"],
        "LOCAL_TOKEN": e["tuser_tok"],
        "MASTER_HS_URL": MASTER_HS,
        "MASTER_USER": MASTER_ALICE_USER,
        "MASTER_TOKEN": MASTER_ALICE_TOKEN,
        "MANAGER_MXID": MASTER_MANAGER_USER,
        "MASTER_SPACE": MASTER_SPACE_ALICE,
        "UPLINK_DB": db_path,
        "UPLINK_SYNC_TIMEOUT": "2000",
        "UPLINK_BACKFILL": "500",
        "UPLINK_LOG_LEVEL": "INFO",
    })
    if extra_env:
        env.update(extra_env)
    logf = open(log_path, "ab")
    proc = subprocess.Popen([sys.executable, UPLINK_PY], env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    proc._logf = logf  # keep handle
    return proc


def stop_uplink(proc):
    if proc is None:
        return
    try:
        proc.send_signal(signal.SIGINT)  # run() catches KeyboardInterrupt -> clean exit
        proc.wait(timeout=8)
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    try:
        proc._logf.close()
    except Exception:
        pass


# ------------------------------------------------------------------ state db read
def mirror_of(db_path, local_room_id):
    if not os.path.exists(db_path):
        return None
    db = sqlite3.connect(db_path)
    try:
        r = db.execute("SELECT master_room_id, source, last_synced_pos "
                       "FROM mirror_rooms WHERE local_room_id=?",
                       (local_room_id,)).fetchone()
        return r
    finally:
        db.close()


def meta_get(db_path, key):
    if not os.path.exists(db_path):
        return None
    db = sqlite3.connect(db_path)
    try:
        r = db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return r[0] if r else None
    finally:
        db.close()


# ------------------------------------------------------------------ master asserts
def master_messages(room_id, token=MASTER_ALICE_TOKEN):
    """All m.room.message events in a master room, oldest->newest by origin_ts.

    Returns list of dicts: {body, from_me, origin_ts, source, redacted, event_id}.
    Skips redacted events' content (body None) so revocation/redaction is visible.
    """
    r = master(token, "GET",
               "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="")
               + "/messages", query={"dir": "b", "limit": "200"})
    out = []
    for e in (r.get("chunk") or []):
        if e.get("type") != "m.room.message":
            continue
        c = e.get("content") or {}
        redacted = not c  # redacted events arrive with empty content
        out.append({
            "event_id": e.get("event_id"),
            "body": c.get("body"),
            "from_me": c.get("com.jkali.from_me"),
            "origin_ts": c.get("com.jkali.origin_ts"),
            "source": c.get("com.jkali.source"),
            "origin_sender": c.get("com.jkali.origin_sender"),
            "redacted": redacted,
        })
    out.sort(key=lambda m: (m["origin_ts"] if m["origin_ts"] is not None else 0))
    return out


def master_source_tag(room_id, token=MASTER_ALICE_TOKEN):
    try:
        r = master(token, "GET",
                   "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="")
                   + "/state/com.jkali.source/")
        return r.get("source")
    except MxError:
        return None


def master_room_alive(room_id, token=MASTER_ALICE_TOKEN):
    """True if the room still exists AND alice is still joined (mirror present)."""
    try:
        master(token, "GET",
               "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="")
               + "/state/m.room.create/")
        return True
    except MxError:
        return False


def manager_sees_room(room_id):
    """True if @manager is still joined to the mirror room (not revoked)."""
    try:
        r = master(MASTER_MANAGER_TOKEN, "GET",
                   "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="")
                   + "/joined_members")
        return MASTER_MANAGER_USER in (r.get("joined") or {})
    except MxError:
        return False


def space_children(space_id, token):
    """state_keys of m.space.child in a master space (the mirror rooms grouped under it)."""
    r = master(token, "GET",
               "/_matrix/client/v3/rooms/" + urllib.parse.quote(space_id, safe="")
               + "/state")
    kids = []
    for e in r:
        if e.get("type") == "m.space.child" and (e.get("content") or {}).get("via"):
            kids.append(e.get("state_key"))
    return kids


# ------------------------------------------------------------------ poll helper
def wait_until(fn, timeout=45, interval=1.0, desc=""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:
            last = "exc:" + str(e)
        time.sleep(interval)
    raise TimeoutError("wait_until timed out (%s); last=%r" % (desc, last))


def tail_log(log_path, n=25):
    try:
        with open(log_path) as f:
            return "".join(f.readlines()[-n:])
    except Exception:
        return ""


# ------------------------------------------------------------------ per-scenario setup
def fresh_env(tag):
    """Register a test user + contact, wipe any prior uplink db for this tag."""
    tuser_lp = uniq("tuser")
    contact_lp = uniq("contact")
    tuser_id, tuser_tok = register_user(tuser_lp)
    contact_id, contact_tok = register_user(contact_lp)
    os.makedirs(STATE_DIR, exist_ok=True)
    db_path = os.path.join(STATE_DIR, "state_%s.db" % tag)
    log_path = os.path.join(STATE_DIR, "uplink_%s.log" % tag)
    for p in (db_path, log_path):
        if os.path.exists(p):
            os.remove(p)
    return {
        "tuser_id": tuser_id, "tuser_tok": tuser_tok,
        "contact_id": contact_id, "contact_tok": contact_tok,
        "db_path": db_path, "log_path": log_path,
    }


def make_convo(e, space_id, name):
    """Create a DM under a source space; contact joins; return room_id."""
    rid = create_dm(e["tuser_tok"], name, invite=[e["contact_id"]])
    join_room(e["contact_tok"], rid)
    link_child(e["tuser_tok"], space_id, rid)
    return rid


# =====================================================================
# Scenarios. Each returns (pass_bool, evidence_str).
# =====================================================================

def scenario_1_share_one():
    """Share one conversation -> mirror appears under space:alice with full
    history, chronological order (by origin_ts), correct from_me alignment, and
    the com.jkali.source tag."""
    e = fresh_env("s1")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    rid = make_convo(e, imsg_space, "Dana Lewis")
    # Alternate authorship so from_me is exercised both ways, known order.
    seq = [
        ("them", "hey are we still on for friday"),
        ("me", "yes 2pm works"),
        ("them", "great see you then"),
        ("me", "ill bring the deck"),
        ("them", "perfect"),
    ]
    for who, body in seq:
        tok = e["tuser_tok"] if who == "me" else e["contact_tok"]
        post_msg(tok, rid, body)
        time.sleep(0.05)  # ensure strictly increasing origin_server_ts
    # Explicitly share this one conversation.
    set_override(e["tuser_tok"], e["tuser_id"], rid, "share")

    proc = start_uplink(e)
    try:
        row = wait_until(lambda: mirror_of(e["db_path"], rid), timeout=45,
                         desc="mirror row for s1")
        master_room = row[0]
        msgs = wait_until(
            lambda: (lambda m: m if len(m) >= len(seq) else None)(master_messages(master_room)),
            timeout=45, desc="s1 messages")
    finally:
        stop_uplink(proc)

    ev = []
    ok = True
    # full history
    if len(msgs) != len(seq):
        ok = False
    ev.append("history=%d/%d" % (len(msgs), len(seq)))
    # chronological order by origin_ts matches authored order
    got_bodies = [m["body"] for m in msgs]
    want_bodies = [b for _, b in seq]
    if got_bodies != want_bodies:
        ok = False
    ev.append("order_ok=%s" % (got_bodies == want_bodies))
    ts = [m["origin_ts"] for m in msgs]
    mono = all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1)) and all(t is not None for t in ts)
    if not mono:
        ok = False
    ev.append("origin_ts_monotonic=%s" % mono)
    # from_me alignment
    got_fm = [bool(m["from_me"]) for m in msgs]
    want_fm = [who == "me" for who, _ in seq]
    if got_fm != want_fm:
        ok = False
    ev.append("from_me=%s want=%s" % (got_fm, want_fm))
    # source tag both on state and stamped on events
    tag = master_source_tag(master_room)
    if tag != "imessage":
        ok = False
    ev.append("source_state_tag=%s" % tag)
    ev_src = set(m["source"] for m in msgs)
    if ev_src != {"imessage"}:
        ok = False
    ev.append("event_source_tags=%s" % sorted(ev_src))
    ev.append("master_room=%s" % master_room)
    return ok, "; ".join(ev)


def scenario_2_new_message():
    """Post a new local message after mirroring -> appears on master within a
    sync cycle."""
    e = fresh_env("s2")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    rid = make_convo(e, imsg_space, "Priya N")
    post_msg(e["contact_tok"], rid, "initial history msg")
    set_override(e["tuser_tok"], e["tuser_id"], rid, "share")

    proc = start_uplink(e)
    try:
        row = wait_until(lambda: mirror_of(e["db_path"], rid), timeout=45,
                         desc="mirror row s2")
        master_room = row[0]
        wait_until(lambda: len(master_messages(master_room)) >= 1, timeout=45,
                   desc="s2 backfill")
        # Now post a brand-new live message.
        marker = "LIVE-" + uniq("tail")
        post_msg(e["tuser_tok"], rid, marker)
        arrived = wait_until(
            lambda: any(m["body"] == marker for m in master_messages(master_room)),
            timeout=45, desc="s2 live tail")
    finally:
        stop_uplink(proc)
    msgs = master_messages(master_room)
    live = [m for m in msgs if m["body"] == marker]
    ok = len(live) == 1 and bool(live[0]["from_me"]) is True
    ev = "live_marker_count=%d from_me=%s total=%d room=%s" % (
        len(live), live[0]["from_me"] if live else None, len(msgs), master_room)
    return ok, ev


def scenario_3_offline_catchup():
    """Stop the uplink, inject several new local messages, restart -> all
    injected messages arrive on master exactly once via watermark resume."""
    e = fresh_env("s3")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    rid = make_convo(e, imsg_space, "Sam Ortiz")
    post_msg(e["contact_tok"], rid, "s3 seed message")
    set_override(e["tuser_tok"], e["tuser_id"], rid, "share")

    proc = start_uplink(e)
    try:
        row = wait_until(lambda: mirror_of(e["db_path"], rid), timeout=45,
                         desc="mirror row s3")
        master_room = row[0]
        wait_until(lambda: len(master_messages(master_room)) >= 1, timeout=45,
                   desc="s3 backfill")
    finally:
        stop_uplink(proc)  # UPLINK OFFLINE
    base_count = len(master_messages(master_room))

    # Inject several messages while the uplink is DOWN.
    injected = ["s3-offline-%d-%s" % (i, uniq("x")) for i in range(5)]
    for i, body in enumerate(injected):
        tok = e["tuser_tok"] if i % 2 == 0 else e["contact_tok"]
        post_msg(tok, rid, body)
        time.sleep(0.05)
    # Confirm they did NOT reach master while the uplink was down.
    during = master_messages(master_room)
    leaked = [b for b in injected if any(m["body"] == b for m in during)]

    # Restart the uplink -> watermark resume delivers exactly the backlog.
    proc = start_uplink(e)
    try:
        wait_until(
            lambda: all(any(m["body"] == b for m in master_messages(master_room))
                        for b in injected),
            timeout=60, desc="s3 catch-up")
    finally:
        stop_uplink(proc)

    final = master_messages(master_room)
    counts = {b: sum(1 for m in final if m["body"] == b) for b in injected}
    dupes = {b: c for b, c in counts.items() if c != 1}
    ok = (not leaked) and (not dupes) and len(final) == base_count + len(injected)
    ev = ("leaked_while_down=%s all_once=%s final=%d expected=%d dup_or_gap=%s"
          % (leaked, not dupes, len(final), base_count + len(injected), dupes))
    return ok, ev


def scenario_4_master_offline():
    """docker stop the master synapse; post local messages; confirm the uplink
    buffers and does NOT advance its watermark; docker start it -> messages
    deliver, still exactly once."""
    e = fresh_env("s4")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    rid = make_convo(e, imsg_space, "Wei Chen")
    post_msg(e["contact_tok"], rid, "s4 seed")
    set_override(e["tuser_tok"], e["tuser_id"], rid, "share")

    proc = start_uplink(e)
    master_room = None
    try:
        row = wait_until(lambda: mirror_of(e["db_path"], rid), timeout=45,
                         desc="mirror row s4")
        master_room = row[0]
        wait_until(lambda: len(master_messages(master_room)) >= 1, timeout=45,
                   desc="s4 backfill")
        # let the tail settle and record the watermark
        time.sleep(4)
        wm_before = meta_get(e["db_path"], "sync_since")

        # MASTER GOES OFFLINE.
        docker(["stop", "matrix-master-synapse-1"])
        base_count_unknown = True  # can't read master while down

        # Post local messages while master is unreachable.
        buffered = ["s4-buffered-%d-%s" % (i, uniq("b")) for i in range(4)]
        for i, body in enumerate(buffered):
            tok = e["tuser_tok"] if i % 2 == 0 else e["contact_tok"]
            post_msg(tok, rid, body)
            time.sleep(0.05)
        # Give the uplink several loop cycles to try + fail + buffer.
        time.sleep(12)
        wm_during = meta_get(e["db_path"], "sync_since")
        log_snapshot = tail_log(e["log_path"], 40)
        buffered_flag = ("master unreachable" in log_snapshot.lower()
                         or "buffering" in log_snapshot.lower())

        # MASTER COMES BACK.
        docker(["start", "matrix-master-synapse-1"])
        wait_master_health()
        wait_until(
            lambda: all(any(m["body"] == b for m in master_messages(master_room))
                        for b in buffered),
            timeout=90, desc="s4 delivery after master back")
    finally:
        stop_uplink(proc)
        # Safety: make absolutely sure master is running for later scenarios.
        docker(["start", "matrix-master-synapse-1"], check=False)
        wait_master_health()

    final = master_messages(master_room)
    counts = {b: sum(1 for m in final if m["body"] == b) for b in buffered}
    dupes = {b: c for b, c in counts.items() if c != 1}
    # Watermark must NOT have advanced while master was unreachable (§7/§8.2).
    wm_frozen = (wm_before == wm_during)
    ok = buffered_flag and wm_frozen and (not dupes)
    ev = ("buffered_log=%s watermark_frozen=%s(before=%r during=%r) all_once=%s dupes=%s"
          % (buffered_flag, wm_frozen, wm_before, wm_during, not dupes, dupes))
    return ok, ev


def scenario_5_share_all_standing():
    """Per-source share-all for iMessage -> all current iMessage rooms mirror up
    AND a newly-created iMessage room auto-mirrors; a per-conversation private
    override on one iMessage room keeps it OUT."""
    e = fresh_env("s5")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    li_space = create_space(e["tuser_tok"], "LinkedIn")
    a = make_convo(e, imsg_space, "iMsg A")
    b = make_convo(e, imsg_space, "iMsg B (excluded)")
    li = make_convo(e, li_space, "LinkedIn C")
    for rid in (a, b, li):
        post_msg(e["contact_tok"], rid, "seed for " + rid[:8])
    # Standing policy: share ALL iMessage; LinkedIn stays inherit->private.
    set_policy(e["tuser_tok"], e["tuser_id"],
               {"global": "private", "sources": {"imessage": "share-all"}})
    # Per-conversation exclusion on one iMessage room.
    set_override(e["tuser_tok"], e["tuser_id"], b, "private")

    proc = start_uplink(e)
    try:
        # a mirrors (share-all), b excluded, li not iMessage -> not mirrored.
        wait_until(lambda: mirror_of(e["db_path"], a), timeout=45, desc="s5 A mirror")
        time.sleep(4)  # allow reconcile to consider b and li
        a_mirror = mirror_of(e["db_path"], a)
        b_mirror = mirror_of(e["db_path"], b)
        li_mirror = mirror_of(e["db_path"], li)

        # Now a NEW iMessage room arrives under the standing policy.
        c = make_convo(e, imsg_space, "iMsg D (new arrival)")
        post_msg(e["contact_tok"], c, "brand new imessage thread")
        c_mirror = wait_until(lambda: mirror_of(e["db_path"], c), timeout=45,
                              desc="s5 auto-share new room")
    finally:
        stop_uplink(proc)

    ok = (a_mirror is not None and b_mirror is None and li_mirror is None
          and c_mirror is not None)
    ev = ("A_mirrored=%s B_excluded=%s LinkedIn_not_shared=%s new_room_auto=%s"
          % (a_mirror is not None, b_mirror is None, li_mirror is None,
             c_mirror is not None))
    return ok, ev


def scenario_6_revoke_levels():
    """Revoke at each level (clear per-conversation share; set source
    private-all; set global private) -> the corresponding master mirror room(s)
    are removed from the manager's view."""
    e = fresh_env("s6")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    li_space = create_space(e["tuser_tok"], "LinkedIn")
    # r_conv: shared by explicit per-conversation override (revoke by clearing).
    r_conv = make_convo(e, imsg_space, "Revoke-by-conv")
    # r_src: shared by source share-all iMessage (revoke by source private-all).
    r_src = make_convo(e, imsg_space, "Revoke-by-source")
    # r_glob: LinkedIn room shared only via global share-all (revoke by global private).
    r_glob = make_convo(e, li_space, "Revoke-by-global")
    for rid in (r_conv, r_src, r_glob):
        post_msg(e["contact_tok"], rid, "seed " + rid[:8])
    # Start with everything shared: global share-all covers all; plus explicit
    # share on r_conv. (global share-all already shares r_src and r_glob too.)
    set_policy(e["tuser_tok"], e["tuser_id"],
               {"global": "share-all", "sources": {}})
    set_override(e["tuser_tok"], e["tuser_id"], r_conv, "share")

    proc = start_uplink(e)
    ev = []
    ok = True
    try:
        m_conv = wait_until(lambda: mirror_of(e["db_path"], r_conv), timeout=45, desc="s6 conv")
        m_src = wait_until(lambda: mirror_of(e["db_path"], r_src), timeout=45, desc="s6 src")
        m_glob = wait_until(lambda: mirror_of(e["db_path"], r_glob), timeout=45, desc="s6 glob")
        room_conv, room_src, room_glob = m_conv[0], m_src[0], m_glob[0]
        # All three are mirrored: each is a child of space:alice (the manager's
        # grouping) AND @alice (the uplink) is joined to write it.
        time.sleep(2)
        kids0 = space_children(MASTER_SPACE_ALICE, MASTER_ALICE_TOKEN)
        all_up = all(r in kids0 for r in (room_conv, room_src, room_glob))
        ev.append("initially_all_mirrored=%s" % all_up)
        ok = ok and all_up

        # Observable revocation signal (delete_mirror per uplink.py: clear the
        # m.space.child, kick @manager, then leave the room). The mirror is gone
        # from the manager's view when: (a) the local mapping is dropped, (b) the
        # master room is no longer a child of the manager's space:alice grouping,
        # and (c) the uplink account @alice has LEFT it (so it can no longer be
        # written or surfaced). A former member can still read the historical
        # create event via /state, so we use join membership, not room existence.
        def alice_joined(master_rid):
            try:
                jr = master(MASTER_ALICE_TOKEN, "GET",
                            "/_matrix/client/v3/joined_rooms")["joined_rooms"]
                return master_rid in jr
            except MxError:
                return False

        def revoked(local_rid, master_rid):
            return (mirror_of(e["db_path"], local_rid) is None
                    and master_rid not in space_children(MASTER_SPACE_ALICE, MASTER_ALICE_TOKEN)
                    and not alice_joined(master_rid))

        # (1) REVOKE per-conversation: set r_conv private (most-specific override
        # beats the global share-all that would otherwise keep it shared).
        set_override(e["tuser_tok"], e["tuser_id"], r_conv, "private")
        wait_until(lambda: mirror_of(e["db_path"], r_conv) is None, timeout=45,
                   desc="s6 conv revoked")
        conv_gone = revoked(r_conv, room_conv)
        ev.append("conv_revoked=%s" % conv_gone)
        ok = ok and conv_gone

        # (2) REVOKE per-source: set iMessage source to private-all (overrides
        # global share-all for r_src).
        set_policy(e["tuser_tok"], e["tuser_id"],
                   {"global": "share-all", "sources": {"imessage": "private-all"}})
        wait_until(lambda: mirror_of(e["db_path"], r_src) is None, timeout=45,
                   desc="s6 src revoked")
        src_gone = revoked(r_src, room_src)
        ev.append("source_revoked=%s" % src_gone)
        ok = ok and src_gone

        # (3) REVOKE global: set global to private. r_glob (LinkedIn, shared only
        # via global share-all) drops out.
        set_policy(e["tuser_tok"], e["tuser_id"],
                   {"global": "private", "sources": {"imessage": "private-all"}})
        wait_until(lambda: mirror_of(e["db_path"], r_glob) is None, timeout=45,
                   desc="s6 glob revoked")
        glob_gone = revoked(r_glob, room_glob)
        ev.append("global_revoked=%s" % glob_gone)
        ok = ok and glob_gone
    finally:
        stop_uplink(proc)
    return ok, "; ".join(ev)


def scenario_7_read_only():
    """@manager cannot send into a mirror room (expect 403); apps/master has no
    send path."""
    e = fresh_env("s7")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    rid = make_convo(e, imsg_space, "ReadOnly Target")
    post_msg(e["contact_tok"], rid, "seed ro")
    set_override(e["tuser_tok"], e["tuser_id"], rid, "share")

    proc = start_uplink(e)
    try:
        row = wait_until(lambda: mirror_of(e["db_path"], rid), timeout=45, desc="s7 mirror")
        master_room = row[0]
        wait_until(lambda: len(master_messages(master_room)) >= 1, timeout=45,
                   desc="s7 backfill")
    finally:
        stop_uplink(proc)

    ev = []
    ok = True
    # Manager joins (invited by the uplink) then attempts to send -> 403.
    try:
        master(MASTER_MANAGER_TOKEN, "POST",
               "/_matrix/client/v3/rooms/" + urllib.parse.quote(master_room, safe="")
               + "/join", {})
    except MxError as e2:
        ev.append("manager_join=%d" % e2.code)
    send_code = None
    try:
        master(MASTER_MANAGER_TOKEN, "PUT",
               "/_matrix/client/v3/rooms/" + urllib.parse.quote(master_room, safe="")
               + "/send/m.room.message/" + uniq("mgr"),
               {"msgtype": "m.text", "body": "manager trying to send"})
        send_code = 200  # should NOT happen
    except MxError as e2:
        send_code = e2.code
    if send_code != 403:
        ok = False
    ev.append("manager_send_status=%s(want 403)" % send_code)

    # Build-time: apps/master must contain no send path. Strip JS comments first
    # (the file documents the ABSENCE of a send path in comments, e.g.
    # "// PUT .../send/m.room.message ..."), then look for a real write call.
    mj = os.path.join(REPO, "apps", "master", "main.js")
    mh = os.path.join(REPO, "apps", "master", "index.html")
    src = open(mj).read()
    html = open(mh).read()
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)         # block comments
    code = re.sub(r"(?m)//.*$", "", code)                    # line comments
    has_send_put = bool(re.search(r"/send/m\.room\.message", code)) or bool(
        re.search(r"['\"]PUT['\"]\s*,\s*[^)]*?/send/", code))
    has_composer = bool(re.search(r"<textarea", html)) or bool(
        re.search(r'id="(composer|msg-input|send)"|class="[^"]*composer', html))
    if has_send_put or has_composer:
        ok = False
    ev.append("apps_master_send_put=%s composer=%s" % (has_send_put, has_composer))
    return ok, "; ".join(ev)


def scenario_8_cross_user_isolation():
    """The test user syncing as @alice cannot cause writes into space:bob."""
    e = fresh_env("s8")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    rid = make_convo(e, imsg_space, "Isolation Target")
    post_msg(e["contact_tok"], rid, "seed iso")
    set_override(e["tuser_tok"], e["tuser_id"], rid, "share")

    proc = start_uplink(e)
    try:
        row = wait_until(lambda: mirror_of(e["db_path"], rid), timeout=45, desc="s8 mirror")
        master_room = row[0]
        wait_until(lambda: len(master_messages(master_room)) >= 1, timeout=45,
                   desc="s8 backfill")
    finally:
        stop_uplink(proc)

    ev = []
    ok = True
    # 1. Alice's mirror landed under space:alice, NOT space:bob.
    alice_kids = space_children(MASTER_SPACE_ALICE, MASTER_ALICE_TOKEN)
    in_alice = master_room in alice_kids
    bob_kids = space_children(MASTER_SPACE_BOB, MASTER_BOB_TOKEN)
    in_bob = master_room in bob_kids
    if not in_alice or in_bob:
        ok = False
    ev.append("mirror_under_alice=%s under_bob=%s" % (in_alice, in_bob))

    # 2. Alice's token is refused when it tries to write into space:bob directly
    #    (add a child) — the scoped-account isolation boundary.
    child_code = None
    try:
        master(MASTER_ALICE_TOKEN, "PUT",
               "/_matrix/client/v3/rooms/" + urllib.parse.quote(MASTER_SPACE_BOB, safe="")
               + "/state/m.space.child/" + urllib.parse.quote(master_room, safe=""),
               {"via": ["master"]})
        child_code = 200  # must NOT happen
    except MxError as e2:
        child_code = e2.code
    if child_code == 200:
        ok = False
    ev.append("alice_write_space_bob=%s(want 4xx)" % child_code)

    # 3. Alice cannot even send a message into bob's space room.
    send_code = None
    try:
        master(MASTER_ALICE_TOKEN, "PUT",
               "/_matrix/client/v3/rooms/" + urllib.parse.quote(MASTER_SPACE_BOB, safe="")
               + "/send/m.room.message/" + uniq("iso"),
               {"msgtype": "m.text", "body": "alice into bob space"})
        send_code = 200
    except MxError as e2:
        send_code = e2.code
    if send_code == 200:
        ok = False
    ev.append("alice_send_space_bob=%s(want 4xx)" % send_code)
    return ok, "; ".join(ev)


# ------------------------------------------------------------------ docker helpers
def docker(args, check=True):
    env = dict(os.environ)
    env["PATH"] = "/Applications/Docker.app/Contents/Resources/bin:" + env.get("PATH", "")
    return subprocess.run(["docker"] + args, env=env, check=check,
                          capture_output=True, text=True)


def wait_master_health(timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(MASTER_HS + "/health", timeout=5) as r:
                if r.status == 200:
                    time.sleep(2)  # settle
                    return True
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError("master did not become healthy")


def live_stack_ok():
    """Confirm every live matrix-wa container is still Up (untouched)."""
    out = docker(["ps", "--format", "{{.Names}}\t{{.Status}}"]).stdout
    wa = [ln for ln in out.splitlines() if ln.startswith("matrix-wa-")]
    all_up = all("Up" in ln for ln in wa)
    return all_up, wa


# ------------------------------------------------------------------ main
SCENARIOS = [
    ("1_share_one_conversation", scenario_1_share_one),
    ("2_new_local_message", scenario_2_new_message),
    ("3_offline_online_catchup", scenario_3_offline_catchup),
    ("4_master_offline_buffer", scenario_4_master_offline),
    ("5_share_all_standing_policy", scenario_5_share_all_standing),
    ("6_revoke_each_level", scenario_6_revoke_levels),
    ("7_read_only_manager", scenario_7_read_only),
    ("8_cross_user_isolation", scenario_8_cross_user_isolation),
]


def main():
    only = sys.argv[1:] or None
    results = []
    for name, fn in SCENARIOS:
        if only and not any(name.startswith(o) or name == o or o in name for o in only):
            continue
        sys.stderr.write("\n=== running %s ===\n" % name)
        t0 = time.time()
        try:
            ok, ev = fn()
        except Exception as e:
            ok, ev = False, "EXCEPTION: %r" % e
        dt = time.time() - t0
        sys.stderr.write("--- %s: %s (%.1fs)\n    %s\n" %
                         (name, "PASS" if ok else "FAIL", dt, ev))
        results.append({"name": name, "pass": ok, "evidence": ev})

    up, wa = live_stack_ok()
    summary = {"scenarios": results,
               "live_stack_untouched": up,
               "live_stack": wa}
    print(json.dumps(summary, indent=2))
    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
