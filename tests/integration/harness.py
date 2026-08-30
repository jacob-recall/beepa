#!/usr/bin/env python3
"""Integration harness for the uplink one-way sync (PLAN-MASTER-SYNC §13, P2.4/P2.5).

Drives the twelve edge-case scenarios end-to-end against TWO real running
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
import base64
import glob
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

# Scenario 12 seeds contacts.db directly through the real store module (same
# way tests/unit/contacts_store.test.py imports it) rather than reimplementing
# its schema here.
sys.path.insert(0, os.path.join(REPO, "agents", "contacts"))
import contacts_store  # noqa: E402

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


# ------------------------------------------------------------------ media (v1.5)
# A real 1x1 PNG (~68 bytes). Uploaded to the TEST hub, posted as an m.image
# event, and expected to be re-uploaded verbatim to the MASTER media store.
TINY_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def upload_media(base, token, data, content_type, filename):
    """Upload raw bytes to a homeserver's media store; return the mxc URI."""
    url = base + "/_matrix/media/v3/upload?filename=" + urllib.parse.quote(filename)
    req = urllib.request.Request(url, method="POST", data=data)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["content_uri"]


def download_media(base, token, mxc):
    """Fetch bytes for an mxc via the authenticated client-v1 media endpoint."""
    m = re.match(r"^mxc://([^/]+)/(.+)$", mxc)
    server, mid = m.group(1), m.group(2)
    url = (base + "/_matrix/client/v1/media/download/"
           + urllib.parse.quote(server, safe="") + "/" + urllib.parse.quote(mid, safe=""))
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def post_media_event(token, room_id, content):
    """PUT an m.room.message with a caller-supplied media content dict."""
    tx = uniq("mm")
    r = local(token, "PUT",
              "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="")
              + "/send/m.room.message/" + tx, content)
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


def set_profiles(token, user_id, profiles):
    """Write com.jkali.contact_profiles (§12 phase 5 unified contacts).

    profiles is the full { "profiles": [ {id, displayName, roomIds, share} ] }
    document the uplink's read_profiles() consumes (parity with
    shared/model/contacts.js)."""
    local(token, "PUT",
          "/_matrix/client/v3/user/" + urllib.parse.quote(user_id, safe="")
          + "/account_data/com.jkali.contact_profiles", profiles)


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
            "msgtype": c.get("msgtype"),               # v1.5 media assertions
            "url": c.get("url"),
            "media_placeholder": c.get("com.jkali.media_placeholder"),
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


def master_profile_tag(room_id, token=MASTER_ALICE_TOKEN):
    """The com.jkali.profile state event stamped on a mirror room, or None.

    Returns the content dict {id, displayName} the uplink stamps when the room
    is a member of a SHARED contact profile (§12 phase 5)."""
    try:
        return master(token, "GET",
                      "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="")
                      + "/state/com.jkali.profile/")
    except MxError:
        return None


def master_contact_states(room_id, token=MASTER_ALICE_TOKEN):
    """{state_key: content} for every com.jkali.contact STATE event in a
    master contacts room (Task 6's per-handle mirror, §12 phase 6)."""
    r = master(token, "GET",
              "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="")
              + "/state")
    out = {}
    for e in r:
        if e.get("type") == "com.jkali.contact":
            out[e.get("state_key")] = e.get("content") or {}
    return out


def contact_state_key(source, network_id):
    """Byte-parity with uplink.py's _put_contact: state_key = sha1(source|network_id)."""
    return hashlib.sha1((source + "|" + network_id).encode("utf-8")).hexdigest()


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


# ------------------------------------------------- apps/master build-time scan
# The static half of scenario 7. apps/master's read-only guarantee is "absent
# code, not a hidden button", so it is asserted against the SOURCE of EVERY .js
# file in that directory (not main.js alone — the console gained invites.js, and
# a second file must not be able to smuggle in a write path):
#   * no /send/m.room.message anywhere;
#   * the only /send/<type> performed is com.jkali.proposal;
#   * every non-GET call goes to an ALLOWLISTED endpoint.
#
# The endpoint is never a single string literal in this codebase — every
# room-scoped call is 'prefix' + encodeURIComponent(id) + 'suffix' — so the
# allowlist is matched against the concatenation of the string LITERAL fragments
# found inside the path expression:
#   '/_matrix/client/v3/rooms/' + enc(id) + '/join'
#     -> '/_matrix/client/v3/rooms//join'
# Anything whose fragments match no entry (or whose method is not a literal)
# fails the scenario — the check is fail-closed by construction.
MASTER_WRITE_ALLOWLIST = {
    "POST": [
        r"^/_matrix/client/v3/login$",
        r"^/_matrix/client/v3/logout$",
        r"^/_matrix/client/v3/rooms//join$",     # auto-join of a gated invite
        r"^/admin/add-teammate$",                # ENROLL_BASE + '/admin/add-teammate'
    ],
    "PUT": [
        r"^/_matrix/client/v3/rooms//send/com\.jkali\.proposal/$",
    ],
    "DELETE": [],
}


def strip_js_comments(src):
    """Strip // and /* */ comments WITHOUT damaging string bodies.

    A naive line-comment regex mangles 'http://127.0.0.1:8018' into an
    unterminated quote, which would then swallow the rest of the file for any
    subsequent parse. This walks the source instead, tracking quotes.
    """
    out = []
    i, n, quote = 0, len(src), None
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"`":
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _balanced_args(src, open_idx):
    """Raw text between the parens opening at open_idx, quote/nesting aware."""
    depth, i, n, quote = 0, open_idx, len(src), None
    while i < n:
        c = src[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1:i]
        i += 1
    return None


def _split_top_args(argtext):
    """Split a call's argument text on TOP-LEVEL commas only."""
    parts, buf, depth, quote = [], [], 0, None
    for i, c in enumerate(argtext):
        if quote:
            buf.append(c)
            if c == quote and argtext[i - 1] != "\\":
                quote = None
            continue
        if c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(c)
    parts.append("".join(buf))
    return parts


def _literal_fragments(expr):
    """Concatenate every string literal inside one expression, in order."""
    found = re.findall(r"'([^'\\]*)'|\"([^\"\\]*)\"|`([^`\\$]*)`", expr)
    return "".join(a or b or c for a, b, c in found)


def js_write_calls(code):
    """[(METHOD, joined_literal_fragments, raw_expr)] for every non-GET call."""
    calls = []
    for m in re.finditer(r"\bapi\s*\(", code):
        args = _balanced_args(code, m.end() - 1)
        if args is None:
            continue
        parts = _split_top_args(args)
        if len(parts) < 2:
            continue
        lit = re.match(r"""^\s*['"]([A-Za-z]+)['"]\s*$""", parts[0])
        if not lit:
            calls.append(("<non-literal-method>", "", parts[0]))   # fail closed
            continue
        method = lit.group(1).upper()
        if method == "GET":
            continue
        calls.append((method, _literal_fragments(parts[1]), parts[1]))
    for m in re.finditer(r"\bfetch\s*\(", code):
        args = _balanced_args(code, m.end() - 1)
        if args is None:
            continue
        parts = _split_top_args(args)
        opts = parts[1] if len(parts) > 1 else ""
        lit = re.search(r"""method\s*:\s*['"]([A-Za-z]+)['"]""", opts)
        method = lit.group(1).upper() if lit else "GET"
        if method == "GET":
            continue
        calls.append((method, _literal_fragments(parts[0]), parts[0]))
    return calls


def scan_apps_master_write_surface():
    """(scanned_filenames, has_msg_send, send_types, violations) for apps/master."""
    files = sorted(glob.glob(os.path.join(REPO, "apps", "master", "*.js")))
    scanned, send_types, violations = [], set(), []
    has_msg_send = False
    for path in files:
        name = os.path.basename(path)
        scanned.append(name)
        with open(path) as fh:
            code = strip_js_comments(fh.read())
        if re.search(r"/send/m\.room\.message", code):
            has_msg_send = True
        send_types |= set(re.findall(r"/send/([A-Za-z0-9_.]+)", code))
        for method, joined, expr in js_write_calls(code):
            allowed = MASTER_WRITE_ALLOWLIST.get(method, [])
            if not any(re.match(p, joined) for p in allowed):
                violations.append("%s:%s %s" % (name, method, joined or expr.strip()[:60]))
    return scanned, has_msg_send, send_types, violations


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

    # Build-time invariant. V1 shipped "no composer at all". V2 intentionally adds
    # ONE write path — the compose-PROPOSAL panel — and the invite fix adds POST
    # /join, so the check is the precise HARD LIMIT instead of a blanket composer
    # ban: across EVERY .js file in apps/master/ there must be (a) NO
    # /send/m.room.message path anywhere, (b) the ONLY /send/<type> performed is
    # com.jkali.proposal (into a proposals room), and (c) every non-GET call must
    # match the write-surface allowlist above. Comments are stripped first (the
    # files document the ABSENCE of an m.room.message path in prose).
    scanned, has_msg_send, send_types, write_violations = scan_apps_master_write_surface()
    non_proposal_sends = send_types - {"com.jkali.proposal"}
    if not scanned or has_msg_send or non_proposal_sends or write_violations:
        ok = False
    ev.append("apps_master_scanned=%s msg_send=%s send_types=%s write_violations=%s"
              % (scanned, has_msg_send, sorted(send_types), write_violations))
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


def ensure_media_writable():
    """No-op: the media_store permission fix is now DURABLE and TRACKED.

    Both stacks used to mount media_store as a NAMED volume (created root-owned)
    while Synapse runs as UID 501, so the first upload 500'd with PermissionError
    on /data/media_store — patched at runtime here with a `chmod 0777`. The fix
    now lives in the tracked compose files: media_store is served from the
    ./synapse bind mount (host-owned by UID 501), so the container can write it
    with no runtime chmod. Kept as a no-op so callers/imports stay valid."""
    return


def scenario_9_media_reupload():
    """v1.5 media re-upload. Post a REAL image event to a shared room; the uplink
    must download it from LOCAL and re-upload it to the MASTER media store, posting
    the NEW master mxc (metadata preserved, com.jkali.media_placeholder false). A
    forced download failure (unresolvable mxc) and the size guard (tiny
    UPLINK_MEDIA_MAX) must each fall back to the v1 placeholder instead."""
    ensure_media_writable()
    e = fresh_env("s9")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    rid = make_convo(e, imsg_space, "Media Chat")
    post_msg(e["contact_tok"], rid, "check this photo")            # a text msg too
    # (a) a real image on the LOCAL hub, posted as m.image "from me".
    local_mxc = upload_media(TEST_HS, e["tuser_tok"], TINY_PNG, "image/png", "tiny.png")
    post_media_event(e["tuser_tok"], rid, {
        "msgtype": "m.image", "body": "tiny.png", "url": local_mxc,
        "info": {"mimetype": "image/png", "size": len(TINY_PNG), "w": 1, "h": 1},
    })
    # (b) forced-failure: an m.image whose mxc does not resolve on LOCAL -> the
    # download 404s -> the uplink must fall back to the placeholder (url stripped).
    bogus_mxc = "mxc://localhost/" + uniq("ghost")
    post_media_event(e["contact_tok"], rid, {
        "msgtype": "m.image", "body": "ghost.png", "url": bogus_mxc,
        "info": {"mimetype": "image/png", "size": 321},
    })
    set_override(e["tuser_tok"], e["tuser_id"], rid, "share")

    proc = start_uplink(e)
    try:
        row = wait_until(lambda: mirror_of(e["db_path"], rid), timeout=45, desc="s9 mirror")
        master_room = row[0]
        imgs = wait_until(
            lambda: (lambda i: i if len(i) >= 2 else None)(
                [m for m in master_messages(master_room) if m.get("msgtype") == "m.image"]),
            timeout=60, desc="s9 media mirrored")
    finally:
        stop_uplink(proc)

    ev = []
    ok = True
    real = [m for m in imgs if m.get("media_placeholder") is not True and m.get("url")]
    placeholder = [m for m in imgs if m.get("media_placeholder") is True]

    # 1. real image re-uploaded to a NEW master mxc (different from the local one).
    new_mxc = real[0]["url"] if real else None
    new_ok = bool(new_mxc and new_mxc.startswith("mxc://master/") and new_mxc != local_mxc)
    ok = ok and new_ok
    ev.append("new_master_mxc=%s local=%s different=%s" % (new_mxc, local_mxc, new_ok))

    # 2. bytes retrievable from the MASTER media API and byte-identical to the source.
    got = b""
    bytes_ok = False
    if new_mxc:
        try:
            got = download_media(MASTER_HS, MASTER_ALICE_TOKEN, new_mxc)
            bytes_ok = (got == TINY_PNG)
        except Exception as ex:
            ev.append("download_exc=%r" % ex)
    ok = ok and bytes_ok
    ev.append("bytes_retrievable=%s len=%d/%d" % (bytes_ok, len(got), len(TINY_PNG)))

    # 3. forced-failure (unresolvable mxc) fell back to placeholder, url stripped.
    ph_ok = len(placeholder) >= 1 and all(not p.get("url") for p in placeholder)
    ok = ok and ph_ok
    ev.append("forced_failure_placeholder=%s n=%d" % (ph_ok, len(placeholder)))
    ev.append("real_placeholder_flag=%s" % (real[0].get("media_placeholder") if real else None))

    # 4. size guard: a fresh room, tiny UPLINK_MEDIA_MAX -> the same real image
    #    is placeholdered (re-upload skipped above the cap).
    e2 = fresh_env("s9b")
    sp2 = create_space(e2["tuser_tok"], "iMessage")
    rid2 = make_convo(e2, sp2, "Media Guard")
    mxc2 = upload_media(TEST_HS, e2["tuser_tok"], TINY_PNG, "image/png", "tiny.png")
    post_media_event(e2["tuser_tok"], rid2, {
        "msgtype": "m.image", "body": "tiny.png", "url": mxc2,
        "info": {"mimetype": "image/png", "size": len(TINY_PNG)},
    })
    set_override(e2["tuser_tok"], e2["tuser_id"], rid2, "share")
    proc2 = start_uplink(e2, extra_env={"UPLINK_MEDIA_MAX": "8"})
    try:
        row2 = wait_until(lambda: mirror_of(e2["db_path"], rid2), timeout=45, desc="s9b mirror")
        mroom2 = row2[0]
        guard = wait_until(
            lambda: (lambda i: i if i else None)(
                [m for m in master_messages(mroom2) if m.get("msgtype") == "m.image"]),
            timeout=60, desc="s9b media mirrored")
    finally:
        stop_uplink(proc2)
    guard_ok = len(guard) >= 1 and all(
        g.get("media_placeholder") is True and not g.get("url") for g in guard)
    ok = ok and guard_ok
    ev.append("size_guard_placeholder=%s" % guard_ok)
    ev.append("master_room=%s" % master_room)
    return ok, "; ".join(ev)


def local_events_of_type(token, room_id, etype):
    """All timeline events of a given type in a LOCAL room, newest-first chunk."""
    r = local(token, "GET",
              "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="")
              + "/messages", query={"dir": "b", "limit": "200"})
    return [e for e in (r.get("chunk") or []) if e.get("type") == etype]


def local_state_present(token, room_id, etype):
    try:
        local(token, "GET",
              "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="")
              + "/state/" + urllib.parse.quote(etype, safe="") + "/")
        return True
    except MxError:
        return False


def scenario_10_proposal_down():
    """V2 proposal channel (master -> user). The manager writes a
    com.jkali.proposal into the teammate's DEDICATED master proposals room; the
    uplink pulls it DOWN into the teammate's DEDICATED LOCAL proposals room
    exactly once. Asserts: the proposal lands ONLY in the local proposals room
    (never in the mirror room nor the real conversation), idempotent dedup on a
    re-pull, the master-side power levels let the manager send com.jkali.proposal
    (200) but NOT m.room.message (403), and the local proposals room is a
    dedicated, marked, NOT-mirrored-up room."""
    e = fresh_env("s10")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    rid = make_convo(e, imsg_space, "Proposal Target")
    post_msg(e["contact_tok"], rid, "s10 seed message")
    set_override(e["tuser_tok"], e["tuser_id"], rid, "share")

    proc = start_uplink(e)
    ev = []
    ok = True
    try:
        # Mirror up + both proposals rooms created (recorded in uplink meta).
        row = wait_until(lambda: mirror_of(e["db_path"], rid), timeout=45, desc="s10 mirror")
        master_room = row[0]
        mpr = wait_until(lambda: meta_get(e["db_path"], "master_proposals_room"),
                         timeout=45, desc="s10 master proposals room")
        lpr = wait_until(lambda: meta_get(e["db_path"], "local_proposals_room"),
                         timeout=45, desc="s10 local proposals room")

        # The manager accepts the invite (as the master app auto-joins) so it can
        # write into the proposals room.
        master(MASTER_MANAGER_TOKEN, "POST",
               "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="") + "/join", {})

        # Power levels: the manager may send com.jkali.proposal but NOT
        # m.room.message (defense in depth, mirrors §8.3 read-only levels).
        msg_code = None
        try:
            master(MASTER_MANAGER_TOKEN, "PUT",
                   "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="")
                   + "/send/m.room.message/" + uniq("mgrmsg"),
                   {"msgtype": "m.text", "body": "manager should not be able to message here"})
            msg_code = 200  # must NOT happen
        except MxError as e2:
            msg_code = e2.code
        if msg_code != 403:
            ok = False
        ev.append("manager_msg_send=%s(want 403)" % msg_code)

        # The manager PROPOSES a message: target_room is the teammate's REAL local
        # conversation room (rid) — exactly what the master app reads from the
        # mirror room's com.jkali.mirror_of. body carries a unique marker.
        marker = "PROPOSAL-" + uniq("p")
        prop_content = {
            "target_room": rid,
            "body": marker,
            "created_by": MASTER_MANAGER_USER,
            "origin_ts": int(time.time() * 1000),
        }
        prop_code = None
        try:
            master(MASTER_MANAGER_TOKEN, "PUT",
                   "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="")
                   + "/send/com.jkali.proposal/" + uniq("prop"), prop_content)
            prop_code = 200
        except MxError as e2:
            prop_code = e2.code
        if prop_code != 200:
            ok = False
        ev.append("manager_proposal_send=%s(want 200)" % prop_code)

        # The uplink pulls it DOWN into the local proposals room exactly once.
        got = wait_until(
            lambda: [p for p in local_events_of_type(e["tuser_tok"], lpr, "com.jkali.proposal")
                     if (p.get("content") or {}).get("body") == marker] or None,
            timeout=45, desc="s10 proposal pulled down")
        # Let the loop run more cycles; the count must stay exactly one (dedup).
        time.sleep(6)
        final_local = [p for p in local_events_of_type(e["tuser_tok"], lpr, "com.jkali.proposal")
                       if (p.get("content") or {}).get("body") == marker]
        once = len(final_local) == 1
        if not once:
            ok = False
        ev.append("local_proposal_count=%d(want 1)" % len(final_local))

        # The pulled proposal carries the target_room + created_by, marked template
        # absent (not a template here).
        pc = (final_local[0].get("content") or {}) if final_local else {}
        target_ok = pc.get("target_room") == rid
        if not target_ok:
            ok = False
        ev.append("target_room_ok=%s created_by=%s" % (target_ok, pc.get("created_by")))

        # It landed ONLY in the local proposals room: NOT in the mirror room, and
        # NOT in the real conversation room.
        mirror_props = [m for m in master_messages(master_room)
                        if m.get("body") == marker]
        convo_props = [x for x in local_events_of_type(e["tuser_tok"], rid, "com.jkali.proposal")]
        convo_msgs = [m for m in local_events_of_type(e["tuser_tok"], rid, "m.room.message")
                      if (m.get("content") or {}).get("body") == marker]
        isolated = (not mirror_props) and (not convo_props) and (not convo_msgs)
        if not isolated:
            ok = False
        ev.append("isolated(not_in_mirror/convo)=%s" % isolated)

        # The local proposals room is dedicated + marked + NOT mirrored up.
        marked = local_state_present(e["tuser_tok"], lpr, "com.jkali.proposals")
        not_mirrored = mirror_of(e["db_path"], lpr) is None
        if not (marked and not_mirrored):
            ok = False
        ev.append("local_marked=%s not_mirrored_up=%s" % (marked, not_mirrored))

        # The master proposals room is grouped under space:alice (manager view)
        # and is itself marked com.jkali.proposals.
        under_space = mpr in space_children(MASTER_SPACE_ALICE, MASTER_ALICE_TOKEN)
        if not under_space:
            ok = False
        ev.append("master_proposals_under_space=%s" % under_space)
    finally:
        stop_uplink(proc)

    # Idempotency across a RESTART: bring the uplink back; the already-pulled
    # proposal must not be re-posted (proposal_map dedup survives restart).
    proc = start_uplink(e)
    try:
        time.sleep(8)
    finally:
        stop_uplink(proc)
    lpr2 = meta_get(e["db_path"], "local_proposals_room")
    after_restart = [p for p in local_events_of_type(e["tuser_tok"], lpr2, "com.jkali.proposal")
                     if (p.get("content") or {}).get("body")
                     and "PROPOSAL-" in (p.get("content") or {}).get("body", "")]
    restart_once = len(after_restart) == 1
    if not restart_once:
        ok = False
    ev.append("after_restart_count=%d(want 1)" % len(after_restart))
    ev.append("master_proposals_room=%s local_proposals_room=%s" % (mpr, lpr))
    return ok, "; ".join(ev)


def scenario_11_profile_span_platforms():
    """§12 phase 5 unified contacts: a contact profile spanning TWO platforms.

    Link an iMessage room + a LinkedIn room (+ a 2nd iMessage room) to ONE
    contact profile and set the profile to `share`. Global stays private and no
    per-source policy is set, so the ONLY thing that shares these rooms is the
    profile level (proving the new level works and beats the default). Expect:
      * BOTH the iMessage room and the LinkedIn room mirror up, and each mirror
        carries the SAME com.jkali.profile {id, displayName} — so the master can
        group this one person's threads across platforms;
      * a per-conversation `private` override on the 3rd member keeps it OUT
        (per-conv override still wins over the profile) and, being unmirrored,
        it never gets a profile stamp.
    """
    e = fresh_env("s11")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    li_space = create_space(e["tuser_tok"], "LinkedIn")
    im = make_convo(e, imsg_space, "Dana Lewis (iMessage)")
    li = make_convo(e, li_space, "Dana Lewis (LinkedIn)")
    excluded = make_convo(e, imsg_space, "Dana Lewis (old iMessage, excluded)")
    for rid in (im, li, excluded):
        post_msg(e["contact_tok"], rid, "seed for " + rid[:8])

    prof_id = "cp_dana_" + uniq("id")
    prof_name = "Dana Lewis"
    # One profile, three linked rooms across two sources, set to SHARE.
    set_profiles(e["tuser_tok"], e["tuser_id"], {"profiles": [{
        "id": prof_id, "displayName": prof_name,
        "roomIds": [im, li, excluded], "share": "share",
    }]})
    # Global + per-source stay at the safe default (nothing set) so the profile
    # is the sole cause of sharing. Per-conversation private on the 3rd member.
    set_override(e["tuser_tok"], e["tuser_id"], excluded, "private")

    proc = start_uplink(e)
    ev = []
    ok = True
    try:
        im_row = wait_until(lambda: mirror_of(e["db_path"], im), timeout=45, desc="s11 iMessage mirror")
        li_row = wait_until(lambda: mirror_of(e["db_path"], li), timeout=45, desc="s11 LinkedIn mirror")
        time.sleep(4)  # give reconcile a chance to (not) mirror the excluded one
        ex_row = mirror_of(e["db_path"], excluded)
        im_master, li_master = im_row[0], li_row[0]

        im_tag = wait_until(lambda: master_profile_tag(im_master), timeout=30, desc="s11 iMessage profile stamp")
        li_tag = wait_until(lambda: master_profile_tag(li_master), timeout=30, desc="s11 LinkedIn profile stamp")
    finally:
        stop_uplink(proc)

    # Both platforms mirrored.
    both_up = im_row is not None and li_row is not None
    ev.append("imessage_mirrored=%s linkedin_mirrored=%s" % (im_row is not None, li_row is not None))
    ok = ok and both_up
    # Excluded member (per-conv private) stayed OUT.
    ev.append("excluded_member_out=%s" % (ex_row is None))
    ok = ok and ex_row is None
    # Both mirrors carry the SAME profile id + displayName.
    im_id = (im_tag or {}).get("id")
    li_id = (li_tag or {}).get("id")
    same_profile = (im_id == prof_id and li_id == prof_id
                    and (im_tag or {}).get("displayName") == prof_name
                    and (li_tag or {}).get("displayName") == prof_name)
    ev.append("same_profile_id=%s (im=%s li=%s want=%s)" % (im_id == li_id == prof_id, im_id, li_id, prof_id))
    ev.append("profile_displayName=%s" % (im_tag or {}).get("displayName"))
    ok = ok and same_profile
    # Excluded room must NOT have been mirrored, hence no stamp to check; assert
    # it is not a child of the manager's space grouping.
    kids = space_children(MASTER_SPACE_ALICE, MASTER_ALICE_TOKEN)
    grouped = im_master in kids and li_master in kids
    ev.append("both_under_space_alice=%s" % grouped)
    ok = ok and grouped
    return ok, "; ".join(ev)


def scenario_12_contact_share_and_propose():
    """§12 phase 6: share one address-book contact up, group it with a mirrored
    room under one person_id, then have the master submit a PERSON-TARGETED
    proposal (identifier, not room) against that contact. Self-directed only —
    the seeded, shared handle is the tester's OWN synthetic self-number, so
    even if the approve->start-chat leg were driven for real it would message
    no one but the tester (imsg-startchat SC.A). No real iMessage daemon runs
    against this synthetic stack (docker-compose.test.yml is Synapse-only), so
    that leg is asserted at the proposal-shape level here; SC.A's manual
    self-test (PLAN-IMSG-STARTCHAT.md) is the real send-path acceptance check.

    Assertions:
      1. The self-number's com.jkali.contact state event lands in the master
         contacts room carrying the SAME person_id as the profile's mirrored
         room's com.jkali.profile stamp (cross-platform grouping, visible on
         the master, by construction from one com.jkali.contact_profiles doc).
      2. A second, NOT-shared contact (a different, unshared source) never
         appears in the master contacts room at all.
      3. The manager submits a person-targeted proposal (target_source +
         target_identifier + target_display, NO target_room); exactly one
         com.jkali.proposal with that shape lands in the teammate's dedicated
         local proposals room.
      4. The master has no send path: m.room.message into the proposals room
         is rejected (403) and a static scan of apps/master/*.js (reusing
         scenario 7's write-surface scanner) confirms no m.room.message send
         and no 'start-chat' reference anywhere in the master app.
    """
    e = fresh_env("s12")
    contacts_db_path = os.path.join(STATE_DIR, "contacts_s12.db")
    if os.path.exists(contacts_db_path):
        os.remove(contacts_db_path)

    # Synthetic E.164 self-number + a second, DIFFERENT-source handle that
    # stays unshared (contact-share policy below only share-alls imessage).
    tag_n = int(time.time()) % 1000000
    self_number = "+1555%07d" % tag_n
    other_number = "+1555%07d" % ((tag_n + 1) % 10000000)

    # 1a. Seed contacts.db directly (agents/contacts/contacts_store.py's real
    # API) with the self-number contact PLUS a second, not-shared contact.
    conn = contacts_store.open_store(contacts_db_path)
    try:
        contacts_store.upsert_contacts(conn, "imessage", [
            {"network_id": self_number, "kind": "phone", "display_name": "Self (Tester)"},
        ])
        contacts_store.upsert_contacts(conn, "linkedin", [
            {"network_id": other_number, "kind": "handle", "display_name": "Not Shared"},
        ])
    finally:
        conn.close()

    # 1b. Cross-platform profile (reuses scenario 11's shape): links the
    # self-number iMessage handle AND an existing mirrored LinkedIn room under
    # ONE person_id, set to share.
    li_space = create_space(e["tuser_tok"], "LinkedIn")
    li_room = make_convo(e, li_space, "Self Person (LinkedIn)")
    post_msg(e["contact_tok"], li_room, "seed for s12 cross-platform profile")

    prof_id = "cp_self_" + uniq("id")
    prof_name = "Self Person"
    set_profiles(e["tuser_tok"], e["tuser_id"], {"profiles": [{
        "id": prof_id, "displayName": prof_name,
        "roomIds": [li_room],
        "handleIds": [{"source": "imessage", "network_id": self_number}],
        "share": "share",
    }]})

    # 1c. Contact-share policy: share ALL imessage contacts. linkedin (the
    # NOT-shared contact's source) has no rule and global stays 'private', so
    # it never resolves shared (own dimension from conversation consent).
    local(e["tuser_tok"], "PUT",
          "/_matrix/client/v3/user/" + urllib.parse.quote(e["tuser_id"], safe="")
          + "/account_data/com.jkali.contact_share_policy",
          {"global": "private", "sources": {"imessage": "share-all"}})

    proc = start_uplink(e, extra_env={"UPLINK_CONTACTS_DB": contacts_db_path})
    ev = []
    ok = True
    try:
        # -- assertion 1: self-number contact mirrors, person_id matches the
        # profile's mirrored-room com.jkali.profile stamp.
        mcr = wait_until(lambda: meta_get(e["db_path"], "master_contacts_room"),
                         timeout=45, desc="s12 master contacts room")
        li_row = wait_until(lambda: mirror_of(e["db_path"], li_room), timeout=45,
                            desc="s12 li room mirror")
        li_master = li_row[0]
        li_tag = wait_until(lambda: master_profile_tag(li_master), timeout=30,
                            desc="s12 li profile stamp")

        self_key = contact_state_key("imessage", self_number)
        self_contact = wait_until(lambda: master_contact_states(mcr).get(self_key),
                                  timeout=45, desc="s12 self-number contact mirrored")

        person_match = (self_contact.get("person_id") == prof_id
                        and (li_tag or {}).get("id") == prof_id)
        ev.append("self_contact_person_id=%s li_profile_id=%s want=%s"
                  % (self_contact.get("person_id"), (li_tag or {}).get("id"), prof_id))
        ok = ok and person_match

        # -- assertion 2: the NOT-shared (linkedin-source) contact never appears.
        time.sleep(4)  # let the reconcile pass consider (and skip) it
        other_key = contact_state_key("linkedin", other_number)
        other_present = other_key in master_contact_states(mcr)
        ev.append("not_shared_contact_present=%s(want False)" % other_present)
        ok = ok and not other_present

        # -- assertion 3: person-targeted proposal, submitted by the master.
        mpr = wait_until(lambda: meta_get(e["db_path"], "master_proposals_room"),
                         timeout=45, desc="s12 master proposals room")
        lpr = wait_until(lambda: meta_get(e["db_path"], "local_proposals_room"),
                         timeout=45, desc="s12 local proposals room")
        master(MASTER_MANAGER_TOKEN, "POST",
              "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="") + "/join", {})

        nonce = "pmmng-test-" + uniq("n")
        prop_content = {
            "target_source": "imessage",
            "target_identifier": self_number,
            "target_display": prof_name,
            "body": nonce,
            "created_by": MASTER_MANAGER_USER,
            "origin_ts": int(time.time() * 1000),
        }
        master(MASTER_MANAGER_TOKEN, "PUT",
              "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="")
              + "/send/com.jkali.proposal/" + uniq("prop"), prop_content)

        got = wait_until(
            lambda: [p for p in local_events_of_type(e["tuser_tok"], lpr, "com.jkali.proposal")
                     if (p.get("content") or {}).get("body") == nonce] or None,
            timeout=45, desc="s12 identifier proposal pulled down")
        time.sleep(6)  # extra cycles must not duplicate it
        final_local = [p for p in local_events_of_type(e["tuser_tok"], lpr, "com.jkali.proposal")
                       if (p.get("content") or {}).get("body") == nonce]
        once = len(final_local) == 1
        ev.append("local_identifier_proposal_count=%d(want 1)" % len(final_local))
        ok = ok and once

        pc = (final_local[0].get("content") or {}) if final_local else {}
        shape_ok = (pc.get("target_identifier") == self_number
                   and pc.get("target_source") == "imessage"
                   and "target_room" not in pc)
        ev.append("identifier_shape_ok=%s target_identifier=%s has_target_room=%s"
                  % (shape_ok, pc.get("target_identifier"), "target_room" in pc))
        ok = ok and shape_ok

        # It landed ONLY in the local proposals room: never a mirror room, the
        # real conversation, or the contacts room (same isolation as scenario 10).
        convo_props = local_events_of_type(e["tuser_tok"], li_room, "com.jkali.proposal")
        isolated = not convo_props
        ev.append("isolated(not_in_convo)=%s" % isolated)
        ok = ok and isolated

        # -- assertion 4: the master cannot send at all (defense in depth) and
        # has no start-chat/m.room.message code path (static scan, reused from
        # scenario 7). start-chat itself needs the teammate's LOCAL account,
        # which the master never holds -> structurally unreachable from here.
        msg_code = None
        try:
            master(MASTER_MANAGER_TOKEN, "PUT",
                  "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="")
                  + "/send/m.room.message/" + uniq("mgrmsg12"),
                  {"msgtype": "m.text", "body": "manager should not be able to message here"})
            msg_code = 200  # must NOT happen
        except MxError as e2:
            msg_code = e2.code
        ev.append("manager_msg_send=%s(want 403)" % msg_code)
        ok = ok and msg_code == 403

        scanned, has_msg_send, send_types, write_violations = scan_apps_master_write_surface()
        no_send_path = scanned and not has_msg_send and not write_violations
        # apps/master never even names 'start-chat' (that concept lives only on
        # the teammate side, in apps/user/proposals.js + imessage/daemon.py).
        master_files = glob.glob(os.path.join(REPO, "apps", "master", "*.js"))
        no_startchat_ref = not any(
            "start-chat" in open(f).read() for f in master_files)
        ev.append("master_no_send_path=%s master_no_startchat_ref=%s"
                  % (no_send_path, no_startchat_ref))
        ok = ok and no_send_path and no_startchat_ref
    finally:
        stop_uplink(proc)

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
    ("9_media_reupload", scenario_9_media_reupload),
    ("10_proposal_down", scenario_10_proposal_down),
    ("11_profile_span_platforms", scenario_11_profile_span_platforms),
    ("12_contact_share_and_propose", scenario_12_contact_share_and_propose),
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
