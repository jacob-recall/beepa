#!/usr/bin/env python3
"""Opt-in, self-directed LIVE send verification for iMessage / WhatsApp /
Google Messages — the three bridges that let a user message their own
number. LinkedIn / X / Instagram have no self-DM concept, so they are NOT
covered here; see tests/live/README.md for their manual checklist.

WHAT THIS DOES (only when explicitly told to)
  For each requested platform, drives the SAME management-room command path
  the hub's Directory uses (shared/ui/sources.js's resolveMgmt/sendCmd,
  PLAN-IMSG-STARTCHAT.md's start-chat), sends a unique nonce to the
  OPERATOR'S OWN handle, then polls that platform's portal room until the
  nonce round-trips back (confirming the bridge actually delivered it), or
  times out.

THIS IS NOT PART OF tests/run.sh OR CI. It sends REAL messages through
REAL bridges to REAL accounts (your own). Nothing in this file executes
unless the operator passes explicit handle flags AND the confirmation flag
below — see tests/live/README.md before running it.

Config (same shape as agents/uplink/uplink.env.local, LOCAL_* only):
  LOCAL_HS_URL, LOCAL_USER, LOCAL_TOKEN — this install's own hub, read from
  --env-file (default agents/uplink/uplink.env.local) or the environment.

Usage:
  python3 tests/live/self_send_verify.py \\
      --imessage +15551234567 --whatsapp +15551234567 \\
      --i-am-sending-to-myself

With no platform flags, prints usage and exits without sending anything.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
DEFAULT_ENV_FILE = os.path.join(REPO, "agents", "uplink", "uplink.env.local")

# Same shape as shared/ui/sources.js's PHONE_RE/EMAIL_RE + validHandle (D-2).
PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{2,24}$")

# Bot mxids, one per bridge (shared/ui/sources.js SOURCES).
BOT_MXID = {
    "imessage": "@imessagebot:localhost",
    "whatsapp": "@whatsappbot:localhost",
    "gmessages": "@gmessagesbot:localhost",
}

# Guardrail caps. These are NOT identity verification (nothing here can prove
# a handle is really the operator's own) — they only bound blast radius and
# force a deliberate, explicit opt-in per run. See tests/live/README.md.
MAX_PLATFORMS_PER_RUN = 3          # can't exceed len(BOT_MXID) anyway
DEFAULT_TIMEOUT_S = 60
DEFAULT_POLL_INTERVAL_S = 2

# A best-effort smell test for obviously-not-a-real-number input (movie/test
# placeholders, all-same-digit, strictly sequential). This is a courtesy
# check on top of --i-am-sending-to-myself, never a substitute for it.
def _looks_obviously_fake(handle):
    digits = re.sub(r"\D", "", handle)
    if len(set(digits)) == 1:
        return True  # e.g. +11111111111
    seq_up = "0123456789012345"
    seq_down = "9876543210987654"
    if digits in seq_up or digits in seq_down:
        return True
    if re.search(r"555?01\d\d$", digits):
        return True  # classic fictional 555-01xx range
    return False


class ConfigError(Exception):
    pass


def load_local_config(env_file):
    """LOCAL_HS_URL / LOCAL_USER / LOCAL_TOKEN — env wins over the file, the
    file wins over nothing. Mirrors agents/uplink/uplink.py's Config."""
    file_vals = {}
    if env_file and os.path.isfile(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if k in ("LOCAL_HS_URL", "LOCAL_USER", "LOCAL_TOKEN"):
                    file_vals[k] = v

    def resolve(k):
        return os.environ.get(k) or file_vals.get(k)

    hs, user, token = resolve("LOCAL_HS_URL"), resolve("LOCAL_USER"), resolve("LOCAL_TOKEN")
    missing = [k for k, v in (("LOCAL_HS_URL", hs), ("LOCAL_USER", user), ("LOCAL_TOKEN", token)) if not v]
    if missing:
        raise ConfigError(
            "missing local hub config: %s (set env vars or pass --env-file "
            "pointing at a file shaped like agents/uplink/uplink.env.local)" % ", ".join(missing))
    return hs.rstrip("/"), user, token


class Client:
    """Minimal Matrix CS-API client, stdlib only — same pattern as
    agents/uplink/uplink.py's _mx()."""

    def __init__(self, base, token, user):
        self.base = base
        self.token = token
        self.user = user

    def call(self, method, path, body=None, query=None, timeout=30):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, method=method, data=data)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}

    def joined_rooms(self):
        return self.call("GET", "/_matrix/client/v3/joined_rooms").get("joined_rooms", [])

    def room_state(self, room_id):
        try:
            return self.call("GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="") + "/state")
        except urllib.error.HTTPError:
            return None

    def joined_members(self, room_id):
        try:
            m = self.call("GET", "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="") + "/joined_members")
            return list((m.get("joined") or {}).keys())
        except urllib.error.HTTPError:
            return []

    def send_text(self, room_id, body):
        txn = uuid.uuid4().hex
        return self.call(
            "PUT",
            "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="") + "/send/m.room.message/" + txn,
            {"msgtype": "m.text", "body": body},
        )

    def recent_messages(self, room_id, limit=30):
        try:
            r = self.call(
                "GET",
                "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="") + "/messages",
                query={"dir": "b", "limit": str(limit)},
            )
            return r.get("chunk", [])
        except urllib.error.HTTPError:
            return []


# ---- management-room resolution (mirrors shared/ui/sources.js C-1) --------
def is_bot_dm_mgmt(client, bot_mxid, room_id):
    members = client.joined_members(room_id)
    if not (len(members) == 2 and bot_mxid in members and client.user in members):
        return False
    st = client.room_state(room_id)
    if not isinstance(st, list):
        return False  # cannot prove "not a portal" -> refuse, same as the hub
    is_portal = any(e.get("type") == "uk.half-shot.bridge" for e in st)
    is_space = any(
        e.get("type") == "m.room.create" and e.get("state_key") == ""
        and (e.get("content") or {}).get("type") == "m.space"
        for e in st
    )
    return not is_portal and not is_space


def find_bot_dm_mgmt(client, bot_mxid):
    for room_id in client.joined_rooms():
        if is_bot_dm_mgmt(client, bot_mxid, room_id):
            return room_id
    created = client.call(
        "POST", "/_matrix/client/v3/createRoom",
        {"invite": [bot_mxid], "is_direct": True, "preset": "trusted_private_chat"},
    )
    return created["room_id"]


def verify_imsg_mgmt(client, room_id):
    st = client.room_state(room_id)
    if not isinstance(st, list):
        return False
    has_marker = any(e.get("type") == "com.jkali.bridge.mgmt" and e.get("state_key") == "imessage" for e in st)
    is_portal = any(e.get("type") == "uk.half-shot.bridge" for e in st)
    return has_marker and not is_portal


def resolve_imsg_mgmt(client):
    for room_id in client.joined_rooms():
        if verify_imsg_mgmt(client, room_id):
            return room_id
    return None  # never auto-created, matching resolveImsgMgmt()


def resolve_mgmt(client, platform):
    if platform == "imessage":
        return resolve_imsg_mgmt(client)
    return find_bot_dm_mgmt(client, BOT_MXID[platform])


def verify_mgmt(client, platform, room_id):
    if platform == "imessage":
        return verify_imsg_mgmt(client, room_id)
    return is_bot_dm_mgmt(client, BOT_MXID[platform], room_id)


# ---- portal discovery + polling -------------------------------------------
def find_portal_for_handle(client, platform, handle, before):
    """A crude but workable portal finder: any joined room, not already in
    `before`, whose name/topic mentions the handle — new portals created by
    start-chat are named after the contact. Falls back to scanning all
    joined rooms' state for the handle if nothing new shows up (covers the
    "the self-chat already exists" case)."""
    after = set(client.joined_rooms())
    candidates = list(after - set(before)) or list(after)
    digits = re.sub(r"\D", "", handle)
    for room_id in candidates:
        st = client.room_state(room_id)
        if not isinstance(st, list):
            continue
        if any(e.get("type") == "uk.half-shot.bridge" for e in st):
            blob = json.dumps(st)
            if digits and digits in re.sub(r"\D", "", blob):
                return room_id
            name_ev = next((e for e in st if e.get("type") == "m.room.name"), None)
            if name_ev and handle in (name_ev.get("content") or {}).get("name", ""):
                return room_id
    return None


def poll_for_nonce(client, room_id, nonce, timeout_s, poll_interval_s, local_user):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for ev in client.recent_messages(room_id):
            body = (ev.get("content") or {}).get("body", "")
            if nonce in body and ev.get("sender") != local_user:
                return True, ev.get("sender")
        time.sleep(poll_interval_s)
    return False, None


# ---- per-platform drivers ---------------------------------------------------
def run_imessage(client, handle, nonce, timeout_s, poll_interval_s):
    mgmt = resolve_imsg_mgmt(client)
    if not mgmt:
        return False, "no verified iMessage management room found (never auto-created)"
    if not verify_imsg_mgmt(client, mgmt):
        return False, "iMessage management room failed re-verification before send"
    text = "pm_mng-self-check-" + nonce
    before = client.joined_rooms()
    client.send_text(mgmt, "start-chat " + handle + " | " + text)
    portal = None
    deadline = time.time() + timeout_s
    while time.time() < deadline and not portal:
        portal = find_portal_for_handle(client, "imessage", handle, before)
        if not portal:
            time.sleep(poll_interval_s)
    if not portal:
        return False, "no iMessage portal appeared for %s within %ss" % (handle, timeout_s)
    ok, sender = poll_for_nonce(client, portal, text, timeout_s, poll_interval_s, client.user)
    if ok:
        return True, "nonce round-tripped in portal %s (sender=%s)" % (portal, sender)
    return False, "nonce never round-tripped in portal %s within %ss" % (portal, timeout_s)


def run_bridge_start_chat(client, platform, handle, nonce, timeout_s, poll_interval_s):
    """WhatsApp / Google Messages: `start-chat <handle>` opens/finds the
    portal, then the nonce is sent as an ordinary message into it."""
    mgmt = resolve_mgmt(client, platform)
    if not mgmt:
        return False, "no %s management room found" % platform
    if not verify_mgmt(client, platform, mgmt):
        return False, "%s management room failed re-verification before send" % platform
    before = client.joined_rooms()
    client.send_text(mgmt, "start-chat " + handle)
    portal = None
    deadline = time.time() + timeout_s
    while time.time() < deadline and not portal:
        portal = find_portal_for_handle(client, platform, handle, before)
        if not portal:
            time.sleep(poll_interval_s)
    if not portal:
        return False, "no %s portal appeared for %s within %ss" % (platform, handle, timeout_s)
    text = "pm_mng-self-check-" + nonce
    client.send_text(portal, text)
    ok, sender = poll_for_nonce(client, portal, text, timeout_s, poll_interval_s, client.user)
    if ok:
        return True, "nonce round-tripped in portal %s (sender=%s)" % (portal, sender)
    return False, "nonce never round-tripped in portal %s within %ss" % (portal, timeout_s)


DRIVERS = {
    "imessage": run_imessage,
    "whatsapp": lambda c, h, n, t, p: run_bridge_start_chat(c, "whatsapp", h, n, t, p),
    "gmessages": lambda c, h, n, t, p: run_bridge_start_chat(c, "gmessages", h, n, t, p),
}


def build_parser():
    p = argparse.ArgumentParser(
        prog="self_send_verify.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--imessage", metavar="HANDLE", help="operator's OWN iMessage handle (+E.164 or email)")
    p.add_argument("--whatsapp", metavar="HANDLE", help="operator's OWN WhatsApp number (+E.164)")
    p.add_argument("--gmessages", metavar="HANDLE", help="operator's OWN Google Messages number (+E.164)")
    p.add_argument(
        "--i-am-sending-to-myself", action="store_true", dest="confirm",
        help="REQUIRED to send anything: confirms every handle above is the operator's own")
    p.add_argument(
        "--env-file", default=DEFAULT_ENV_FILE,
        help="local hub config file, same shape as agents/uplink/uplink.env.local "
             "(default: %(default)s)")
    p.add_argument("--nonce", help="override the generated nonce (advanced/debugging only)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="seconds to wait per platform (default: %(default)s)")
    p.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_S, help="seconds between polls (default: %(default)s)")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    targets = [(name, getattr(args, name)) for name in ("imessage", "whatsapp", "gmessages") if getattr(args, name)]

    if not targets:
        parser.print_help(sys.stderr)
        sys.stderr.write(
            "\nNo platform handle given (--imessage / --whatsapp / --gmessages) — "
            "sending nothing.\n")
        return 1

    if len(targets) > MAX_PLATFORMS_PER_RUN:
        sys.stderr.write("refusing: more than %d platforms in one run\n" % MAX_PLATFORMS_PER_RUN)
        return 1

    if not args.confirm:
        sys.stderr.write(
            "refusing to send: pass --i-am-sending-to-myself to explicitly confirm "
            "every handle above is YOUR OWN. This tool is self-directed only.\n")
        return 1

    # Validate + smell-test every handle before touching the network.
    for name, handle in targets:
        if not (PHONE_RE.match(handle) or EMAIL_RE.match(handle)):
            sys.stderr.write("refusing: --%s handle %r does not look like a valid E.164 number or email\n" % (name, handle))
            return 1
        if _looks_obviously_fake(handle):
            sys.stderr.write(
                "refusing: --%s handle %r looks like a placeholder/test number, not a real "
                "self-handle. If this really is your own number, this is a courtesy check "
                "you cannot bypass here — use a real handle.\n" % (name, handle))
            return 1

    try:
        hs, user, token = load_local_config(args.env_file)
    except ConfigError as e:
        sys.stderr.write("config error: %s\n" % e)
        return 1

    client = Client(hs, token, user)
    nonce = args.nonce or uuid.uuid4().hex[:12]

    print("self_send_verify: hub=%s user=%s nonce=%s" % (hs, user, nonce))
    print("this WILL send real messages through: %s" % ", ".join(n for n, _ in targets))

    overall_ok = True
    for name, handle in targets:
        print("--- %s ---" % name)
        try:
            ok, detail = DRIVERS[name](client, handle, nonce, args.timeout, args.poll_interval)
        except urllib.error.HTTPError as e:
            ok, detail = False, "HTTP %s from local hub" % e.code
        except urllib.error.URLError as e:
            ok, detail = False, "cannot reach local hub: %s" % e.reason
        print("%s: %s (%s)" % (name, "PASS" if ok else "FAIL", detail))
        overall_ok = overall_ok and ok

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
