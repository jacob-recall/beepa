#!/usr/bin/env python3
"""master/enroll.py — one-time enrollment-code issuance for teammate uplinks.

Replaces the manual scoped-token handoff (PLAN-MASTER-SYNC.md §5.3 v1.5,
PLAN-MASTER-SYNC-IMPL.md P3.2). Python 3.9+ stdlib only — no pip deps, no new
public network surface beyond the already-exposed CS API / a localhost action.

Operations
----------
  mint <teammate> [--ttl SECONDS]
      Mint a SHORT-LIVED, SINGLE-USE enrollment code for an already-provisioned
      teammate account (alice, bob, ...). Prints the code on stdout. The store
      keeps only the code's SHA-256, never the plaintext.

  exchange <code>
      Given a VALID, UNUSED, UNEXPIRED code: log in as that teammate to mint a
      FRESH access token SCOPED to that teammate, then return, as JSON, the
      master base URL, the teammate mxid, that fresh scoped token, the manager
      mxid, and the teammate's MASTER_SPACE — and mark the code used. A used,
      expired, or invalid code is refused (exit 3 / HTTP 403).

  serve [--host 127.0.0.1] [--port 8019]
      Loopback HTTP endpoint the uplink calls to exchange a code remotely:
          GET  /enroll/health              -> 200 "ok"
          POST /enroll/exchange {"code":…} -> 200 {enrollment json} | 403 {error}
      Bound to loopback only; in production it sits behind the SAME TLS reverse
      proxy as the CS API, so it adds no new public exposure.

Security invariants
-------------------
  * The returned token is minted by password-logging-in AS the teammate, so it
    is inherently limited to that teammate: @alice's code can never yield a
    token that writes @bob's rooms — Synapse enforces per-account authorization.
  * Codes are single-use (marked used only AFTER a token is successfully issued,
    so a transient master outage never burns a code), expire after their TTL,
    and only the SHA-256 of a code is ever persisted. Store is mode 600.
  * Reads teammate passwords from master/.provision-state.local and space ids /
    base URL / manager mxid from master/tokens.local — both already mode 600,
    produced by provision.sh. This helper does NOT alter account provisioning.

Env overrides (mainly for tests):
  ENROLL_STORE     path to the code store           (default: master/enrollments.local)
  MASTER_CS_BASE   master CS API base URL            (default: tokens.local, else 127.0.0.1:8018)
  ENROLL_TTL       default code lifetime in seconds  (default: 600)
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, ".provision-state.local")
TOKENS_FILE = os.path.join(HERE, "tokens.local")
DEFAULT_STORE = os.path.join(HERE, "enrollments.local")
DEFAULT_CS_BASE = "http://127.0.0.1:8018"
DEFAULT_TTL = int(os.environ.get("ENROLL_TTL", "600"))  # 10 minutes


class EnrollError(Exception):
    """A refused exchange: invalid / used / expired code, or bad teammate."""


# ------------------------------------------------------------------ shell files
def _parse_shell_vars(path):
    """Parse KEY='value' lines (the format provision.sh writes). Missing -> {}."""
    out = {}
    try:
        with open(path) as f:
            txt = f.read()
    except FileNotFoundError:
        return out
    for m in re.finditer(r"^(\w+)='([^']*)'", txt, re.M):
        out[m.group(1)] = m.group(2)
    return out


def _tokens():
    return _parse_shell_vars(TOKENS_FILE)


def _state():
    return _parse_shell_vars(STATE_FILE)


def _cs_base():
    return (os.environ.get("MASTER_CS_BASE")
            or _tokens().get("MASTER_CS_BASE")
            or DEFAULT_CS_BASE).rstrip("/")


def known_teammates():
    """Teammate localparts that provision.sh issued a scoped account for."""
    toks = _tokens()
    out = []
    for k in toks:
        m = re.match(r"^MASTER_([A-Z0-9]+)_USER$", k)
        if m and m.group(1) != "MANAGER":
            out.append(m.group(1).lower())
    return sorted(out)


def _teammate_facts(teammate):
    """(mxid, space, password) for a provisioned teammate, or raise EnrollError."""
    up = teammate.upper()
    toks = _tokens()
    st = _state()
    mxid = toks.get("MASTER_%s_USER" % up)
    space = toks.get("MASTER_SPACE_%s" % up)
    pw = st.get("%s_PW" % up)
    if not mxid or not space or not pw:
        raise EnrollError("unknown or unprovisioned teammate: %s" % teammate)
    return mxid, space, pw


# ------------------------------------------------------------------ code store
def _store_path():
    return os.environ.get("ENROLL_STORE") or DEFAULT_STORE


def _load_store():
    path = _store_path()
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"codes": {}}


def _save_store(store):
    """Atomically write the store mode 600 (never world/group readable)."""
    path = _store_path()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


# ------------------------------------------------------------------ operations
def mint(teammate, ttl=None):
    """Mint a single-use enrollment code for a provisioned teammate."""
    teammate = teammate.lower()
    _teammate_facts(teammate)  # validates the teammate exists (raises otherwise)
    ttl = DEFAULT_TTL if ttl is None else int(ttl)
    code = secrets.token_urlsafe(24)
    now = int(time.time())
    store = _load_store()
    store.setdefault("codes", {})[_hash(code)] = {
        "teammate": teammate,
        "created_at": now,
        "expires_at": now + ttl,
        "used_at": None,
    }
    _save_store(store)
    return code


def _login(cs_base, localpart, password):
    """Password-login as the teammate -> a FRESH access token scoped to them."""
    body = json.dumps({
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": localpart},
        "password": password,
        "initial_device_display_name": "uplink-enrollment",
    }).encode()
    req = urllib.request.Request(
        cs_base + "/_matrix/client/v3/login", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def exchange(code):
    """Redeem a code -> scoped enrollment dict. Marks the code used on success.

    Raises EnrollError for an invalid / used / expired code. The code is burned
    ONLY after a fresh scoped token is successfully minted, so a master outage
    (login failure) leaves the code re-usable.
    """
    store = _load_store()
    rec = store.get("codes", {}).get(_hash(code))
    if rec is None:
        raise EnrollError("invalid enrollment code")
    if rec.get("used_at") is not None:
        raise EnrollError("enrollment code already used")
    if int(time.time()) > int(rec["expires_at"]):
        raise EnrollError("enrollment code expired")

    teammate = rec["teammate"]
    mxid, space, pw = _teammate_facts(teammate)
    cs_base = _cs_base()
    manager = _tokens().get("MASTER_MANAGER_USER", "")
    token = _login(cs_base, teammate, pw)  # fresh, scoped to this teammate

    # burn the code only now that issuance succeeded
    rec["used_at"] = int(time.time())
    _save_store(store)

    return {
        "master_hs_url": cs_base,
        "master_user": mxid,
        "master_token": token,
        "manager_mxid": manager,
        "master_space": space,
    }


# ------------------------------------------------------------------ HTTP serve
def _make_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet; avoid noisy stderr access logs
            pass

        def _json(self, code, obj):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/enroll/health":
                self._json(200, {"status": "ok"})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/enroll/exchange":
                self._json(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length") or "0")
                data = json.loads(self.rfile.read(n) or b"{}")
                code = data.get("code")
            except (ValueError, TypeError):
                self._json(400, {"error": "malformed request"})
                return
            if not code or not isinstance(code, str):
                self._json(400, {"error": "missing code"})
                return
            try:
                self._json(200, exchange(code))
            except EnrollError as e:
                # refuse used/expired/invalid codes with 403 Forbidden
                self._json(403, {"error": str(e)})
            except Exception as e:  # e.g. master unreachable during login
                self._json(502, {"error": "issuance failed: %s" % e})

    return Handler


def serve(host, port):
    from http.server import HTTPServer  # single-threaded: serial, no code race
    httpd = HTTPServer((host, port), _make_handler())
    sys.stderr.write("[enroll] serving on http://%s:%d (loopback)\n"
                     % (host, port))
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# ------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="teammate uplink enrollment")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mint", help="mint a single-use code for a teammate")
    m.add_argument("teammate")
    m.add_argument("--ttl", type=int, default=None,
                   help="code lifetime in seconds (default %d)" % DEFAULT_TTL)

    x = sub.add_parser("exchange", help="redeem a code -> scoped enrollment json")
    x.add_argument("code")

    s = sub.add_parser("serve", help="loopback exchange endpoint for the uplink")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8019)

    args = ap.parse_args(argv)
    if args.cmd == "mint":
        try:
            print(mint(args.teammate, args.ttl))
        except EnrollError as e:
            sys.stderr.write("enroll: %s\n" % e)
            return 2
        return 0
    if args.cmd == "exchange":
        try:
            print(json.dumps(exchange(args.code), indent=2))
        except EnrollError as e:
            sys.stderr.write("enroll: refused: %s\n" % e)
            return 3
        return 0
    if args.cmd == "serve":
        serve(args.host, args.port)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
