#!/usr/bin/env python3
"""Integration harness for the uplink one-way sync (PLAN-MASTER-SYNC §13, P2.4/P2.5).

Drives scenarios against TWO disposable homeservers provisioned by sandbox.py.
The details below describe their modeled roles, not live service dependencies:

  * TEST-USER hub  — an ISOLATED throwaway Synapse (server_name: localhost) on
    127.0.0.1:8028, brought up by tests/integration/docker-compose.test.yml
    (compose project matrix-synctest). This models ONE teammate's LOCAL stack.
    It is fully separate from the live matrix-wa stack (8008/8009/8010) — this
    harness NEVER touches matrix-wa.
  * MASTER hub     — an isolated throwaway master on an allocated loopback port.
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
Run tests/integration/run.sh to create both stacks; no live credentials are read.
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

from sandbox import load_manifest
SANDBOX = load_manifest()
TEST_HS = SANDBOX["local_url"]
MASTER_HS = SANDBOX["master_url"]
LOCAL_SERVER = "localhost"
SHARED_SECRET = SANDBOX["local_secret"]
STATE_DIR = SANDBOX["state_dir"]


def load_master_tokens():
    path = os.path.join(SANDBOX["master_dir"], "tokens.local")
    with open(path) as f:
        txt = f.read()
    return dict(re.findall(r"^(\w+)='([^']*)'", txt, re.M))


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
    """state in {'share','direct','private'} (direct-share-level plan, D1) or
    None to clear back to the (unset ->) private default — there is no more
    'inherit': an absent or unrecognized override always resolves private.

    Room account-data lives under the user path
    /user/{userId}/rooms/{roomId}/account_data/{type} (this is exactly what
    /sync surfaces as rooms.join[room].account_data, which the uplink reads)."""
    content = {"state": state} if state else {}
    local(token, "PUT",
          "/_matrix/client/v3/user/" + urllib.parse.quote(user_id, safe="")
          + "/rooms/" + urllib.parse.quote(room_id, safe="")
          + "/account_data/com.jkali.share_override", content)


def get_override_raw(token, user_id, room_id):
    """The raw com.jkali.share_override content for one room, or None if
    absent/cleared (used by the D0 migration scenario to observe the
    materialized override)."""
    try:
        return local(token, "GET",
                     "/_matrix/client/v3/user/" + urllib.parse.quote(user_id, safe="")
                     + "/rooms/" + urllib.parse.quote(room_id, safe="")
                     + "/account_data/com.jkali.share_override")
    except MxError:
        return None


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
    durably ingests references while delivery is frozen; docker start it ->
    messages deliver, still exactly once."""
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
        with sqlite3.connect(e["db_path"]) as db:
            delivered_before = db.execute("SELECT count(*) FROM delivery_map").fetchone()[0]

        # MASTER GOES OFFLINE.
        docker(["stop", SANDBOX["master_container"]])
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
                         or "buffering" in log_snapshot.lower()
                         or "masterunreachable" in log_snapshot.lower())
        with sqlite3.connect(e["db_path"]) as db:
            delivered_during = db.execute("SELECT count(*) FROM delivery_map").fetchone()[0]
            queued_during = db.execute("SELECT count(*) FROM pending_events").fetchone()[0]

        # MASTER COMES BACK.
        docker(["start", SANDBOX["master_container"]])
        wait_master_health()
        wait_until(
            lambda: all(any(m["body"] == b for m in master_messages(master_room))
                        for b in buffered),
            timeout=90, desc="s4 delivery after master back")
    finally:
        stop_uplink(proc)
        # Safety: make absolutely sure master is running for later scenarios.
        docker(["start", SANDBOX["master_container"]], check=False)
        wait_master_health()

    final = master_messages(master_room)
    counts = {b: sum(1 for m in final if m["body"] == b) for b in buffered}
    dupes = {b: c for b, c in counts.items() if c != 1}
    # Ingestion and delivery are independent: source cursor may advance only
    # because durable references exist, while confirmed delivery cannot advance.
    delivery_frozen = delivered_before == delivered_during
    ingestion_durable = wm_before != wm_during and queued_during >= len(buffered)
    ok = buffered_flag and delivery_frozen and ingestion_durable and (not dupes)
    ev = ("buffered_log=%s delivery_frozen=%s ingestion_durable=%s pending=%s all_once=%s dupes=%s"
          % (buffered_flag, delivery_frozen, ingestion_durable, queued_during, not dupes, dupes))
    return ok, ev


def scenario_5_explicit_share_and_migration():
    """Ports the removed standing-policy 'share-all' scenario to the explicit
    per-conversation override model (D1), plus the D0 upgrade path it
    introduced.

    Part A — explicit multi-room share + exclusion (what the old scenario
    proved: several conversations can be shared together, and one stays OUT
    despite sharing the same source). Under the explicit model there is no
    more 'share-all', so this is now a bulk explicit 'share' write (the
    actual shape of apps/user's bulk action, D3) on two rooms plus an
    explicit 'private' override on a third. It also proves the flip side of
    the old behavior: a standing policy used to auto-mirror a room created
    *later*; under the explicit model NOTHING does that any more — a brand
    new room with no override stays private until it gets its own explicit
    override, which is asserted both ways (absent -> private, then set ->
    mirrors).

    Part B — D0 migration (the upgrade path the removed standing policy
    needed): rewinds one already-mirrored room to a simulated PRE-S1 state
    (override cleared back to absent, an old-style share-all standing policy
    restored, the one-time migration flag rewound in state.db) and restarts
    the SAME uplink instance against the SAME state.db. D0's S1 acceptance:
    the room keeps the SAME master_room_id (no delete/re-create — zero
    revocation observed) AND ends with an explicit `migrated: true` 'share'
    override materialized in account-data.
    """
    e = fresh_env("s5")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    li_space = create_space(e["tuser_tok"], "LinkedIn")
    a = make_convo(e, imsg_space, "iMsg A (bulk share)")
    b = make_convo(e, imsg_space, "iMsg B (bulk share)")
    c = make_convo(e, imsg_space, "iMsg C (excluded)")
    li = make_convo(e, li_space, "LinkedIn D (never touched)")
    for rid in (a, b, c, li):
        post_msg(e["contact_tok"], rid, "seed for " + rid[:8])
    # Bulk explicit share on two rooms; explicit exclusion on a third sharing
    # the same source; the LinkedIn room gets no override at all (stays the
    # unset/private default).
    set_override(e["tuser_tok"], e["tuser_id"], a, "share")
    set_override(e["tuser_tok"], e["tuser_id"], b, "share")
    set_override(e["tuser_tok"], e["tuser_id"], c, "private")

    ev = []
    ok = True
    proc = start_uplink(e)
    try:
        a_row = wait_until(lambda: mirror_of(e["db_path"], a), timeout=45, desc="s5 A mirror")
        b_row = wait_until(lambda: mirror_of(e["db_path"], b), timeout=45, desc="s5 B mirror")
        time.sleep(4)  # allow reconcile to consider c and li
        c_mirror = mirror_of(e["db_path"], c)
        li_mirror = mirror_of(e["db_path"], li)
        both_shared = a_row is not None and b_row is not None
        ev.append("bulk_A_and_B_mirrored=%s" % both_shared)
        ok = ok and both_shared
        ev.append("C_excluded=%s LinkedIn_untouched_stays_private=%s"
                  % (c_mirror is None, li_mirror is None))
        ok = ok and c_mirror is None and li_mirror is None

        # A brand-new room, same source, NO override: under the explicit
        # model nothing auto-mirrors it any more (the removed behavior).
        d = make_convo(e, imsg_space, "iMsg E (new arrival, no override yet)")
        post_msg(e["contact_tok"], d, "brand new imessage thread")
        time.sleep(4)
        d_mirror_before = mirror_of(e["db_path"], d)
        ev.append("new_room_stays_private_without_override=%s" % (d_mirror_before is None))
        ok = ok and d_mirror_before is None
        # ...but an explicit override on it DOES mirror it (reconcile keeps
        # picking up newly-written explicit levels every pass).
        set_override(e["tuser_tok"], e["tuser_id"], d, "share")
        d_row = wait_until(lambda: mirror_of(e["db_path"], d), timeout=45,
                           desc="s5 new room mirrors once explicitly shared")
        ev.append("new_room_mirrors_once_explicit=%s" % (d_row is not None))
        ok = ok and d_row is not None
    finally:
        stop_uplink(proc)

    # -- Part B: D0 migration acceptance, against the SAME state.db ---------
    a_master_before = a_row[0]
    # Rewind room `a` to a simulated pre-S1 state: clear its explicit
    # override (back to absent — nothing "was set", as pre-S1 account-data
    # never had this event type at all), restore an old-style standing
    # policy that would have shared it under the retained inherit-semantics
    # resolver, and rewind the one-time migration flag so migrate_explicit_
    # levels() runs again as if for the first time.
    set_override(e["tuser_tok"], e["tuser_id"], a, None)
    set_policy(e["tuser_tok"], e["tuser_id"],
               {"global": "private", "sources": {"imessage": "share-all"}})
    db = sqlite3.connect(e["db_path"])
    try:
        db.execute("DELETE FROM meta WHERE k='migrated_explicit_levels'")
        db.commit()
    finally:
        db.close()

    proc2 = start_uplink(e)
    try:
        migrated_override = wait_until(
            lambda: get_override_raw(e["tuser_tok"], e["tuser_id"], a) or None,
            timeout=45, desc="s5 migrated override materialized")
        migrated_flag = wait_until(lambda: meta_get(e["db_path"], "migrated_explicit_levels"),
                                   timeout=45, desc="s5 migration flag set")
        a_master_after = (mirror_of(e["db_path"], a) or (None,))[0]
        # Give the same pass a moment to have evaluated (and NOT executed)
        # any deletion before checking liveness.
        time.sleep(3)
        alice_joined = a_master_before in master(
            MASTER_ALICE_TOKEN, "GET", "/_matrix/client/v3/joined_rooms")["joined_rooms"]
        still_alive = master_room_alive(a_master_before) and alice_joined
        still_child = a_master_before in space_children(MASTER_SPACE_ALICE, MASTER_ALICE_TOKEN)
    finally:
        stop_uplink(proc2)

    migrated_ok = (migrated_override.get("state") == "share"
                  and migrated_override.get("migrated") is True)
    ev.append("migrated_override=%s(want state=share migrated=True)" % migrated_override)
    ok = ok and migrated_ok
    ev.append("migration_flag_set=%s" % (migrated_flag == "1"))
    ok = ok and migrated_flag == "1"
    same_room = a_master_after == a_master_before
    ev.append("same_master_room_id(no_delete_recreate)=%s (before=%s after=%s)"
              % (same_room, a_master_before, a_master_after))
    ok = ok and same_room
    ev.append("mirror_still_alive_and_grouped=%s (alive=%s child=%s)"
              % (still_alive and still_child, still_alive, still_child))
    ok = ok and still_alive and still_child
    return ok, "; ".join(ev)


def scenario_6_revoke_levels():
    """Revoke at each level -> the corresponding master mirror room(s) are
    removed from the manager's view.

    Ports the old 3-tier standing-policy revocation scenario (per-conv clear,
    per-source private-all, global private) to the explicit model (D1), which
    has exactly ONE level knob per room: the per-conversation override. What
    the old scenario actually proved — "however a room came to be shared,
    setting it un-shared fully revokes the master copy" — is preserved here
    against the two real entry points a room can be shared through: 'share'
    and the new, higher-stakes 'direct' (worth its own dedicated revocation
    coverage, since a `direct` room is also carrying the uplink's auto-send
    capability, D2 — revoking it must remove that capability along with the
    mirror)."""
    e = fresh_env("s6")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    li_space = create_space(e["tuser_tok"], "LinkedIn")
    r_share = make_convo(e, imsg_space, "Revoke-from-share")
    r_direct = make_convo(e, li_space, "Revoke-from-direct")
    for rid in (r_share, r_direct):
        post_msg(e["contact_tok"], rid, "seed " + rid[:8])
    set_override(e["tuser_tok"], e["tuser_id"], r_share, "share")
    set_override(e["tuser_tok"], e["tuser_id"], r_direct, "direct")

    proc = start_uplink(e)
    ev = []
    ok = True
    try:
        m_share = wait_until(lambda: mirror_of(e["db_path"], r_share), timeout=45, desc="s6 share")
        m_direct = wait_until(lambda: mirror_of(e["db_path"], r_direct), timeout=45, desc="s6 direct")
        room_share, room_direct = m_share[0], m_direct[0]
        # Both are mirrored: each is a child of space:alice (the manager's
        # grouping) AND @alice (the uplink) is joined to write it.
        time.sleep(2)
        kids0 = space_children(MASTER_SPACE_ALICE, MASTER_ALICE_TOKEN)
        all_up = all(r in kids0 for r in (room_share, room_direct))
        ev.append("initially_both_mirrored=%s" % all_up)
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

        # (1) REVOKE the 'share' room: override -> private.
        set_override(e["tuser_tok"], e["tuser_id"], r_share, "private")
        wait_until(lambda: mirror_of(e["db_path"], r_share) is None, timeout=45,
                   desc="s6 share revoked")
        share_gone = revoked(r_share, room_share)
        ev.append("share_revoked=%s" % share_gone)
        ok = ok and share_gone

        # (2) REVOKE the 'direct' room: override -> private. Also the room
        # that was carrying the auto-send capability, so this is the more
        # consequential of the two revocations.
        set_override(e["tuser_tok"], e["tuser_id"], r_direct, "private")
        wait_until(lambda: mirror_of(e["db_path"], r_direct) is None, timeout=45,
                   desc="s6 direct revoked")
        direct_gone = revoked(r_direct, room_direct)
        ev.append("direct_revoked=%s" % direct_gone)
        ok = ok and direct_gone
    finally:
        stop_uplink(proc)
    return ok, "; ".join(ev)


# ------------------------------------------------- apps/master build-time scan
# The static half of scenario 7. apps/master's read-only guarantee is "absent
# code, not a hidden button", so it is asserted against the SOURCE of EVERY .js
# file in that directory (not main.js alone — the console gained invites.js, and
# a second file must not be able to smuggle in a write path):
#   * no /send/m.room.message anywhere;
#   * message-shaped writes are proposals; CRM operation writes are confined
#     to the verified metadata store;
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
        r"^/admin/delete-teammate$",             # ENROLL_BASE + '/admin/delete-teammate'
    ],
    "PUT": [
        r"^/_matrix/client/v3/rooms//send/com\.jkali\.proposal/$",
    ],
    "DELETE": [],
}

# New CRM authority is file-scoped, never a blanket expansion for all modules.
MASTER_FILE_WRITE_ALLOWLIST = {
    "control.js": {"POST": [r"^/admin/control$"]},
    "notifications.js": {"POST": [r"^/admin/notifications$"]},
    "notification-sw.js": {"POST": [r"^/admin/notifications/receipt$"]},
    "main.js": {"POST": [r"^/admin/crm-room$"], "PUT": [r"^/_matrix/client/v3/rooms//send/com\.jkali\.read_request/$"]},
    "crm-store.js": {"PUT": [r"^/_matrix/client/v3/rooms//send/com\.jkali\.crm\.operation/crm_$"]},
    "crm-members.js": {"PUT": [r"^/_matrix/client/v3/rooms//send/com\.jkali\.crm\.direct_request/crm_direct__$"]},
    "crm-batches.js": {"PUT": [
        r"^/_matrix/client/v3/rooms//send/com\.jkali\.proposal/crm_proposal__$",
        r"^/_matrix/client/v3/rooms//send/com\.jkali\.crm\.batch_request/crm_batch__submit$",
        r"^/_matrix/client/v3/rooms//send/com\.jkali\.crm\.batch_request/crm_batch__cancel$",
    ]},
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


def scan_master_import_graph():
    """Walk relative imports/re-exports, including literal dynamic imports.

    This is a deliberately restricted application module graph, not a general JS
    parser. A nonliteral dynamic import, external module, or absent local module
    is refused rather than silently omitted from the send-path inspection.
    """
    pending = sorted(glob.glob(os.path.join(REPO, "apps", "master", "*.js")))
    visited, violations = set(), []
    forbidden = {"shared/ui/" + name + ".js" for name in
                 ("render", "rows", "nav", "chat", "search", "sources", "connections")}
    root = os.path.realpath(REPO)
    while pending:
        path = os.path.realpath(pending.pop())
        if path in visited:
            continue
        visited.add(path)
        relative = os.path.relpath(path, root).replace(os.sep, "/")
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            violations.append("unavailable/outside module: " + relative)
            continue
        if relative in forbidden or relative.startswith("apps/user/"):
            violations.append("native-send dependency: " + relative)
            continue
        with open(path) as handle:
            code = strip_js_comments(handle.read())
        if re.search(r"/send/m\.room\.(?:message|encrypted)", code):
            violations.append("native-send code: " + relative)
        patterns = [
            r"\b(?:import|export)\s+[^;\"']*?\bfrom\s*['\"]([^'\"]+)['\"]",
            r"(?:^|[;\n])\s*import\s*['\"]([^'\"]+)['\"]",
            r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
        ]
        imports = [value for pattern in patterns for value in re.findall(pattern, code)]
        dynamic = list(re.finditer(r"\bimport\s*\(", code))
        literal_dynamic = re.findall(patterns[2], code)
        if len(dynamic) != len(literal_dynamic):
            violations.append("uninspectable dynamic import: " + relative)
        for value in imports:
            if not value.startswith(".") or "?" in value or "#" in value:
                violations.append("uninspectable import: " + relative + " -> " + value)
                continue
            pending.append(os.path.join(os.path.dirname(path), value))
    return sorted(os.path.relpath(path, root).replace(os.sep, "/") for path in visited), violations


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
            allowed = MASTER_WRITE_ALLOWLIST.get(method, []) + MASTER_FILE_WRITE_ALLOWLIST.get(name, {}).get(method, [])
            if not any(re.match(p, joined) for p in allowed):
                violations.append("%s:%s %s" % (name, method, joined or expr.strip()[:60]))
    _, import_violations = scan_master_import_graph()
    violations.extend(import_violations)
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
    # com.jkali.proposal or the file-scoped CRM operation, and (c) every non-GET call must
    # match the write-surface allowlist above. Comments are stripped first (the
    # files document the ABSENCE of an m.room.message path in prose).
    scanned, has_msg_send, send_types, write_violations = scan_apps_master_write_surface()
    non_proposal_sends = send_types - {"com.jkali.proposal", "com.jkali.crm.operation", "com.jkali.crm.direct_request", "com.jkali.crm.batch_request", "com.jkali.read_request"}
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

        decision_ids = []
        for n, decision in enumerate(('accepted', 'declined')):
            proposal_id = master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="")
                + "/send/com.jkali.proposal/" + uniq("queue"), {**prop_content, "body": marker + str(n), "com.jkali.send_mode": "suggest"})["event_id"]
            local_proposal = wait_until(lambda: next((p for p in local_events_of_type(e["tuser_tok"], lpr, "com.jkali.proposal")
                if p.get("content", {}).get("com.jkali.proposal_source_event") == proposal_id), None), timeout=45, desc="queued suggestion")
            local_id = local_proposal["event_id"]
            local(e["tuser_tok"], "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(lpr, safe="") + "/state/com.jkali.proposal_decision/" + urllib.parse.quote(local_id, safe=""),
                {"schema": 1, "proposal_event_id": local_id, "decision": decision, "send_state": "none"})
            def read_decision():
                try:
                    value = master(MASTER_MANAGER_TOKEN, "GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="") + "/state/com.jkali.proposal_status/" + urllib.parse.quote(proposal_id, safe=""))
                    return value if value.get("decision") == decision else None
                except MxError:
                    return None
            assert wait_until(read_decision, timeout=45, desc="exact decision relayed")
            decision_ids.append(proposal_id)
        try:
            master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="") + "/state/com.jkali.proposal_status/" + urllib.parse.quote(decision_ids[0], safe=""), {"decision":"declined"})
            raise AssertionError("Manager forged source decision")
        except MxError as error:
            assert error.code == 403
        # Real filtered pagination can yield empty intermediate pages. Follow
        # their end tokens; do not drop older suggestions behind state traffic.
        proposal_events, cursor, seen = [], None, set()
        for page in range(100):
            query = {"dir":"b", "limit":1, "filter":json.dumps({"types":["com.jkali.proposal"]})}
            if cursor: query["from"] = cursor
            data = master(MASTER_MANAGER_TOKEN, "GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="") + "/messages?" + urllib.parse.urlencode(query))
            proposal_events.extend(data["chunk"])
            cursor = data.get("end")
            if not cursor: break
            assert cursor not in seen, "Repeated history cursor"
            seen.add(cursor)
        else: raise AssertionError("Incomplete suggestion history")
        assert len(proposal_events) == 3
        ev.append("independent_accepted_declined_history=verified manager_forgery=403")

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
    restart_once = len(after_restart) == 3
    if not restart_once:
        ok = False
    ev.append("after_restart_count=%d(want 3)" % len(after_restart))
    ev.append("master_proposals_room=%s local_proposals_room=%s" % (mpr, lpr))
    return ok, "; ".join(ev)


def scenario_11_profile_span_platforms():
    """§12 phase 5 unified contacts: a contact profile spanning TWO platforms.

    Link an iMessage room + a LinkedIn room (+ a 2nd iMessage room) to ONE
    contact profile. Ported for D1: a profile's own `share` field no longer
    causes conversation mirroring at all (conversation sharing is explicit-
    only — profile/source/global inheritance was removed from that path).
    What this scenario actually proves survives unchanged: a shared contact
    profile still GROUPS conversations across platforms with the same
    com.jkali.profile stamp — it just now requires each member room to carry
    its OWN explicit 'share' override to be shared, same as any other room.
    So: explicit 'share' on the iMessage AND LinkedIn rooms (the profile
    itself stays `share` too, for storage-shape parity with the real
    account-data document, but is inert for the mirror decision). Expect:
      * BOTH the iMessage room and the LinkedIn room mirror up, and each mirror
        carries the SAME com.jkali.profile {id, displayName} — so the master can
        group this one person's threads across platforms;
      * a per-conversation `private` override on the 3rd member keeps it OUT
        (no override ever makes it 'share' — it is simply never shared) and,
        being unmirrored, it never gets a profile stamp.
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
    # One profile, three linked rooms across two sources (grouping only —
    # D1 dropped any effect this had on mirroring).
    set_profiles(e["tuser_tok"], e["tuser_id"], {"profiles": [{
        "id": prof_id, "displayName": prof_name,
        "roomIds": [im, li, excluded], "share": "share",
    }]})
    # The two grouped members each need their OWN explicit share; the 3rd
    # member gets an explicit private override (and would stay unshared even
    # without one — there is no more inheritance to override).
    set_override(e["tuser_tok"], e["tuser_id"], im, "share")
    set_override(e["tuser_tok"], e["tuser_id"], li, "share")
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

    Assertions (the LinkedIn room carries its OWN explicit 'share' override,
    D1 — the profile groups it, it no longer mirrors it):
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

    # D1: a profile's own `share` no longer mirrors its member room — the
    # room needs its own explicit override (contact-share, asserted below via
    # com.jkali.contact_share_policy, is a separate, untouched dimension).
    set_override(e["tuser_tok"], e["tuser_id"], li_room, "share")

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
def scenario_13_contact_backfill_on_enable():
    """pm_mng-q5u.2: the contact mirror is a per-pass DIFF, not a forward-only
    cursor, so enabling a source BACKFILLS contacts imported before the flip,
    a later import's new contact flows, revoking tombstones everything, and
    re-enabling re-pushes it. Self-directed only: every seeded handle is a
    synthetic +1555… number of the tester's own, nothing is ever sent.

    Legs (all against the real uplink + both live homeservers):
      1. seed TWO imessage contacts, NO contact-share policy (default
         private) -> the contacts room exists but holds ZERO
         com.jkali.contact states after a settle.
      2. PUT policy {imessage: share-all} -> BOTH pre-existing rows appear
         (deleted:false). This leg fails on the old cursor code, which had
         already advanced past both rows while they were private.
      3. re-import a 3-row snapshot (the two + one new) -> the third appears.
      4. PUT policy global private -> all three become {deleted: true}.
      5. PUT {imessage: share-all} again -> all three are back, deleted:false
         (their mirror rows were dropped on the tombstone 2xx, so the diff
         re-pushes them).
    """
    e = fresh_env("s13")
    contacts_db_path = os.path.join(STATE_DIR, "contacts_s13.db")
    if os.path.exists(contacts_db_path):
        os.remove(contacts_db_path)

    tag_n = int(time.time()) % 1000000
    nums = ["+1555%07d" % ((tag_n + i) % 10000000) for i in range(3)]
    keys = [contact_state_key("imessage", n) for n in nums]

    def seed(count):
        conn = contacts_store.open_store(contacts_db_path)
        try:
            contacts_store.upsert_contacts(conn, "imessage", [
                {"network_id": nums[i], "kind": "phone", "display_name": "Self %d" % i}
                for i in range(count)
            ])
        finally:
            conn.close()

    def put_policy(policy):
        local(e["tuser_tok"], "PUT",
              "/_matrix/client/v3/user/" + urllib.parse.quote(e["tuser_id"], safe="")
              + "/account_data/com.jkali.contact_share_policy", policy)

    def states(mcr, want_keys, deleted):
        """True when every key in want_keys is present with the wanted
        deleted-flag (a missing key is neither)."""
        st = master_contact_states(mcr)
        return all(k in st and bool(st[k].get("deleted")) == deleted for k in want_keys)

    # leg 1: two rows, default-private policy.
    seed(2)
    proc = start_uplink(e, extra_env={"UPLINK_CONTACTS_DB": contacts_db_path})
    ev = []
    ok = True
    try:
        mcr = wait_until(lambda: meta_get(e["db_path"], "master_contacts_room"),
                         timeout=45, desc="s13 master contacts room")
        time.sleep(4)  # the pass that created the room has planned+applied by now
        n0 = len(master_contact_states(mcr))
        ev.append("private_states=%d(want 0)" % n0)
        ok = ok and n0 == 0

        # leg 2: enable -> backfill of rows that pre-date the flip.
        put_policy({"global": "private", "sources": {"imessage": "share-all"}})
        wait_until(lambda: states(mcr, keys[:2], False), timeout=75,
                   desc="s13 backfill of pre-existing contacts")
        ev.append("backfilled=2")

        # leg 3: a later import adds one contact (complete 3-row snapshot).
        seed(3)
        wait_until(lambda: states(mcr, keys, False), timeout=75,
                   desc="s13 new contact after re-import")
        ev.append("new_contact_flowed=1")

        # leg 4: revoke -> every mirrored handle tombstoned.
        put_policy({"global": "private"})
        wait_until(lambda: states(mcr, keys, True), timeout=75,
                   desc="s13 revoke tombstones all")
        ev.append("revoked=3")

        # leg 5: re-enable -> re-pushed after tombstone.
        put_policy({"global": "private", "sources": {"imessage": "share-all"}})
        wait_until(lambda: states(mcr, keys, False), timeout=75,
                   desc="s13 re-share after tombstone")
        ev.append("reshared=3")
    except Exception as ex:
        ok = False
        ev.append("EXCEPTION: %r" % ex)
    finally:
        stop_uplink(proc)
    return ok, "; ".join(ev)


# ---------------------------------------------------------------- F2 empirical probe
# The direct-share-level plan's F2 finding asks whether a real mautrix bridge,
# on receiving a message posted by the teammate's OWN account into a portal
# room whose body happens to start with '!' (e.g. "!wa help"), would execute
# it as an admin command rather than relay it as ordinary chat text. This
# throwaway TEST-USER hub (tests/integration/docker-compose.test.yml, compose
# project matrix-synctest, 127.0.0.1:8028) is a BARE Synapse — no bridge
# container of any kind runs against it (confirmed: `docker ps` under that
# compose project shows only postgres + synapse) — so the probe cannot be
# executed here and this harness does not improvise a fake bridge to answer
# it. NOT EXECUTABLE IN CI. The defense ships regardless of how the
# hypothesis resolves: `_direct_send_gate()`'s sanitization step (D2.2)
# refuses to auto-send any body whose first non-whitespace character is '!',
# so the uplink's one send path can never hand a bridge a command-shaped
# string, whatever that bridge would do with one. A real answer to the
# hypothesis needs a live mautrix bridge (e.g. the matrix-wa stack's
# mautrix-whatsapp, which this harness must never touch) exercised manually,
# outside CI.


def scenario_14_direct_proposal_autosend():
    """Direct proposal round-trip (D2/D2b, wire contract in the plan). A
    manager proposal targeting a 'direct' room is auto-sent by the uplink
    with no review click; the same shape against a 'share' room is untouched.

    Order matters here: the FIRST proposal pulled by a fresh uplink is always
    a cold start (D2.3 — no prior `since`/dedup map), which routes the WHOLE
    batch to the inbox regardless of level, so state loss can never replay
    history as real sends. This scenario deliberately uses the negative
    ('share') leg's proposal AS that cold-start primer (proving its own
    'ordinary draft, no send' assertion in the same step), THEN posts the
    'direct' proposal once a `since` token and a non-empty proposal_map exist
    — an ordinary incremental pull, exactly what D2 requires before it will
    ever auto-send.
    """
    e = fresh_env("s14")
    imsg_space = create_space(e["tuser_tok"], "iMessage")
    direct_room = make_convo(e, imsg_space, "Direct Target")
    share_room = make_convo(e, imsg_space, "Share Target")
    for rid in (direct_room, share_room):
        post_msg(e["contact_tok"], rid, "seed for " + rid[:8])
    set_override(e["tuser_tok"], e["tuser_id"], direct_room, "direct")
    set_override(e["tuser_tok"], e["tuser_id"], share_room, "share")

    ev = []
    ok = True
    proc = start_uplink(e)
    try:
        wait_until(lambda: mirror_of(e["db_path"], direct_room), timeout=45,
                   desc="s14 direct mirror")
        wait_until(lambda: mirror_of(e["db_path"], share_room), timeout=45,
                   desc="s14 share mirror")
        mpr = wait_until(lambda: meta_get(e["db_path"], "master_proposals_room"),
                         timeout=45, desc="s14 master proposals room")
        lpr = wait_until(lambda: meta_get(e["db_path"], "local_proposals_room"),
                         timeout=45, desc="s14 local proposals room")
        master(MASTER_MANAGER_TOKEN, "POST",
              "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="") + "/join", {})

        # -- negative leg first (also the cold-start primer, see docstring) --
        marker_share = "SHAREPROP-" + uniq("sp")
        prop_share = {
            "target_room": share_room, "body": marker_share,
            "created_by": MASTER_MANAGER_USER, "origin_ts": int(time.time() * 1000),
        }
        master(MASTER_MANAGER_TOKEN, "PUT",
              "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="")
              + "/send/com.jkali.proposal/" + uniq("prop"), prop_share)
        share_recs = wait_until(
            lambda: [p for p in local_events_of_type(e["tuser_tok"], lpr, "com.jkali.proposal")
                     if (p.get("content") or {}).get("body") == marker_share] or None,
            timeout=45, desc="s14 share proposal reaches inbox")
        share_content = (share_recs[0].get("content") or {}) if share_recs else {}
        share_ordinary = (not share_content.get("com.jkali.auto_sent")
                          and not share_content.get("com.jkali.send_ambiguous"))
        ev.append("share_proposal_ordinary_draft=%s" % share_ordinary)
        ok = ok and share_ordinary
        share_no_send = not [
            m for m in local_events_of_type(e["tuser_tok"], share_room, "m.room.message")
            if (m.get("content") or {}).get("body") == marker_share]
        ev.append("share_room_no_message_sent=%s" % share_no_send)
        ok = ok and share_no_send

        # -- positive leg: now an ordinary incremental pull (since is set, --
        # -- proposal_map is non-empty) — auto-send is eligible.           --
        marker = "DIRECT-" + uniq("d")
        prop_direct = {
            "target_room": direct_room, "body": marker,
            "created_by": MASTER_MANAGER_USER, "origin_ts": int(time.time() * 1000),
        }
        master(MASTER_MANAGER_TOKEN, "PUT",
              "/_matrix/client/v3/rooms/" + urllib.parse.quote(mpr, safe="")
              + "/send/com.jkali.proposal/" + uniq("prop"), prop_direct)

        # Exactly one m.room.message auto-sent into the REAL local
        # conversation, carrying the cosmetic provenance field.
        sent = wait_until(
            lambda: [m for m in local_events_of_type(e["tuser_tok"], direct_room, "m.room.message")
                     if (m.get("content") or {}).get("body") == marker] or None,
            timeout=45, desc="s14 auto-sent message appears in the conversation")
        ev.append("auto_sent_message_count=%d(want 1)" % len(sent))
        ok = ok and len(sent) == 1
        cosmetic = ((sent[0].get("content") or {}).get("com.jkali.auto_sent_from_proposal")
                   if sent else None)
        ev.append("cosmetic_provenance_field=%r" % cosmetic)
        ok = ok and bool(cosmetic)

        # The local proposals room holds exactly ONE record for this
        # proposal, and it is the non-actionable com.jkali.auto_sent:true one
        # (never a plain pending draft alongside it).
        recs = [p for p in local_events_of_type(e["tuser_tok"], lpr, "com.jkali.proposal")
                if (p.get("content") or {}).get("body") == marker]
        ev.append("inbox_record_count=%d(want 1)" % len(recs))
        ok = ok and len(recs) == 1
        rec = (recs[0].get("content") or {}) if recs else {}
        auto_sent_shape_ok = (rec.get("com.jkali.auto_sent") is True
                              and bool(rec.get("sent_event_id"))
                              and not rec.get("com.jkali.send_ambiguous"))
        ev.append("inbox_record_is_non_actionable_auto_sent=%s (auto_sent=%s sent_event_id=%s)"
                  % (auto_sent_shape_ok, rec.get("com.jkali.auto_sent"), rec.get("sent_event_id")))
        ok = ok and auto_sent_shape_ok

        # A second, identical pull produces NO duplicate: several more loop
        # cycles must leave both counts at exactly one.
        time.sleep(8)
        sent2 = [m for m in local_events_of_type(e["tuser_tok"], direct_room, "m.room.message")
                if (m.get("content") or {}).get("body") == marker]
        recs2 = [p for p in local_events_of_type(e["tuser_tok"], lpr, "com.jkali.proposal")
                if (p.get("content") or {}).get("body") == marker]
        no_dup = len(sent2) == 1 and len(recs2) == 1
        ev.append("no_duplicate_after_second_pull=%s (sent=%d inbox=%d)"
                  % (no_dup, len(sent2), len(recs2)))
        ok = ok and no_dup
    finally:
        stop_uplink(proc)
    return ok, "; ".join(ev)


def scenario_15_revocation_retains_history():
    """Existing authority contract: stop future copies; do not promise erasure."""
    e = fresh_env("s15")
    source = create_space(e["tuser_tok"], "iMessage")
    room = make_convo(e, source, "Revocation retention fixture")
    marker = "s15-before-" + uniq("retained")
    post_msg(e["contact_tok"], room, marker)
    set_override(e["tuser_tok"], e["tuser_id"], room, "share")
    proc = start_uplink(e)
    try:
        destination = wait_until(lambda: mirror_of(e["db_path"], room), timeout=45,
                                 desc="s15 mirror")[0]
        master(MASTER_MANAGER_TOKEN, "POST", "/_matrix/client/v3/rooms/"
               + urllib.parse.quote(destination, safe="") + "/join", {})
        wait_until(lambda: any(m["body"] == marker for m in master_messages(destination, MASTER_MANAGER_TOKEN)),
                   timeout=45, desc="s15 manager reads original history")
        set_override(e["tuser_tok"], e["tuser_id"], room, "private")
        wait_until(lambda: mirror_of(e["db_path"], room) is None, timeout=45,
                   desc="s15 cleanup complete")
        private_marker = "s15-after-" + uniq("private")
        post_msg(e["contact_tok"], room, private_marker)
        time.sleep(4)
        retained = master_messages(destination, MASTER_MANAGER_TOKEN)
        joined = master(MASTER_MANAGER_TOKEN, "GET", "/_matrix/client/v3/joined_rooms")["joined_rooms"]
        old_readable = any(m["body"] == marker for m in retained)
        no_new_copy = not any(m["body"] == private_marker for m in retained)
        with sqlite3.connect(e["db_path"]) as db:
            no_pending = db.execute("SELECT count(*) FROM pending_events WHERE local_room_id=?", (room,)).fetchone()[0] == 0
        unlinked = destination not in space_children(MASTER_SPACE_ALICE, MASTER_ALICE_TOKEN)
        kicked = destination not in joined
        return (old_readable and no_new_copy and no_pending and unlinked and kicked,
                "historical_message_still_readable=%s future_copy_stopped=%s queue_empty=%s unlinked=%s manager_kicked=%s"
                % (old_readable, no_new_copy, no_pending, unlinked, kicked))
    finally:
        stop_uplink(proc)


def scenario_16_original_timestamps():
    """Native time survives delayed import; repair writes state, not messages."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('repair_timestamps', os.path.join(REPO, 'imessage', 'repair_timestamps.py'))
    repair = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair)
    e = fresh_env('s16')
    space = create_space(e['tuser_tok'], 'iMessage')
    room = make_convo(e, space, 'Timestamp fixture')
    native = 1704110400000
    post_media_event(e['tuser_tok'], room, {'msgtype': 'm.text', 'body': 'native fixture',
        'com.jkali.origin_ts': native, 'com.beepa.timestamp_source': 'imessage'})
    legacy = post_msg(e['tuser_tok'], room, 'legacy fixture')
    base = '/_matrix/client/v3/rooms/' + urllib.parse.quote(room, safe='')
    local(e['tuser_tok'], 'PUT', base + '/state/com.beepa.timestamp_correction/' + urllib.parse.quote(legacy, safe=''),
          {'version': 1, 'source': 'imessage', 'origin_ts': native + 1000})
    set_override(e['tuser_tok'], e['tuser_id'], room, 'share')
    proc = start_uplink(e)
    try:
        row = wait_until(lambda: mirror_of(e['db_path'], room), timeout=45, desc='timestamp mirror')
        destination = row[0]
        messages = wait_until(lambda: (lambda m: m if len(m) == 2 else None)(master_messages(destination)),
                              timeout=45, desc='timestamp messages')
    finally:
        stop_uplink(proc)
    if [m['origin_ts'] for m in messages] != [native, native + 1000]:
        return False, 'original timestamp was lost'
    target = messages[0]['event_id']
    def request(method, path, body=None):
        try:
            return master(MASTER_ALICE_TOKEN, method, path, body)
        except MxError as exc:
            raise urllib.error.HTTPError('', exc.code, '', {}, None)
    journal = []
    first = repair.correction(request, destination, target, native - 1000,
                              lambda sender: sender == MASTER_ALICE_USER, True, lambda *v: journal.append(v))
    again = repair.correction(request, destination, target, native - 1000,
                              lambda sender: sender == MASTER_ALICE_USER, True, lambda *v: journal.append(v))
    unchanged = master_messages(destination) == messages
    # Initial-sync timeline filtering must retain correction state and messages.
    result = master(MASTER_ALICE_TOKEN, 'GET', '/_matrix/client/v3/sync', query={'timeout': 0,
        'filter': json.dumps({'room': {'rooms': [destination], 'timeline': {'limit': 5,
            'not_types': ['com.beepa.timestamp_correction']}}})})
    snap = result['rooms']['join'][destination]
    state = snap.get('state', {}).get('events', [])
    state_ok = any(ev['type'] == 'com.beepa.timestamp_correction' and ev['state_key'] == target for ev in state)
    return first == 'corrected' and again == 'already_correct' and unchanged and state_ok, (
        'native/repaired source times preserved; original messages unchanged=%s; correction state survives filter=%s; rerun=%s'
        % (unchanged, state_ok, again))


def scenario_17_discord_dms():
    environment = fresh_env('discord')
    token = environment['tuser_tok']
    identity = environment['tuser_id']
    root = create_space(token, 'Discord')
    dms = create_space(token, 'Direct Messages')
    link_child(token, root, dms)
    direct = make_convo(environment, dms, 'Discord DM')
    group = make_convo(environment, dms, 'Discord group DM')
    third, third_token = register_user(uniq('third'))
    local(token, 'POST', '/_matrix/client/v3/rooms/' + group + '/invite', {'user_id': third})
    join_room(third_token, group)
    post_msg(environment['contact_tok'], direct, 'Discord incoming')
    own_event = post_msg(token, direct, 'Discord outgoing')
    original = local(token, 'GET', '/_matrix/client/v3/rooms/' + direct + '/event/' + own_event)
    post_msg(third_token, group, 'Private group history')
    set_override(token, identity, direct, 'share')
    process = start_uplink(environment)
    try:
        direct_mirror = wait_until(lambda: mirror_of(environment['db_path'], direct), desc='nested Discord DM')[0]
        messages = wait_until(lambda: (lambda result: result if len(result) == 2 else None)(master_messages(direct_mirror)), desc='Discord messages')
        assert mirror_of(environment['db_path'], group) is None, 'Group inherited sharing'
        assert master_source_tag(direct_mirror) == 'discord'
        outgoing = next(message for message in messages if message['body'] == 'Discord outgoing')
        incoming = next(message for message in messages if message['body'] == 'Discord incoming')
        assert outgoing['from_me'] is True and not incoming['from_me']
        assert outgoing['origin_ts'] == original['origin_server_ts']
        set_override(token, identity, group, 'share')
        group_mirror = wait_until(lambda: mirror_of(environment['db_path'], group), desc='Discord group share')[0]
        wait_until(lambda: master_messages(group_mirror), desc='Discord group history')
        media = upload_media(TEST_HS, environment['contact_tok'], TINY_PNG, 'image/png', 'discord.png')
        post_media_event(environment['contact_tok'], group, {'msgtype': 'm.image', 'body': 'discord.png', 'url': media,
            'info': {'mimetype': 'image/png', 'size': len(TINY_PNG)}})
        mirrored = wait_until(lambda: next((message for message in master_messages(group_mirror) if message['msgtype'] == 'm.image'), None), desc='Discord media')
        assert download_media(MASTER_HS, MASTER_ALICE_TOKEN, mirrored['url']) == TINY_PNG
    finally:
        stop_uplink(process)
    post_msg(environment['contact_tok'], direct, 'Offline Discord message')
    process = start_uplink(environment)
    try:
        wait_until(lambda: len(master_messages(direct_mirror)) == 3, desc='Discord restart catch-up')
        assert len(master_messages(group_mirror)) == 2
        set_override(token, identity, group, 'private')
        wait_until(lambda: mirror_of(environment['db_path'], group) is None, desc='Discord group revoke')
        post_msg(third_token, group, 'Revoked group message')
        assert mirror_of(environment['db_path'], direct) is not None
    finally:
        stop_uplink(process)
    return True, 'Nested DM/group DM consent, authorship, timestamps, media reupload, restart catch-up without duplicates and revocation verified with synthetic Matrix events.'


def scenario_18_daemon_joins_invite_no_mirror():
    """The uplink accepts a bridge invite while the browser is closed, and that
    join shares NOTHING: membership is not consent. Then the teammate shares the
    conversation and the mirror appears on the next reconcile."""
    environment = fresh_env('invites')
    token = environment['tuser_tok']
    identity = environment['tuser_id']
    # The bridge BOT (a code-owned SOURCES[].botMxid on this server_name) creates
    # the portal and invites the teammate, who never joins it themselves.
    bot_id, bot_token = register_user('whatsappbot')
    assert bot_id == '@whatsappbot:localhost', 'bot identity must match the catalog'
    space = create_space(token, 'WhatsApp')
    portal = local(bot_token, 'POST', '/_matrix/client/v3/createRoom',
                   {'name': 'Contact One', 'preset': 'private_chat',
                    'invite': [identity]})['room_id']
    link_child(token, space, portal)

    def joined():
        return portal in local(token, 'GET', '/_matrix/client/v3/joined_rooms')['joined_rooms']

    assert not joined(), 'the teammate must still only be INVITED'
    process = start_uplink(environment)
    try:
        # No com.jkali.autojoin_ack yet: the ack gate is fail-closed, so the
        # daemon joins nothing however many passes it makes.
        wait_until(lambda: meta_get(environment['db_path'], 'last_ingestion_success'),
                   desc='uplink first pass')
        time.sleep(6)
        assert not joined(), 'joined without the teammate confirm (ack gate failed open)'
        # The teammate confirms in the app -> the daemon may join.
        local(token, 'PUT', '/_matrix/client/v3/user/' + urllib.parse.quote(identity, safe='')
              + '/account_data/com.jkali.autojoin_ack', {'ok': True, 'ts': 1})
        wait_until(joined, timeout=90, desc='daemon joins the bridge invite')
        # Joining is membership only: no override -> nothing mirrored.
        time.sleep(6)
        assert mirror_of(environment['db_path'], portal) is None, 'a joined room was mirrored'
        # ...and the teammate's own explicit share is what starts the mirror.
        post_msg(bot_token, portal, 'incoming after the daemon join')
        set_override(token, identity, portal, 'share')
        mirror = wait_until(lambda: mirror_of(environment['db_path'], portal),
                            timeout=90, desc='mirror after explicit share')[0]
        wait_until(lambda: master_messages(mirror), desc='mirrored message')
        assert master_source_tag(mirror) == 'whatsapp'
    finally:
        stop_uplink(process)
    return True, ('Daemon-side invite auto-join verified: refused without the '
                  'teammate ack, joined after it, mirrored nothing until the '
                  'conversation was explicitly shared.')



# ------------------------------------------------------------------ F7 helpers
def sched_rows(db_path):
    """Every scheduled_sends row: {event_id: (state, reason, authorship)}."""
    if not os.path.exists(db_path):
        return {}
    db = sqlite3.connect(db_path)
    try:
        return {r[0]: (r[1], r[2], r[3]) for r in db.execute(
            "SELECT event_id, state, reason, authorship FROM scheduled_sends").fetchall()}
    finally:
        db.close()


def write_schedule(e, lpr, target, body, send_at):
    """The teammate's browser gesture: a com.jkali.scheduled_send they authored."""
    res = local(e["tuser_tok"], "PUT", "/_matrix/client/v3/rooms/"
                + urllib.parse.quote(lpr, safe="") + "/send/com.jkali.scheduled_send/"
                + uniq("sched"),
                {"target_room": target, "body": body, "send_at": int(send_at),
                 "created_by": e["tuser_id"]})
    return res["event_id"]


def cancel_schedule(e, lpr, event_id):
    """Cancellation is a STATE event keyed by the scheduled event id."""
    local(e["tuser_tok"], "PUT", "/_matrix/client/v3/rooms/"
          + urllib.parse.quote(lpr, safe="") + "/state/com.jkali.scheduled_cancel/"
          + urllib.parse.quote(event_id, safe=""), {"ts": int(time.time() * 1000)})


def schedule_outcomes(e, lpr, event_id):
    return [(x.get("content") or {}) for x in
            local_events_of_type(e["tuser_tok"], lpr, "com.jkali.scheduled_outcome")
            if (x.get("content") or {}).get("scheduled_event_id") == event_id]


def proposals_room_ready(e):
    """Start the uplink, wait for the local proposals room, return (proc, lpr)."""
    proc = start_uplink(e)
    lpr = wait_until(lambda: meta_get(e["db_path"], "local_proposals_room"),
                     timeout=45, desc="local proposals room")
    return proc, lpr


def scenario_19_schedule_fires_once_across_restart():
    """A teammate schedule survives a daemon restart ACROSS send_at and fires
    exactly once. The arming row lives only in state.db, which is what makes the
    restart the real test: nothing is reconstructed by rescanning the room."""
    e = fresh_env("s19")
    space = create_space(e["tuser_tok"], "iMessage")
    room = make_convo(e, space, "Schedule Target")
    post_msg(e["contact_tok"], room, "s19 seed")
    marker = "SCHED-" + uniq("s19")
    proc, lpr = proposals_room_ready(e)
    try:
        # The room must be ATTRIBUTED (S-6) before the schedule can fire; that
        # set is persisted by reconcile, not by sharing.
        wait_until(lambda: sqlite3.connect(e["db_path"]).execute(
            "SELECT count(*) FROM attributed_rooms").fetchone()[0], timeout=45,
            desc="s19 attributed set")
        send_at = time.time() * 1000 + 25000
        eid = write_schedule(e, lpr, room, marker, send_at)
        wait_until(lambda: sched_rows(e["db_path"]).get(eid), timeout=45, desc="s19 armed")
    finally:
        stop_uplink(proc)                      # DOWN across send_at
    sent_while_down = [m for m in local_events_of_type(e["tuser_tok"], room, "m.room.message")
                       if (m.get("content") or {}).get("body") == marker]
    time.sleep(max(0.0, (send_at - time.time() * 1000) / 1000.0) + 2)
    proc = start_uplink(e)
    try:
        sent = wait_until(
            lambda: [m for m in local_events_of_type(e["tuser_tok"], room, "m.room.message")
                     if (m.get("content") or {}).get("body") == marker] or None,
            timeout=60, desc="s19 scheduled message fires after restart")
        time.sleep(8)                          # several more loop cycles
        again = [m for m in local_events_of_type(e["tuser_tok"], room, "m.room.message")
                 if (m.get("content") or {}).get("body") == marker]
        outs = schedule_outcomes(e, lpr, eid)
        row = sched_rows(e["db_path"]).get(eid)
    finally:
        stop_uplink(proc)
    provenance = (sent[0].get("content") or {}).get("com.jkali.from_schedule") if sent else None
    ok = (not sent_while_down and len(again) == 1 and len(outs) == 1
          and outs[0].get("state") == "sent" and bool(outs[0].get("sent_event_id"))
          and "body" not in outs[0] and provenance == eid and row and row[0] == "sent")
    return ok, ("no_send_while_daemon_down=%s sent_count=%d(want 1) outcome_records=%d(want 1) "
                "outcome_state=%r provenance=%s row=%r"
                % (not sent_while_down, len(again), len(outs),
                   outs[0].get("state") if outs else None, provenance == eid, row))


def scenario_20_schedule_held_when_conversation_moves_on():
    """S-8: any message in the target after the schedule was written HOLDS it —
    visibly, with a reason — instead of double-texting."""
    e = fresh_env("s20")
    space = create_space(e["tuser_tok"], "iMessage")
    room = make_convo(e, space, "Moved On")
    post_msg(e["contact_tok"], room, "s20 seed")
    marker = "HELD-" + uniq("s20")
    proc, lpr = proposals_room_ready(e)
    try:
        wait_until(lambda: sqlite3.connect(e["db_path"]).execute(
            "SELECT count(*) FROM attributed_rooms").fetchone()[0], timeout=45,
            desc="s20 attributed set")
        send_at = time.time() * 1000 + 20000
        eid = write_schedule(e, lpr, room, marker, send_at)
        wait_until(lambda: sched_rows(e["db_path"]).get(eid), timeout=45, desc="s20 armed")
        post_msg(e["contact_tok"], room, "s20 they replied first")
        outs = wait_until(lambda: schedule_outcomes(e, lpr, eid) or None,
                          timeout=60, desc="s20 held record")
        time.sleep(6)
        sent = [m for m in local_events_of_type(e["tuser_tok"], room, "m.room.message")
                if (m.get("content") or {}).get("body") == marker]
        outs = schedule_outcomes(e, lpr, eid)
    finally:
        stop_uplink(proc)
    ok = (not sent and len(outs) == 1 and outs[0].get("state") == "held"
          and outs[0].get("reason") == "superseded")
    return ok, ("nothing_sent=%s outcome_records=%d(want 1) state=%r reason=%r"
                % (not sent, len(outs), outs[0].get("state") if outs else None,
                   outs[0].get("reason") if outs else None))


def scenario_21_cancel_before_send_at():
    """A cancel STATE event before send_at stops the send; the daemon's
    fire-time point-read is what decides, so the record says 'cancelled'."""
    e = fresh_env("s21")
    space = create_space(e["tuser_tok"], "iMessage")
    room = make_convo(e, space, "Cancelled")
    post_msg(e["contact_tok"], room, "s21 seed")
    marker = "CANCEL-" + uniq("s21")
    proc, lpr = proposals_room_ready(e)
    try:
        wait_until(lambda: sqlite3.connect(e["db_path"]).execute(
            "SELECT count(*) FROM attributed_rooms").fetchone()[0], timeout=45,
            desc="s21 attributed set")
        send_at = time.time() * 1000 + 20000
        eid = write_schedule(e, lpr, room, marker, send_at)
        wait_until(lambda: sched_rows(e["db_path"]).get(eid), timeout=45, desc="s21 armed")
        cancel_schedule(e, lpr, eid)
        outs = wait_until(lambda: schedule_outcomes(e, lpr, eid) or None,
                          timeout=60, desc="s21 cancelled record")
        time.sleep(max(0.0, (send_at - time.time() * 1000) / 1000.0) + 8)
        sent = [m for m in local_events_of_type(e["tuser_tok"], room, "m.room.message")
                if (m.get("content") or {}).get("body") == marker]
        outs = schedule_outcomes(e, lpr, eid)
    finally:
        stop_uplink(proc)
    ok = not sent and len(outs) == 1 and outs[0].get("state") == "cancelled"
    return ok, ("nothing_sent=%s outcome_records=%d(want 1) state=%r"
                % (not sent, len(outs), outs[0].get("state") if outs else None))


def scenario_22_schedule_fires_with_master_unreachable():
    """The teammate's own scheduled message is LOCAL work. It must fire with the
    master down — the stage deliberately runs above the connectivity gate."""
    e = fresh_env("s22")
    space = create_space(e["tuser_tok"], "iMessage")
    room = make_convo(e, space, "Offline Master")
    post_msg(e["contact_tok"], room, "s22 seed")
    marker = "OFFLINE-" + uniq("s22")
    proc, lpr = proposals_room_ready(e)
    try:
        wait_until(lambda: sqlite3.connect(e["db_path"]).execute(
            "SELECT count(*) FROM attributed_rooms").fetchone()[0], timeout=45,
            desc="s22 attributed set")
        docker(["stop", SANDBOX["master_container"]])
        try:
            send_at = time.time() * 1000 + 15000
            eid = write_schedule(e, lpr, room, marker, send_at)
            sent = wait_until(
                lambda: [m for m in local_events_of_type(e["tuser_tok"], room, "m.room.message")
                         if (m.get("content") or {}).get("body") == marker] or None,
                timeout=90, desc="s22 scheduled message fires with master down")
            outs = schedule_outcomes(e, lpr, eid)
        finally:
            docker(["start", SANDBOX["master_container"]], check=False)
            wait_master_health()
    finally:
        stop_uplink(proc)
    ok = len(sent) == 1 and len(outs) == 1 and outs[0].get("state") == "sent"
    return ok, ("sent_with_master_down=%d(want 1) outcome_state=%r"
                % (len(sent), outs[0].get("state") if outs else None))


def scenario_23_manager_timed_direct_superseded():
    """A manager-TIMED proposal for a `direct` room re-runs all twelve D2 gates
    at send_at. A message that arrives in between supersedes it, so the teammate
    gets the ORDINARY draft and nothing is sent — never a hold affordance for
    the manager."""
    e = fresh_env("s23")
    space = create_space(e["tuser_tok"], "iMessage")
    room = make_convo(e, space, "Timed Direct")
    post_msg(e["contact_tok"], room, "s23 seed")
    set_override(e["tuser_tok"], e["tuser_id"], room, "direct")
    proc, lpr = proposals_room_ready(e)
    try:
        wait_until(lambda: mirror_of(e["db_path"], room), timeout=45, desc="s23 mirror")
        mpr = wait_until(lambda: meta_get(e["db_path"], "master_proposals_room"),
                         timeout=45, desc="s23 master proposals room")
        master(MASTER_MANAGER_TOKEN, "POST", "/_matrix/client/v3/rooms/"
               + urllib.parse.quote(mpr, safe="") + "/join", {})
        # The first pull is always a cold start (D2.3), so prime it with an
        # ordinary proposal before the timed one — exactly as scenario 14 does.
        primer = "S23PRIMER-" + uniq("p")
        master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/"
               + urllib.parse.quote(mpr, safe="") + "/send/com.jkali.proposal/" + uniq("prop"),
               {"target_room": room, "body": primer, "created_by": MASTER_MANAGER_USER,
                "origin_ts": int(time.time() * 1000)})
        wait_until(lambda: [x for x in local_events_of_type(e["tuser_tok"], lpr, "com.jkali.proposal")
                            if (x.get("content") or {}).get("body") == primer] or None,
                   timeout=45, desc="s23 cold-start primer")
        marker = "TIMED-" + uniq("t")
        send_at = int(time.time() * 1000 + 25000)
        master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/"
               + urllib.parse.quote(mpr, safe="") + "/send/com.jkali.proposal/" + uniq("prop"),
               {"target_room": room, "body": marker, "created_by": MASTER_MANAGER_USER,
                "origin_ts": int(time.time() * 1000), "send_at": send_at})
        armed = wait_until(lambda: [k for k, v in sched_rows(e["db_path"]).items()
                                    if v[2] == "manager"] or None,
                           timeout=45, desc="s23 manager-timed row armed")
        parked = [x for x in local_events_of_type(e["tuser_tok"], lpr, "com.jkali.proposal")
                  if (x.get("content") or {}).get("body") == marker]
        # The conversation moves on BEFORE send_at.
        post_msg(e["contact_tok"], room, "s23 they answered first")
        time.sleep(max(0.0, (send_at - time.time() * 1000) / 1000.0) + 12)
        recs = [x for x in local_events_of_type(e["tuser_tok"], lpr, "com.jkali.proposal")
                if (x.get("content") or {}).get("body") == marker]
        sent = [m for m in local_events_of_type(e["tuser_tok"], room, "m.room.message")
                if (m.get("content") or {}).get("body") == marker]
        row = sched_rows(e["db_path"]).get(armed[0])
    finally:
        stop_uplink(proc)
    content = (recs[0].get("content") or {}) if recs else {}
    ordinary = (bool(recs) and not content.get("com.jkali.auto_sent")
                and not content.get("com.jkali.send_ambiguous"))
    ok = (not parked and len(recs) == 1 and ordinary and not sent
          and row and row[0] == "refused" and row[1] == "superseded")
    return ok, ("parked_without_artifact=%s inbox_records=%d(want 1) ordinary_draft=%s "
                "nothing_sent=%s row=%r"
                % (not parked, len(recs), ordinary, not sent, row))



def scenario_24_read_unread():
    """Real app receipt writer + local counts + two-homeserver read-state mirror."""
    e = fresh_env("s24")
    space = create_space(e["tuser_tok"], "iMessage")
    rid = make_convo(e, space, "Read state test")
    set_override(e["tuser_tok"], e["tuser_id"], rid, "share")
    room_path = "/_matrix/client/v3/rooms/" + urllib.parse.quote(rid, safe="")

    def app(action, **data):
        payload = dict(base=TEST_HS, token=e["tuser_tok"], roomId=rid,
                       userId=e["tuser_id"], action=action, **data)
        run = subprocess.run(["node", os.path.join(HERE, "read_state_client.js")],
                             input=json.dumps(payload), capture_output=True, text=True, check=True)
        return json.loads(run.stdout)

    def snapshot():
        data = local(e["tuser_tok"], "GET", "/_matrix/client/v3/sync", query={"timeout": "0"})
        return data["rooms"]["join"][rid]

    baseline = post_msg(e["contact_tok"], rid, "baseline")
    app("mark", eventId=baseline)
    wait_until(lambda: app("parse", room=snapshot())["unread"] == 0, desc="baseline read")
    proc = start_uplink(e)
    try:
        mr = wait_until(lambda: mirror_of(e["db_path"], rid), desc="read-state mirror")[0]
        master(MASTER_MANAGER_TOKEN, "POST", "/_matrix/client/v3/rooms/"
               + urllib.parse.quote(mr, safe="") + "/join", {})
        state_path = ("/_matrix/client/v3/rooms/" + urllib.parse.quote(mr, safe="")
                      + "/state/com.jkali.read_state/")

        def state():
            return master(MASTER_MANAGER_TOKEN, "GET", state_path)

        wait_until(lambda: state().get("teammate_read_ts", 0) > 0, desc="initial teammate receipt")
        incoming = post_msg(e["contact_tok"], rid, "new unread message")
        wait_until(lambda: app("parse", room=snapshot())["unread"] == 1, desc="app unread count one")
        event = local(e["tuser_tok"], "GET", room_path + "/event/" + urllib.parse.quote(incoming, safe=""))
        incoming_ts = event["origin_server_ts"]
        wait_until(lambda: any(m["body"] == "new unread message" for m in master_messages(mr)),
                   desc="incoming mirror")
        assert state()["teammate_read_ts"] < incoming_ts, "master must show teammate unread"
        app("mark", eventId=incoming)
        wait_until(lambda: app("parse", room=snapshot())["unread"] == 0, desc="app clears unread")
        wait_until(lambda: state()["teammate_read_ts"] >= incoming_ts, desc="master clears teammate unread")
        before = state()["teammate_read_ts"]
        reply = post_msg(e["tuser_tok"], rid, "reply for remote receipt")
        local(e["contact_tok"], "POST", room_path + "/read_markers",
              {"m.fully_read": reply, "m.read": reply})
        wait_until(lambda: app("parse", room=snapshot())["remoteReadTs"] > 0, desc="app remote read")
        wait_until(lambda: state()["remote_read_ts"] > 0, desc="master remote read")
        assert state()["teammate_read_ts"] >= before, "remote receipt must not erase teammate read position"
        remote_before = state()["remote_read_ts"]
        stop_uplink(proc)
        proc = start_uplink(e)
        after_restart = post_msg(e["contact_tok"], rid, "unread after restart")
        restart_event = local(e["tuser_tok"], "GET", room_path + "/event/"
                              + urllib.parse.quote(after_restart, safe=""))
        app("mark", eventId=after_restart)
        wait_until(lambda: state()["teammate_read_ts"] >= restart_event["origin_server_ts"],
                   desc="new receipt mirrored by restarted daemon")
        assert state()["remote_read_ts"] >= remote_before, "restart must retain remote read position"
        return True, "app unread 0→1→0; actual markRead writer; master unread→read; remote receipt; restart"
    finally:
        stop_uplink(proc)


def scenario_25_receipt_outage_recovery():
    """Receipt-only changes survive master outage + daemon restart; private drops them."""
    e = fresh_env("s25")
    space = create_space(e["tuser_tok"], "iMessage")
    rid = make_convo(e, space, "Receipt outage")
    private_rid = make_convo(e, space, "Receipt revoked")
    for room in (rid, private_rid):
        set_override(e["tuser_tok"], e["tuser_id"], room, "share")
        post_msg(e["contact_tok"], room, "seed")
    proc = start_uplink(e)
    stopped = False
    try:
        mr = wait_until(lambda: mirror_of(e["db_path"], rid), desc="outage mirror")[0]
        revoked = wait_until(lambda: mirror_of(e["db_path"], private_rid), desc="revocation mirror")[0]
        wait_until(lambda: len(master_messages(mr)) >= 1, desc="seed delivery")
        docker(["stop", SANDBOX["master_container"]]); stopped = True
        expected = {}
        targets = {}
        for room in (rid, private_rid):
            eid = post_msg(e["contact_tok"], room, "read while master sleeps")
            path = "/_matrix/client/v3/rooms/" + urllib.parse.quote(room, safe="")
            event = local(e["tuser_tok"], "GET", path + "/event/" + urllib.parse.quote(eid, safe=""))
            expected[room] = event["origin_server_ts"]
            targets[room] = eid
            local(e["tuser_tok"], "POST", path + "/read_markers", {"m.fully_read": eid, "m.read": eid})

        def queued(room):
            with sqlite3.connect(e["db_path"]) as db:
                return db.execute("SELECT teammate_read_ts,dirty FROM receipt_outbox WHERE local_room_id=?", (room,)).fetchone()
        wait_until(lambda: all(queued(r) and queued(r)[0] >= expected[r] and queued(r)[1] for r in expected),
                   desc="receipts durably queued during outage")
        set_override(e["tuser_tok"], e["tuser_id"], private_rid, "private")
        wait_until(lambda: queued(private_rid) is None, desc="private drops pending receipt before recovery")
        stop_uplink(proc)
        proc = start_uplink(e)
        docker(["start", SANDBOX["master_container"]]); stopped = False
        wait_master_health()
        path = "/_matrix/client/v3/rooms/" + urllib.parse.quote(mr, safe="") + "/state/com.jkali.read_state/"
        wait_until(lambda: master(MASTER_ALICE_TOKEN, "GET", path).get("teammate_read_ts", 0) >= expected[rid],
                   timeout=90, desc="receipt retried after restart with no new receipt")
        wait_until(lambda: queued(rid) and queued(rid)[1] == 0, desc="receipt acknowledged")
        def exact_clean():
            with sqlite3.connect(e['db_path']) as db:
                row = db.execute('SELECT dirty FROM message_receipts WHERE local_room_id=? AND target=?', (rid, targets[rid])).fetchone()
                revoked_rows = db.execute('SELECT count(*) FROM message_receipts WHERE local_room_id=?', (private_rid,)).fetchone()[0]
                mapped = db.execute('SELECT master_event_id FROM delivery_map WHERE master_room_id=? AND local_event_id=?', (mr, targets[rid])).fetchone()
            if not row or row[0] or not mapped or revoked_rows: return False
            exact = master(MASTER_ALICE_TOKEN, 'GET', '/_matrix/client/v3/rooms/' + urllib.parse.quote(mr, safe='') +
                           '/state/com.jkali.message_receipt/' + urllib.parse.quote(mapped[0], safe=''))
            return exact.get('teammate_read') is True
        wait_until(exact_clean, timeout=90, desc='exact receipt survives outage/restart and revoked target stays absent')
        # The scoped account may have left the revoked room. Either absence or
        # membership denial proves no new state was published to it.
        try:
            leaked = master(MASTER_ALICE_TOKEN, "GET", "/_matrix/client/v3/rooms/"
                            + urllib.parse.quote(revoked, safe="") + "/state/com.jkali.read_state/")
            assert leaked.get("teammate_read_ts", 0) < expected[private_rid]
        except MxError as exc:
            assert exc.code in (403, 404)
        return True, "receipt durable during outage; process restart; recovery without new receipt; private suppressed"
    finally:
        stop_uplink(proc)
        if stopped:
            docker(["start", SANDBOX["master_container"]], check=False)
            wait_master_health()


def scenario_26_message_checks():
    """Trusted delivery + exact late read targets, across both real homeservers."""
    e = fresh_env("s26")
    space = create_space(e["tuser_tok"], "iMessage")
    rid = make_convo(e, space, "Message checkmarks")
    bot_id, bot_token = register_user('imessagebot')
    path = "/_matrix/client/v3/rooms/" + urllib.parse.quote(rid, safe="")
    local(e["tuser_tok"], 'POST', path + '/invite', {'user_id': bot_id})
    join_room(bot_token, rid)
    set_override(e["tuser_tok"], e["tuser_id"], rid, "share")
    a = post_msg(e["tuser_tok"], rid, "older outgoing")
    b = post_msg(e["tuser_tok"], rid, "newer outgoing")
    proc = start_uplink(e)
    try:
        mr = wait_until(lambda: mirror_of(e["db_path"], rid), desc="checks mirror")[0]
        def mapped(eid):
            with sqlite3.connect(e['db_path']) as db:
                row = db.execute('SELECT master_event_id FROM delivery_map WHERE master_room_id=? AND local_event_id=?', (mr, eid)).fetchone()
                return row[0] if row else None
        ma = wait_until(lambda: mapped(a), desc='A mapped')
        mb = wait_until(lambda: mapped(b), desc='B mapped')
        def stamp(eid):
            try:
                return master(MASTER_ALICE_TOKEN, 'GET', '/_matrix/client/v3/rooms/' + urllib.parse.quote(mr, safe='') +
                              '/state/com.jkali.message_receipt/' + urllib.parse.quote(eid, safe=''))
            except MxError as exc:
                if exc.code == 404: return {}
                raise
        def status(token, eid, **extra):
            local(token, 'PUT', path + '/send/com.beeper.message_send_status/' + uniq('status'),
                  {'status': 'SUCCESS', 'm.relates_to': {'rel_type': 'm.reference', 'event_id': eid}, **extra})
        status(bot_token, a)
        wait_until(lambda: stamp(ma).get('sent'), desc='bridge acceptance mirrored')
        assert not stamp(ma).get('delivered'), 'acceptance is not delivery'
        status(bot_token, a, **{'com.jkali.delivered': True})
        wait_until(lambda: stamp(ma).get('delivered'), desc='confirmed delivery mirrored')
        # A forged delivery on B and a late read of A cannot advance B.
        status(e['contact_tok'], b, **{'com.jkali.delivered': True})
        local(e['contact_tok'], 'POST', path + '/read_markers', {'m.read': a, 'm.fully_read': a})
        wait_until(lambda: stamp(ma).get('read'), desc='A exact read mirrored')
        assert not stamp(mb).get('read') and not stamp(mb).get('delivered')
        room = local(e['tuser_tok'], 'GET', '/_matrix/client/v3/sync', query={'timeout': '0'})['rooms']['join'][rid]
        result = subprocess.run(['node', os.path.join(HERE, 'read_state_client.js')], input=json.dumps({
            'base': TEST_HS, 'token': e['tuser_tok'], 'roomId': rid, 'userId': e['tuser_id'],
            'action': 'parse', 'sourceId': 'imessage', 'room': room}), capture_output=True, text=True, check=True)
        checks = json.loads(result.stdout)['receipts']
        assert checks[a]['read'] and not checks.get(b, {}).get('read') and not checks.get(b, {}).get('delivered')
        stop_uplink(proc); proc = start_uplink(e)
        wait_until(lambda: stamp(ma).get('read'), desc='read persists across restart')
        return True, 'one gray on acceptance; two gray on delivery; two blue for exact A; newer B stays unconfirmed; restart'
    finally:
        stop_uplink(proc)


def scenario_27_native_imessage_receipts():
    """Real daemon + both homeservers/uplink/UI parser; only Apple CLI is fake."""
    import importlib.util
    e = fresh_env('s27')
    space = create_space(e['tuser_tok'], 'iMessage')
    rid = make_convo(e, space, 'Native receipt recovery')
    bot_id, bot_token = register_user('imessagebot')
    path = '/_matrix/client/v3/rooms/' + urllib.parse.quote(rid, safe='')
    local(e['tuser_tok'], 'POST', path + '/invite', {'user_id': bot_id})
    join_room(bot_token, rid)
    power = local(e['tuser_tok'], 'GET', path + '/state/m.room.power_levels')
    power.setdefault('users', {})[bot_id] = 100
    local(e['tuser_tok'], 'PUT', path + '/state/m.room.power_levels', power)
    set_override(e['tuser_tok'], e['tuser_id'], rid, 'share')
    a = post_msg(e['tuser_tok'], rid, 'identical outgoing text')
    b = post_msg(e['tuser_tok'], rid, 'identical outgoing text')
    spec = importlib.util.spec_from_file_location('receipt_daemon', os.path.join(REPO, 'imessage/daemon.py'))
    daemon = importlib.util.module_from_spec(spec); spec.loader.exec_module(daemon)
    state = os.path.join(os.path.dirname(e['db_path']), 'native-receipts')
    cfg = dict(port=29350, hs_url=TEST_HS, domain='localhost', user_id=e['tuser_id'],
               bot_id=bot_id, as_token='synthetic', hs_token='synthetic', cli_path='/never-execute',
               attachment_allow_prefixes=[], backfill_count=0)
    native = {'id': 'native-a', 'threadID': 'chat', 'isSender': True, 'isDelivered': False}
    def runner(argv, **kwargs):
        assert argv[:3] == ['/never-execute', '--json', '--no-events']
        assert argv[3] == 'send'
        return subprocess.CompletedProcess(argv, 0, json.dumps([native]).encode(), b'')
    daemon.initialize(cfg, state_dir=state, runner=runner)
    daemon.map_add('chat', rid)
    daemon.rate_ok = lambda *args: True
    daemon.mx = lambda method, endpoint, content, **kw: local(bot_token, method, endpoint, content)
    for event_id, native_id in [(a, 'native-a'), (b, 'native-b')]:
        native['id'] = native_id
        assert daemon.process_outbound_event({'event_id': event_id, 'room_id': rid, 'sender': e['tuser_id'],
            'type': 'm.room.message', 'content': {'msgtype': 'm.text', 'body': 'identical outgoing text'}})
    proc = start_uplink(e)
    try:
        mr = wait_until(lambda: mirror_of(e['db_path'], rid), desc='native receipt mirror')[0]
        def mapped(event_id):
            with sqlite3.connect(e['db_path']) as db:
                row = db.execute('SELECT master_event_id FROM delivery_map WHERE master_room_id=? AND local_event_id=?', (mr, event_id)).fetchone()
                return row[0] if row else None
        ma = wait_until(lambda: mapped(a), desc='native A mapped')
        mb = wait_until(lambda: mapped(b), desc='native B mapped')
        def stamp(event_id):
            try:
                return master(MASTER_ALICE_TOKEN, 'GET', '/_matrix/client/v3/rooms/' + urllib.parse.quote(mr, safe='') +
                    '/state/com.jkali.message_receipt/' + urllib.parse.quote(event_id, safe=''))
            except MxError as exc:
                if exc.code == 404: return {}
                raise
        daemon.publish_native_receipts()
        wait_until(lambda: stamp(ma).get('sent'), desc='native acceptance')
        assert not stamp(ma).get('delivered')
        daemon.reconcile_native_receipt('chat', dict(native, id='native-a', isDelivered=True))
        daemon.publish_native_receipts()
        wait_until(lambda: stamp(ma).get('delivered'), desc='native delivery')
        assert not stamp(ma).get('read')
        stop_uplink(proc); proc = None
        daemon.reconcile_native_receipt('chat', dict(native, id='native-a', seen=1234567890000))
        daemon.mx = lambda *args, **kwargs: (_ for _ in ()).throw(OSError('synthetic outage'))
        daemon.publish_native_receipts()
        daemon.initialize(cfg, state_dir=state, runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError('native resend')))
        daemon.mx = lambda method, endpoint, content, **kw: local(bot_token, method, endpoint, content)
        daemon.publish_native_receipts()
        proc = start_uplink(e)
        wait_until(lambda: stamp(ma).get('read'), desc='native read after daemon/uplink restart')
        assert not stamp(mb).get('read') and not stamp(mb).get('delivered')
        # Move the receipt out of the limited timeline; fresh room state must retain it.
        for index in range(8): post_msg(e['tuser_tok'], rid, 'timeline filler ' + str(index))
        room = local(e['tuser_tok'], 'GET', '/_matrix/client/v3/sync', query={
            'timeout': '0', 'filter': json.dumps({'room': {'timeline': {'limit': 1}}})})['rooms']['join'][rid]
        result = subprocess.run(['node', os.path.join(HERE, 'read_state_client.js')], input=json.dumps({
            'base': TEST_HS, 'token': e['tuser_tok'], 'roomId': rid, 'userId': e['tuser_id'],
            'action': 'parse', 'sourceId': 'imessage', 'room': room}), capture_output=True, text=True, check=True)
        checks = json.loads(result.stdout)['receipts']
        assert checks[a]['read'] and checks[a]['delivered']
        assert not checks[b]['read'] and not checks[b]['delivered']
        return True, 'native parser -> durable daemon -> local state -> uplink -> master; gray/gray/blue; exact repeated-text IDs; outage/restart; fresh UI sync'
    finally:
        if proc: stop_uplink(proc)
        daemon.DB.close()


def scenario_28_crm_metadata():
    """Real manager bootstrap and Matrix authorization on the private CRM log."""
    from http.server import ThreadingHTTPServer
    import threading
    from unittest.mock import patch
    sys.path.insert(0, REPO)
    from master import enroll, crm

    server = ThreadingHTTPServer(("127.0.0.1", 0), enroll._make_handler())
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:%d" % server.server_port
    evidence = []

    def bootstrap(token):
        return enroll._request("POST", origin + "/admin/crm-room",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, data=b"{}")

    def denied(token, method, path, body=None, statuses=(403,)):
        try:
            master(token, method, path, body)
        except MxError as error:
            assert error.code in statuses, (method, path, error.code)
            return
        raise AssertionError("Unauthorized Matrix action accepted: %s %s" % (method, path))

    try:
        with patch.object(enroll, "_cs_base", return_value=MASTER_HS), patch.object(
                enroll, "_manager_mxid", return_value=MASTER_MANAGER_USER):
            # Actual whoami checks against the disposable server; no stubbed auth.
            status, _ = bootstrap("invalid-fixture-token")
            assert status == 401, status
            for token in (MASTER_ALICE_TOKEN, MASTER_BOB_TOKEN):
                status, _ = bootstrap(token)
                assert status == 403, status
            status, raw = bootstrap(MASTER_MANAGER_TOKEN)
            assert status == 200, (status, raw)
            first = json.loads(raw)
            assert first["manager_mxid"] == MASTER_MANAGER_USER
            assert first["schema_version"] == 1
            rid = first["room_id"]
            status, raw = bootstrap(MASTER_MANAGER_TOKEN)
            assert status == 200 and json.loads(raw) == first, (status, raw)
            evidence.append("authenticated repeated bootstrap returns one room")
            client = subprocess.run([os.environ.get("CONSENT_NODE", "node"),
                os.path.join(HERE, "crm_store_client.js")], input=json.dumps({
                    "base": MASTER_HS, "enrollBase": origin, "token": MASTER_MANAGER_TOKEN,
                    "managerMxid": MASTER_MANAGER_USER, "roomId": rid,
                }), text=True, capture_output=True, timeout=60)
            assert client.returncode == 0, "CRM store client failed: " + client.stderr
            result = json.loads(client.stdout)
            assert result["pass"] is True
            evidence.append("actual JS store " + result["checks"] + "; pages=" + str(result["historyPages"])
                            + "; last-page=" + json.dumps(result["finalPage"]))

        roompath = "/_matrix/client/v3/rooms/" + urllib.parse.quote(rid, safe="")
        state = master(MASTER_MANAGER_TOKEN, "GET", roompath + "/state")
        crm.verify_room_state(state, MASTER_MANAGER_USER)
        alias = "#beepa-crm-v1:" + MASTER_MANAGER_USER.split(":", 1)[1]
        resolved = master(MASTER_MANAGER_TOKEN, "GET", "/_matrix/client/v3/directory/room/"
                          + urllib.parse.quote(alias, safe=""))
        assert resolved["room_id"] == rid
        for token in (MASTER_ALICE_TOKEN, MASTER_BOB_TOKEN):
            denied(token, "GET", roompath + "/state")
            denied(token, "GET", roompath + "/messages?dir=b&limit=10")
            denied(token, "POST", roompath + "/join", {})
            denied(token, "PUT", roompath + "/send/com.jkali.crm.operation/" + uniq("foreign"),
                   {"version": 1, "operation_id": "forged", "body": "foreign"})
        evidence.append("both scoped owners denied state/history/join/typed writes")

        for typ in ("m.room.message", "m.room.encrypted", "com.jkali.unapproved"):
            denied(MASTER_MANAGER_TOKEN, "PUT", roompath + "/send/" + typ + "/" + uniq("denied"),
                   {"msgtype": "m.text", "body": "must not send", "algorithm": "fixture"})
        denied(MASTER_MANAGER_TOKEN, "POST", roompath + "/invite", {"user_id": MASTER_ALICE_USER})
        payload = {"schema": 1, "op_id": uniq("crm-operation"), "workspace_id": rid,
                   "type": "list.create", "entity_id": uniq("crm-list"), "base_revision": 0,
                   "payload": {"name": "Synthetic cross-account list"}}
        txn = uniq("crmop")
        sent = master(MASTER_MANAGER_TOKEN, "PUT", roompath + "/send/com.jkali.crm.operation/" + txn, payload)
        repeated = master(MASTER_MANAGER_TOKEN, "PUT", roompath + "/send/com.jkali.crm.operation/" + txn, payload)
        assert sent["event_id"] == repeated["event_id"]
        event = master(MASTER_MANAGER_TOKEN, "GET", roompath + "/event/" + urllib.parse.quote(sent["event_id"], safe=""))
        assert event["type"] == "com.jkali.crm.operation" and event["content"] == payload
        assert event["sender"] == MASTER_MANAGER_USER
        evidence.append("manager typed operation accepted/idempotent; chat/encrypted/unknown/invite denied")
        # Event reads must also remain inaccessible, even given an exact event ID.
        denied(MASTER_ALICE_TOKEN, "GET", roompath + "/event/" + urllib.parse.quote(sent["event_id"], safe=""),
               statuses=(403, 404))
        return True, "; ".join(evidence)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def scenario_29_crm_ordered_controls():
    """Real Synapse policy rooms, command polling, CAS and disconnected privacy.

    Only provider evidence is synthetic: source room IDs are created by this
    disposable harness and the strict resolver seam returns their pinned proof.
    All auth, capabilities, events, journal writes and acknowledgments are real.
    """
    sys.path.insert(0, os.path.join(REPO, "agents", "uplink"))
    import uplink as uplink_module
    import crm_control
    import crm_batch
    from crm_resolver_sql import run_fixture
    from crm_provider_history_sql import run_fixture as run_history_fixture
    def fixture_sql(sql):
        # SANDBOX was validated against the marked disposable manifest before
        # scenario selection; never discover an installed bridge database.
        return subprocess.run(["docker", "exec", "-i", SANDBOX["project"] + "-local-db-1",
            "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", "matrix", "-d", "synapse"],
            input=sql, text=True, capture_output=True, check=True).stdout
    provider_counts = run_fixture(fixture_sql)
    history_counts = run_history_fixture(fixture_sql)
    e = fresh_env("s29")
    space = create_space(e["tuser_tok"], "iMessage")
    target = make_convo(e, space, "Synthetic CRM contact")
    set_override(e["tuser_tok"], e["tuser_id"], target, "private")
    cfg = uplink_module.Config({
        "LOCAL_HS_URL": TEST_HS, "LOCAL_USER": e["tuser_id"], "LOCAL_TOKEN": e["tuser_tok"],
        "MASTER_HS_URL": MASTER_HS, "MASTER_USER": MASTER_ALICE_USER, "MASTER_TOKEN": MASTER_ALICE_TOKEN,
        "MANAGER_MXID": MASTER_MANAGER_USER, "MASTER_SPACE": MASTER_SPACE_ALICE,
        "UPLINK_DB": e["db_path"], "UPLINK_CONTACTS_DB": os.path.join(os.path.dirname(e["db_path"]), "synthetic-contacts.db"),
    })
    worker = uplink_module.Uplink(cfg)
    worker._conn_state = False
    try:
        worker.crm_local_once()
        controller = worker._crm_control
        policy_room = controller._meta("local_room")
        initial_epoch = controller.epoch
        cap = local(e["tuser_tok"], "GET", "/_matrix/client/v3/user/" + urllib.parse.quote(e["tuser_id"], safe="") + "/account_data/" + crm_control.LOCAL_CAPABILITY)
        assert cap["manual"] and not cap["crm"] and cap["policy_room_id"] == policy_room
        def post_local(command, level, revision):
            payload = {"schema": 1, "command_id": command, "journal_epoch": controller.epoch,
                       "room_id": target, "level": level, "expected_revision": revision}
            local(e["tuser_tok"], "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(policy_room, safe="") +
                  "/send/" + crm_control.POLICY_REQUEST + "/" + uniq("policy"), payload)
            worker.crm_local_once()
            return local(e["tuser_tok"], "GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(policy_room, safe="") +
                         "/state/" + crm_control.POLICY_RESULT + "/" + command)
        assert post_local("offline-private", "private", 0)["state"] == "applied"
        assert get_override_raw(e["tuser_tok"], e["tuser_id"], target)["state"] == "private"
        worker._conn_state = True
        worker.crm_local_once()
        worker.ensure_proposal_rooms()
        proposal_room = worker.meta_get("master_proposals_room")
        master(MASTER_MANAGER_TOKEN, "POST", "/_matrix/client/v3/join/" + urllib.parse.quote(proposal_room, safe=""), {})
        proof = {"room_id": target, "source": "imessage", "native_id": "synthetic-contact", "bridge_epoch": "synthetic-fixture-1"}
        def resolve(binding, owner_mxid, generation):
            assert binding == {"contact": {"source": "imessage", "network_id": "synthetic@example.invalid"}, "contact_version": "1"}
            assert owner_mxid == MASTER_ALICE_USER and generation == controller.generation
            return {"status": "ready", "rooms": [proof]}
        controller.resolve_contact = resolve
        worker.crm_master_once()
        def direct(command, phase="prepare", ticket=None, drain=True):
            payload = {"schema": 1, "command_id": command, "phase": phase, "owner_mxid": MASTER_ALICE_USER,
                "generation": controller.generation, "journal_epoch": controller.epoch, "binding_id": "fixture-binding",
                "contact": {"source": "imessage", "network_id": "synthetic@example.invalid"}, "contact_version": "1", "membership_operation_id": command}
            if ticket:
                payload["ticket_id"] = ticket
            master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="") +
                   "/send/" + crm_control.DIRECT_REQUEST + "/" + uniq("direct"), payload)
            if not drain:
                return None
            worker.crm_master_once()
            return master(MASTER_MANAGER_TOKEN, "GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="") +
                          "/state/" + crm_control.DIRECT_RESULT + "/" + command)
        prepared = direct("race")
        assert prepared["state"] == "prepared" and get_override_raw(e["tuser_tok"], e["tuser_id"], target)["state"] == "private"
        assert post_local("new-private", "private", 1)["state"] == "applied"
        raced = direct("race", "commit", prepared["ticket_id"])
        assert raced["rooms"][0]["reason"] == "revision-conflict"
        assert get_override_raw(e["tuser_tok"], e["tuser_id"], target)["state"] == "private"
        prepared = direct("fresh-grant")
        applied = direct("fresh-grant", "commit", prepared["ticket_id"])
        assert applied["state"] == "applied" and applied["journal_epoch"] == controller.epoch
        assert get_override_raw(e["tuser_tok"], e["tuser_id"], target)["state"] == "direct"
        assert post_local("revoke", "private", 3)["state"] == "applied"
        repeated = direct("fresh-grant", "commit", prepared["ticket_id"])
        assert repeated["state"] == "applied"
        assert get_override_raw(e["tuser_tok"], e["tuser_id"], target)["state"] == "private"
        canceled_applied = direct("fresh-grant", "cancel")
        assert canceled_applied["state"] == "applied" and canceled_applied["safe_to_change_membership"]
        cancel_ticket = direct("cancel-before-write")["ticket_id"]
        direct("cancel-before-write", "commit", cancel_ticket, drain=False)
        canceled = direct("cancel-before-write", "cancel")
        assert canceled["state"] == "canceled" and canceled["safe_to_change_membership"]
        assert direct("cancel-before-write", "commit", cancel_ticket)["state"] == "canceled"
        assert get_override_raw(e["tuser_tok"], e["tuser_id"], target)["state"] == "private"
        batch_grant = direct("batch-member")
        assert direct("batch-member", "commit", batch_grant["ticket_id"])["state"] == "applied"
        controller.batch_enabled = True  # isolated protocol test, production remains gated
        worker.crm_master_once()
        worker.reconcile()
        assert worker.mirror_for(target), "batch requires the real positive D2 mirror gate"
        parent_id = "isolated-batch"
        child_id = "child-" + crm_batch.digest([parent_id, MASTER_ALICE_USER, controller.generation])
        item = {"item_id": "isolated-item", "person_id": "fixture-person", "binding_id": "fixture-binding",
                "binding_revision": 1, "membership_revision": 1, "membership_operation_id": "batch-member", "contact_version": "1",
                "owner_mxid": MASTER_ALICE_USER, "generation": controller.generation, "child_id": child_id,
                "proposal_room_id": proposal_room, "body": "Synthetic CRM reviewed batch message",
                "route": {"mode": "direct_existing", "source": "imessage", "network_id": "synthetic@example.invalid", "room_id": target, "consent_revision": 5}}
        batch_payload = {"schema": 1, "phase": "submit", "owner_mxid": MASTER_ALICE_USER, "generation": controller.generation,
                         "journal_epoch": controller.epoch, "batch_id": parent_id, "child_id": child_id, "workspace_id": "!synthetic-crm:master",
                         "payload_hash": "a" * 64, "reviewed_at": int(time.time() * 1000), "parent_item_count": 1, "items": [item]}
        batch_payload["child_hash"] = crm_batch.digest(crm_batch.child_payload(batch_payload))
        staged = master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="")
                        + "/send/com.jkali.proposal/" + uniq("batch-stage"), {"body": item["body"], "target_room": target,
                          crm_batch.ITEM_KEY: controller.batch.annotation(batch_payload, item)})["event_id"]
        item["proposal_event_id"] = staged
        staged_event = master(MASTER_MANAGER_TOKEN, "GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="") + "/event/" + urllib.parse.quote(staged, safe=""))
        worker.forward_proposals(proposal_room, worker.meta_get("local_proposals_room"), [staged_event], cold_start=False, suspended=False)
        assert not [ev for ev in local_events_of_type(e["tuser_tok"], target, "m.room.message") if ev.get("content", {}).get("body") == item["body"]]
        def submit_batch(txn):
            master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="")
                   + "/send/" + crm_batch.BATCH_REQUEST + "/" + txn, batch_payload)
            worker.crm_master_once()
            return master(MASTER_MANAGER_TOKEN, "GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="")
                          + "/state/" + crm_batch.BATCH_RESULT + "/" + child_id)
        batch_result = submit_batch("batch-submit-one")
        assert batch_result["items"][0]["state"] == "sent", batch_result
        sent_id = batch_result["items"][0]["sent_event_id"]
        sent = local(e["tuser_tok"], "GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(target, safe="") + "/event/" + urllib.parse.quote(sent_id, safe=""))
        assert sent["content"]["body"] == item["body"] and sent["sender"] == e["tuser_id"]
        assert submit_batch("batch-submit-replay")["items"][0]["sent_event_id"] == sent_id
        assert len([ev for ev in local_events_of_type(e["tuser_tok"], target, "m.room.message") if ev.get("content", {}).get("body") == item["body"]]) == 1
        # Native evidence is the only injected seam: actual local reply/read
        # events and their exact mirrored event IDs traverse the real archive.
        import types
        incoming = post_msg(e["contact_tok"], target, "Synthetic next incoming response")
        if isinstance(incoming, dict):
            incoming = incoming["event_id"]
        local(e["contact_tok"], "POST", "/_matrix/client/v3/rooms/" + urllib.parse.quote(target, safe="")
              + "/receipt/m.read/" + urllib.parse.quote(sent_id, safe=""), {})
        native_snapshot = {"window_complete": True, "has_more": False, "room_id": target,
            "source": "imessage", "bridge_epoch": "synthetic-native-epoch",
            "outgoing_parts": {sent_id: ["native-outgoing"]}, "events": [
                {"native_id": "native-outgoing", "order": "100", "timestamp": 1000, "incoming": False, "event_id": None},
                {"native_id": "native-incoming", "order": "200", "timestamp": 2000, "incoming": True, "event_id": incoming}]}
        worker._crm_native_history = types.SimpleNamespace(snapshot=lambda *args, **kwargs: native_snapshot)
        worker.tail_once()
        worker.deliver_pending()
        controller.tracking.update()
        controller.batch.flush()
        tracking = master(MASTER_MANAGER_TOKEN, "GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="")
                          + "/state/" + crm_batch.BATCH_RESULT + "/" + child_id)["items"][0]["tracking"]
        assert tracking["response"]["state"] == "replied" and tracking["response"]["incoming_event_id"] == incoming, tracking
        assert tracking["delivery"]["state"] == "read" and tracking["delivery"]["sent_event_id"] == sent_id, tracking
        assert tracking["response"]["mirror_event_id"] and tracking["delivery"]["mirror_event_id"], tracking
        master(MASTER_MANAGER_TOKEN, "POST", "/_matrix/client/v3/join/" + urllib.parse.quote(tracking["delivery"]["mirror_room_id"], safe=""), {})
        for projection in (tracking["response"], tracking["delivery"]):
            mirrored = master(MASTER_MANAGER_TOKEN, "GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(projection["mirror_room_id"], safe="")
                              + "/event/" + urllib.parse.quote(projection["mirror_event_id"], safe=""))
            assert mirrored["type"] == "m.room.message"
        # A real self-authored control event approves a filed CRM draft. The
        # browser never sends the conversation message; the existing dispatcher
        # records the exact approved result once, even on repeated approval.
        approved_payload = json.loads(json.dumps(batch_payload))
        approved_payload["batch_id"] = "isolated-approved-batch"
        approved_child = "child-" + crm_batch.digest([approved_payload["batch_id"], MASTER_ALICE_USER, controller.generation])
        approved_payload["child_id"] = approved_child
        approved_payload["reviewed_at"] = int(time.time() * 1000)
        approved_item = approved_payload["items"][0]
        approved_item.update(child_id=approved_child, item_id="approved-item", body="Synthetic CRM awaiting approval")
        approved_item.pop("proposal_event_id", None)
        approved_payload["child_hash"] = crm_batch.digest(crm_batch.child_payload(approved_payload))
        approved_item["proposal_event_id"] = master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="")
            + "/send/com.jkali.proposal/" + uniq("approval-stage"), {"body": approved_item["body"], "target_room": target,
            crm_batch.ITEM_KEY: controller.batch.annotation(approved_payload, approved_item)})["event_id"]
        worker.cfg.direct_send_cap = 0
        master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="")
               + "/send/" + crm_batch.BATCH_REQUEST + "/" + uniq("approval-submit"), approved_payload)
        worker.crm_master_once()
        awaiting = controller.batch.result(approved_child)["items"][0]
        assert awaiting["state"] == "refused" and awaiting["local_proposal_event_id"], awaiting
        artifact = local(e["tuser_tok"], "GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(meta_get(e["db_path"], "local_proposals_room"), safe="") + "/event/" + urllib.parse.quote(awaiting["local_proposal_event_id"], safe=""))
        assert artifact["content"]["com.jkali.direct_refused"] is True
        worker.cfg.direct_send_cap = 20
        worker.crm_master_once()
        assert controller.batch.result(approved_child)["items"][0]["state"] == "refused"
        assert not [ev for ev in local_events_of_type(e["tuser_tok"], target, "m.room.message") if ev.get("content", {}).get("body") == approved_item["body"]]
        native_snapshot["window_complete"] = False
        controller.tracking.update()
        assert controller.batch.result(child_id)["items"][0]["tracking"]["response"]["state"] == "reconciling"
        controller.batch_enabled = False
        cancel_payload = {key: value for key, value in batch_payload.items() if key in {"schema", "owner_mxid", "generation", "journal_epoch", "batch_id", "child_id", "child_hash"}}
        cancel_payload["phase"] = "cancel"
        master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="")
               + "/send/" + crm_batch.BATCH_REQUEST + "/" + uniq("disabled-cancel"), cancel_payload)
        worker.crm_master_once()
        assert controller.batch.result(child_id)["state"] == "canceled"
        assert controller.batch.result(child_id)["items"][0]["state"] == "sent"
        assert post_local("after-batch-private", "private", 5)["state"] == "applied"
        masked = controller.batch.result(child_id)["items"][0]["tracking"]["response"]
        assert masked["state"] == "reconciling" and masked["incoming_event_id"] is None, masked
        hidden_delivery = controller.batch.result(child_id)["items"][0]["tracking"]["delivery"]
        assert hidden_delivery["mirror_event_id"] is None and hidden_delivery["mirror_room_id"] is None, hidden_delivery
        # Approved new-chat acceptance later discovers an exact portal and
        # completes the original list permission, without inventing send IDs.
        controller.batch_enabled = True
        discovered = []
        original_resolve = controller.resolve_contact
        def resolve_new(binding, owner_mxid, generation):
            if binding["contact"]["network_id"] == "new-contact@example.invalid":
                assert owner_mxid == MASTER_ALICE_USER and generation == controller.generation
                return {"status": "ready" if discovered else "no_existing_conversation", "rooms": list(discovered)}
            return original_resolve(binding, owner_mxid, generation)
        controller.resolve_contact = resolve_new
        member_request = {"schema": 1, "command_id": "new-chat-member", "phase": "prepare", "owner_mxid": MASTER_ALICE_USER,
            "generation": controller.generation, "journal_epoch": controller.epoch, "binding_id": "new-binding",
            "contact": {"source": "imessage", "network_id": "new-contact@example.invalid"}, "contact_version": "1", "membership_operation_id": "new-chat-member"}
        master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="")
               + "/send/" + crm_control.DIRECT_REQUEST + "/" + uniq("new-member"), member_request)
        worker.crm_master_once()
        assert json.loads(controller._command("new-chat-member")["result_json"])["state"] == "approval_required"
        new_payload = json.loads(json.dumps(batch_payload))
        new_payload.update(batch_id="isolated-new-chat", reviewed_at=int(time.time() * 1000))
        new_child = "child-" + crm_batch.digest([new_payload["batch_id"], MASTER_ALICE_USER, controller.generation])
        new_payload["child_id"] = new_child
        new_item = new_payload["items"][0]
        new_item.update(child_id=new_child, item_id="new-chat-item", binding_id="new-binding", membership_operation_id="new-chat-member", body="Synthetic user-approved new conversation",
                        route={"mode":"request_new_conversation_approval","source":"imessage","network_id":"new-contact@example.invalid","consent_revision":0})
        new_item.pop("proposal_event_id", None)
        new_payload["child_hash"] = crm_batch.digest(crm_batch.child_payload(new_payload))
        new_item["proposal_event_id"] = master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="")
            + "/send/com.jkali.proposal/" + uniq("new-chat-stage"), {"body":new_item["body"],"target_source":"imessage","target_identifier":"new-contact@example.invalid",
                crm_batch.ITEM_KEY:controller.batch.annotation(new_payload,new_item)})["event_id"]
        master(MASTER_MANAGER_TOKEN, "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="")
               + "/send/" + crm_batch.BATCH_REQUEST + "/" + uniq("new-chat-submit"), new_payload)
        worker.crm_master_once()
        new_row = controller.batch.result(new_child)["items"][0]
        assert new_row["state"] == "waiting_for_approval"
        management = create_dm(e["tuser_tok"], "Synthetic native management", invite=[e["contact_id"]])
        join_room(e["contact_tok"], management)
        local(e["tuser_tok"], "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(management, safe="") + "/state/m.room.power_levels/",
              {"users":{e["tuser_id"]:100,e["contact_id"]:50},"state_default":50,"events_default":0})
        worker._crm_contact_reader = types.SimpleNamespace(read_connections=lambda source: {"management_room":management,"bot_mxid":e["contact_id"],"local_owner_mxid":e["tuser_id"]})
        new_approval = {"schema":1,"child_id":new_child,"item_id":"new-chat-item","local_proposal_event_id":new_row["local_proposal_event_id"],
                        "journal_epoch":controller.epoch,"decision":"approve","body":new_item["body"]}
        local(e["tuser_tok"], "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(policy_room, safe="") + "/send/com.jkali.crm.approval_request/" + uniq("new-chat-approve"), new_approval)
        worker.crm_local_once()
        new_row = controller.batch.result(new_child)["items"][0]
        command_id = new_row["native_command_event_id"]
        assert command_id and new_row["state"] == "outcome_unknown" and new_row["sent_event_id"] is None, new_row
        local(e["contact_tok"], "PUT", "/_matrix/client/v3/rooms/" + urllib.parse.quote(management, safe="") + "/state/com.jkali.imessage_start_chat/" + urllib.parse.quote(command_id, safe=""),
              {"schema":1,"command_event_id":command_id,"owner_mxid":e["tuser_id"],"status":"accepted","correlation":"unavailable","native_ids":[],"message_event_ids":[]})
        controller.batch_enabled = False
        worker.crm_local_once()
        assert json.loads(controller._command("new-chat-member")["result_json"])["state"] == "approval_required"
        new_room = make_convo(e, space, "Synthetic newly discovered CRM contact")
        discovered.append({"room_id":new_room,"source":"imessage","native_id":"new-contact@example.invalid","bridge_epoch":"synthetic-fixture-new"})
        worker.crm_local_once()
        worker.crm_master_once()
        activated = master(MASTER_MANAGER_TOKEN, "GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(proposal_room, safe="") + "/state/" + crm_control.DIRECT_RESULT + "/new-chat-member")
        assert activated["state"] == "applied" and activated["rooms"][0]["room_id"] == new_room, activated
        assert get_override_raw(e["tuser_tok"],e["tuser_id"],new_room)["state"] == "direct"
        assert controller.batch.result(new_child)["items"][0]["sent_event_id"] is None
        worker.crm_local_once(); worker.crm_master_once()
        assert len(local_events_of_type(e["tuser_tok"],management,"m.room.message")) == 1
        count = worker.db.execute("SELECT count(*) FROM crm_policy_operations").fetchone()[0]
        worker.db.close()
        worker = uplink_module.Uplink(cfg)
        worker._conn_state = False
        worker.crm_local_once()
        assert worker._crm_control.epoch == initial_epoch
        assert worker.db.execute("SELECT count(*) FROM crm_policy_operations").fetchone()[0] == count
        assert get_override_raw(e["tuser_tok"], e["tuser_id"], target)["state"] == "private"
        return True, "real policy/control and staged CRM batch events; offline manual Private; grant CAS/cancel; staged proposals inert; accepted child sends once via unchanged D2, exact sent ID and duplicate replay; exact read/reply projection and mirror navigation, incomplete-window reconciliation, typed self-approval/edit/replay, disabled cancellation, accepted new-chat portal activates original Direct grant without send IDs; restart epochs/fences preserved; PostgreSQL strict provider joins " + json.dumps(provider_counts, sort_keys=True) + "; observed provider history " + json.dumps(history_counts, sort_keys=True)
    finally:
        worker.db.close()


def scenario_30_importer_retry_recovery():
    """Real Matrix transactions and uplink; synthetic native reads, no Apple sends."""
    import importlib.util
    from unittest.mock import patch
    e = fresh_env('s30')
    space = create_space(e['tuser_tok'], 'iMessage')
    rid = make_convo(e, space, 'Importer recovery')
    bot_id, bot_token = register_user('imessagebot')
    room_path = '/_matrix/client/v3/rooms/' + urllib.parse.quote(rid, safe='')
    local(e['tuser_tok'], 'POST', room_path + '/invite', {'user_id': bot_id})
    join_room(bot_token, rid)
    set_override(e['tuser_tok'], e['tuser_id'], rid, 'share')
    spec = importlib.util.spec_from_file_location('importer_recovery', os.path.join(REPO, 'imessage/daemon.py'))
    d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
    state = os.path.join(os.path.dirname(e['db_path']), 'importer-recovery')
    cfg = dict(port=29350, hs_url=TEST_HS, domain='localhost', user_id=e['tuser_id'], bot_id=bot_id,
               as_token='synthetic', hs_token='synthetic', cli_path='/never-execute',
               attachment_allow_prefixes=[], backfill_count=0, poll_message_budget=1)
    def forbidden(*args, **kwargs): raise AssertionError('native mutation attempted')
    d.initialize(cfg, state_dir=state, runner=forbidden)
    d.map_add('chat', rid)
    d.ensure_portal = lambda *args: rid
    messages = [dict(id='native-'+str(i), threadID='chat', isSender=True, text='import-retry-'+str(i)) for i in range(3)]
    recovered = dict(id='older', threadID='chat', isSender=True, text='import-recovered-older')
    d.cli_json = lambda command, *args, **kwargs: {'items': messages} if command == 'messages' else recovered
    attempts = []
    def matrix(method, endpoint, content, **kwargs):
        result = local(bot_token, method, endpoint, content)
        attempts.append(endpoint)
        if len(attempts) == 1: raise OSError('accepted by Matrix, response lost')
        return result
    d.mx = matrix
    proc = None
    try:
        for _ in range(3): d.handle_chat_delta({}, 'chat')
        assert len(attempts) == 3, 'failed head must not starve other messages'
        d.initialize(cfg, state_dir=state, runner=forbidden)
        d.handle_chat_delta({}, 'chat')
        assert len(attempts) == 3, 'restart must preserve retry cooldown'
        with patch.object(d.time, 'time', return_value=time.time()+16):
            assert d.handle_chat_delta({}, 'chat')
        assert attempts[0] == attempts[-1], 'lost response retry must reuse Matrix transaction'
        d.inbound_pending('chat', 'older')
        assert not d.handle_chat_delta({}, 'chat')
        assert d.health_state()['inbound_source_unavailable'] == 1
        d.recover_pending_sources()
        assert d.health_state()['inbound_pending'] == 0
        proc = start_uplink(e)
        mr = wait_until(lambda: mirror_of(e['db_path'], rid), desc='importer recovery mirror')[0]
        expected = [m['text'] for m in messages] + [recovered['text']]
        def complete():
            bodies = [m['body'] for m in master_messages(mr)]
            return bodies if all(body in bodies for body in expected) else None
        bodies = wait_until(complete, desc='recovered imports delivered to master')
        assert all(bodies.count(body) == 1 for body in expected), 'no duplicated accepted components'
        return True, 'failed head rotated; cooldown survives restart; exact-ID tail recovery; lost response dedup; four imports reach master once; no native sends'
    finally:
        if proc: stop_uplink(proc)
        d.DB.close()


def scenario_31_native_unread_roundtrip():
    """Two real homeservers, real source agent/daemon handlers; synthetic Apple CLI."""
    import importlib.util
    e = fresh_env('s31')
    space = create_space(e['tuser_tok'], 'iMessage')
    rid = make_convo(e, space, 'Native unread roundtrip')
    bot_id, bot_token = register_user('imessagebot')
    path = '/_matrix/client/v3/rooms/' + urllib.parse.quote(rid, safe='')
    local(e['tuser_tok'], 'POST', path + '/invite', {'user_id':bot_id}); join_room(bot_token,rid)
    power = local(e['tuser_tok'],'GET',path+'/state/m.room.power_levels')
    power.setdefault('users',{})[bot_id] = 100
    local(e['tuser_tok'],'PUT',path+'/state/m.room.power_levels',power)
    set_override(e['tuser_tok'],e['tuser_id'],rid,'share')
    post_msg(e['contact_tok'],rid,'Synthetic unread source message')
    spec=importlib.util.spec_from_file_location('unread_daemon',os.path.join(REPO,'imessage/daemon.py'))
    d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
    def forbidden(*a,**k): raise AssertionError('real native executable forbidden')
    d.initialize(dict(port=29350,hs_url=TEST_HS,domain='localhost',user_id=e['tuser_id'],bot_id=bot_id,
        as_token='synthetic',hs_token='synthetic',cli_path='/never-execute',attachment_allow_prefixes=[],backfill_count=0),
        state_dir=os.path.join(os.path.dirname(e['db_path']),'native-unread'),runner=forbidden)
    d.map_add('chat',rid)
    chat=dict(id='chat',unreadCount=0,isUnread=False,isMarkedUnread=False,timestamp=1000,lastReadMessageSortKey='1000',
              messages={'items':[dict(id='latest',sortKey='1000',timestamp=1000)]})
    mutations=[]
    def cli(command,*args,**kwargs):
        assert args == ('chat',)
        if command=='mark-read': mutations.append(command);chat.update(unreadCount=0,isUnread=False,isMarkedUnread=False)
        elif command=='mark-unread': mutations.append(command);chat.update(isUnread=True,isMarkedUnread=True)
        else: assert command=='chat'
        return dict(chat) if command=='chat' else True
    d.cli_json=cli
    d.cli_read_change=lambda action,chat_id,**kw:cli('mark-'+action,chat_id)
    d.mx=lambda method,endpoint,content,**kw:local(bot_token,method,endpoint,content)
    d.publish_native_read_state(chat)
    proc=start_uplink(e)
    try:
        mr=wait_until(lambda:mirror_of(e['db_path'],rid),desc='native unread mirror')[0]
        def meta(key):
            with sqlite3.connect(e['db_path']) as db:
                row=db.execute('SELECT v FROM meta WHERE k=?',(key,)).fetchone()
                return row[0] if row else None
        pr=wait_until(lambda:meta('master_proposals_room'),desc='read control channel')
        wait_until(lambda:meta('proposal_sync_since'),desc='read control initial sync')
        prpath='/_matrix/client/v3/rooms/'+urllib.parse.quote(pr,safe='')
        master(MASTER_MANAGER_TOKEN,'POST',prpath+'/join',{})
        statepath='/_matrix/client/v3/rooms/'+urllib.parse.quote(mr,safe='')+'/state/com.jkali.read_state/'
        def state():
            try:return master(MASTER_ALICE_TOKEN,'GET',statepath)
            except MxError as exc:
                if exc.code==404:return {}
                raise
        wait_until(lambda:state().get('native'),desc='native own-read projection')
        assert state()['unread_count']==0, 'Apple-read replaces Matrix unread backlog'
        def ready_capability():
            try:
                cap=master(MASTER_ALICE_TOKEN,'GET',prpath+'/state/com.jkali.crm.capability/')
                return cap if cap.get('native_read') is True else None
            except MxError as exc:
                if exc.code==404:return None
                raise
        capability=wait_until(ready_capability,desc='native read capability published')
        for action in ['unread','read']:
            command_id='native-'+action
            payload={'target_room':rid,'com.jkali.read_action':dict(schema=1,command_id=command_id,action=action,
                expected_revision=state()['native']['revision'],expires_at=int(time.time()*1000)+120000,
                generation=capability['generation'],mirror_room=mr)}
            master(MASTER_MANAGER_TOKEN,'PUT',prpath+'/send/com.jkali.read_request/'+command_id,payload)
            def command_event():
                events=local(e['tuser_tok'],'GET',path+'/messages',query={'dir':'b','limit':'30'})['chunk']
                return next((ev for ev in events if ev.get('type')==d.NATIVE_READ_REQUEST and ev.get('content',{}).get('command_id')==command_id),None)
            ev=wait_until(command_event,desc='manager metadata forwarded '+action)
            ev['room_id']=rid
            assert d.process_outbound_event(ev)
            assert d.process_outbound_event(ev)
            wait_until(lambda:state().get('native',{}).get('last_command',{}).get('id')==command_id,desc='native confirmation returned '+action)
            assert state()['native']['last_command']['status']=='applied'
            assert state()['unread_count']==(1 if action=='unread' else 0)
        assert mutations==['mark-unread','mark-read'], 'no native replay'
        stop_uplink(proc);proc=start_uplink(e)
        post_msg(e['contact_tok'],rid,'Synthetic late imported history')
        wait_until(lambda:any(m['body']=='Synthetic late imported history' for m in master_messages(mr)),desc='late history delivered')
        assert state()['unread_count']==0, 'Matrix import cannot resurrect native unread after restart'
        return True,'Apple-read overrides imported backlog; manager unread/read round trip; native confirmation; replay protection; restart retains native authority; no native sends'
    finally:
        stop_uplink(proc);d.DB.close()


SCENARIOS = [
    ("1_share_one_conversation", scenario_1_share_one),
    ("2_new_local_message", scenario_2_new_message),
    ("3_offline_online_catchup", scenario_3_offline_catchup),
    ("4_master_offline_buffer", scenario_4_master_offline),
    ("5_explicit_share_and_migration", scenario_5_explicit_share_and_migration),
    ("6_revoke_each_level", scenario_6_revoke_levels),
    ("7_read_only_manager", scenario_7_read_only),
    ("8_cross_user_isolation", scenario_8_cross_user_isolation),
    ("9_media_reupload", scenario_9_media_reupload),
    ("10_proposal_down", scenario_10_proposal_down),
    ("11_profile_span_platforms", scenario_11_profile_span_platforms),
    ("12_contact_share_and_propose", scenario_12_contact_share_and_propose),
    ("13_contact_backfill_on_enable", scenario_13_contact_backfill_on_enable),
    ("14_direct_proposal_autosend", scenario_14_direct_proposal_autosend),
    ("15_revocation_retains_history", scenario_15_revocation_retains_history),
    ("16_original_timestamps", scenario_16_original_timestamps),
    ("17_discord_dms", scenario_17_discord_dms),
    ("18_daemon_joins_invite_no_mirror", scenario_18_daemon_joins_invite_no_mirror),
    ("19_schedule_fires_once_across_restart", scenario_19_schedule_fires_once_across_restart),
    ("20_schedule_held_when_conversation_moves_on", scenario_20_schedule_held_when_conversation_moves_on),
    ("21_cancel_before_send_at", scenario_21_cancel_before_send_at),
    ("22_schedule_fires_with_master_unreachable", scenario_22_schedule_fires_with_master_unreachable),
    ("23_manager_timed_direct_superseded", scenario_23_manager_timed_direct_superseded),
    ("24_read_unread", scenario_24_read_unread),
    ("25_receipt_outage_recovery", scenario_25_receipt_outage_recovery),
    ("26_message_checks", scenario_26_message_checks),
    ("27_native_imessage_receipts", scenario_27_native_imessage_receipts),
    ("28_crm_metadata", scenario_28_crm_metadata),
    ("29_crm_ordered_controls", scenario_29_crm_ordered_controls),
    ("30_importer_retry_recovery", scenario_30_importer_retry_recovery),
    ("31_native_unread_roundtrip", scenario_31_native_unread_roundtrip),
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
