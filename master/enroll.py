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
      Loopback HTTP endpoint the uplink + the manager console call:
          GET  /enroll/health                    -> 200 "ok"
          POST /enroll/exchange {"code":…}        -> 200 {enrollment json} | 403
          POST /admin/add-teammate {"username":…} -> 200 {username, code,
               enroll_url, redeem_cmd} | 401/403 (not the manager) | 400
      /admin/add-teammate requires Authorization: Bearer <manager token> and
      provisions a brand-new teammate slot (register account + read-only space
      + append to tokens.local/.provision-state.local) then mints a one-time
      code — see add_teammate(). CORS on the admin + exchange endpoints allows
      ONLY the console origin http://127.0.0.1:8011 (never "*"); OPTIONS
      preflight is handled. Bound to loopback only; in production it sits
      behind the SAME TLS reverse proxy as the CS API, so it adds no new
      public exposure.

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
import hmac
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
SECRETS_FILE = os.path.join(HERE, "synapse", ".secrets.local")
DEFAULT_STORE = os.path.join(HERE, "enrollments.local")
DEFAULT_CS_BASE = "http://127.0.0.1:8018"
DEFAULT_SERVE_PORT = 8019
DEFAULT_TTL = int(os.environ.get("ENROLL_TTL", "600"))  # 10 minutes

# The ONLY browser origin allowed to call the admin + exchange endpoints. The
# manager console (apps/master) is served by the `views` nginx at this origin;
# no other origin (and never "*") is permitted.
CONSOLE_ORIGIN = "http://127.0.0.1:8011"


class EnrollError(Exception):
    """A refused exchange: invalid / used / expired code, or bad teammate."""


class HttpError(Exception):
    """An auth failure on the admin endpoint, carrying its HTTP status.

    401 = no / invalid bearer token; 403 = a valid token that is NOT the
    manager. Raised BEFORE any provisioning happens, so a rejected caller
    never causes a registration, a space, or a code to be created.
    """

    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


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
    # provision.sh persists each teammate password as PW_<U> (and this module's
    # add_teammate appends the same key), so read that exact key — not <U>_PW.
    pw = st.get("PW_%s" % up)
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


# ------------------------------------------------------------------ admin: add-teammate
#
# A manager-authenticated console action (POST /admin/add-teammate) that
# provisions a brand-new teammate slot end to end — register the account via
# the registration shared secret, create its read-only "space:<name>" with
# @manager invited at power level 0, append it to tokens.local /
# .provision-state.local WITHOUT clobbering existing teammates, and mint a
# one-time enrollment code. It mirrors master/provision.sh's shapes exactly.
#
# Security: EVERY path here first proves the caller is EXACTLY @manager:master
# (whoami against the master CS API). A non-manager (or tokenless) caller is
# refused with 401/403 and NOTHING is provisioned or minted. The shared secret
# and every access token stay out of responses and logs.

def _server_name():
    """The master server_name, derived from the provisioned manager mxid."""
    mgr = _manager_mxid()
    return mgr.split(":", 1)[1] if ":" in mgr else "master"


def _manager_mxid():
    """The provisioned manager mxid (tokens.local), default @manager:master."""
    return _tokens().get("MASTER_MANAGER_USER") or "@manager:master"


def _shared_secret():
    """The registration shared secret, read the same shell-var way as the rest
    (synapse/.secrets.local, produced mode 600 by setup.sh). Never logged/returned."""
    s = _parse_shell_vars(SECRETS_FILE).get("REGISTRATION_SHARED_SECRET")
    if not s:
        raise EnrollError("registration shared secret unavailable")
    return s


def _key(user):
    """Shell-var key segment for a teammate, matching provision.sh's upper():
    uppercase, non-alphanumeric -> '_'. Console usernames are validated to
    [a-z0-9]+ so this is a plain uppercase, keeping known_teammates() in sync."""
    return re.sub(r"[^A-Z0-9]", "_", user.upper())


def _request(method, url, headers=None, data=None, timeout=30):
    """Blocking JSON request that returns (status, body_bytes) even on HTTP
    errors (so whoami/register error bodies can be inspected, not raised)."""
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _whoami(cs_base, token):
    """Resolve a bearer token to its mxid via the CS API, or None if invalid."""
    status, body = _request(
        "GET", cs_base + "/_matrix/client/v3/account/whoami",
        headers={"Authorization": "Bearer " + token})
    if status != 200:
        return None
    try:
        return json.loads(body).get("user_id")
    except (ValueError, TypeError):
        return None


def _require_manager(token):
    """Prove the caller is EXACTLY the manager before anything is provisioned.

    Raises HttpError(401) for a missing/invalid token and HttpError(403) for a
    valid token that is not @manager:master. Returns the manager mxid on success.
    """
    if not token:
        raise HttpError(401, "missing bearer token")
    who = _whoami(_cs_base(), token)
    if who is None:
        raise HttpError(401, "invalid or expired token")
    manager = _manager_mxid()
    if who != manager or who != "@manager:master":
        raise HttpError(403, "not authorized: caller is not the manager")
    return manager


def _register_account(cs_base, secret, user, password):
    """Register @user via the register_new_matrix_user nonce+HMAC shared-secret
    flow against the CS API. Returns True if created, False if it already
    exists; raises EnrollError on any other failure. The password is sent to
    Synapse only; neither it nor the secret is ever logged or returned."""
    status, body = _request("GET", cs_base + "/_synapse/admin/v1/register")
    if status != 200:
        raise EnrollError("register nonce request failed (HTTP %d)" % status)
    try:
        nonce = json.loads(body)["nonce"]
    except (ValueError, KeyError, TypeError):
        raise EnrollError("register nonce response malformed")
    mac = hmac.new(secret.encode(), None, hashlib.sha1)
    for part in (nonce, user, password):
        mac.update(part.encode())
        mac.update(b"\x00")
    mac.update(b"notadmin")
    payload = json.dumps({
        "nonce": nonce, "username": user, "password": password,
        "admin": False, "mac": mac.hexdigest(),
    }).encode()
    status, body = _request(
        "POST", cs_base + "/_synapse/admin/v1/register", data=payload)
    if status == 200:
        return True
    txt = body.decode("utf-8", "replace")
    if "already taken" in txt or "M_USER_IN_USE" in txt:
        return False
    raise EnrollError("account registration failed (HTTP %d)" % status)


def _create_space(cs_base, token, user):
    """Create 'space:<user>' owned by the teammate with @manager invited
    read-only — power levels IDENTICAL to provision.sh's create_space. Called
    with the teammate's OWN token so they own the room."""
    server = _server_name()
    manager_mxid = _manager_mxid()
    user_mxid = "@%s:%s" % (user, server)
    body = json.dumps({
        "name": "space:%s" % user,
        "topic": "Read-only master space for %s" % user_mxid,
        "preset": "private_chat",
        "creation_content": {"type": "m.space"},
        "invite": [manager_mxid],
        "power_level_content_override": {
            "events_default": 50, "state_default": 50, "invite": 50,
            "kick": 50, "ban": 50, "redact": 50, "users_default": 0,
            "users": {user_mxid: 100, manager_mxid: 0},
        },
    }).encode()
    status, resp = _request(
        "POST", cs_base + "/_matrix/client/v3/createRoom",
        headers={"Authorization": "Bearer " + token}, data=body)
    if status != 200:
        raise EnrollError("space creation failed (HTTP %d)" % status)
    try:
        return json.loads(resp)["room_id"]
    except (ValueError, KeyError, TypeError):
        raise EnrollError("space creation response malformed")


def _roster_add(roster_str, user):
    """Append user to a space-separated roster string, de-duplicated, order kept."""
    parts = roster_str.split() if roster_str else []
    if user not in parts:
        parts.append(user)
    return " ".join(parts)


def _upsert_shell_vars(path, updates, header):
    """Rewrite a KEY='value' shell-var file mode 600, preserving EVERY existing
    key (so existing teammates like jkali are never clobbered) and setting/
    appending the given updates. Values may not contain a single quote."""
    for k, v in updates.items():
        if "'" in str(v):
            raise EnrollError("refusing to persist a value containing a quote")
    existing = _parse_shell_vars(path)   # ordered = file order
    existing.update(updates)             # updates in place; new keys appended
    lines = [header]
    for k, v in existing.items():
        lines.append("%s='%s'" % (k, v))
    payload = ("\n".join(lines) + "\n").encode()
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def add_teammate(token, username, enroll_url=None):
    """Manager-authenticated: provision a new teammate slot + mint a code.

    Order: (1) prove the caller is the manager — else raise HttpError and do
    nothing; (2) if the teammate is already provisioned, skip straight to
    minting (idempotent); (3) otherwise register the account, create its
    read-only space, and append it to tokens.local / .provision-state.local
    without clobbering existing teammates; (4) mint a one-time enrollment code;
    (5) return the console-facing contract.
    """
    _require_manager(token)   # raises HttpError(401/403) before any side effect

    user = (username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]{1,64}", user or ""):
        raise EnrollError("invalid username (use lowercase letters and digits)")
    if user == "manager":
        raise EnrollError("username 'manager' is reserved")

    cs_base = _cs_base()
    server = _server_name()

    if user not in known_teammates():
        secret = _shared_secret()
        password = secrets.token_urlsafe(24)
        created = _register_account(cs_base, secret, user, password)
        if not created:
            # Account exists on the server but we hold no password/space for it
            # (not console-managed). We cannot safely mint without its facts.
            raise EnrollError(
                "@%s:%s already exists but is not managed here" % (user, server))
        tok = _login(cs_base, user, password)      # fresh, scoped to this user
        space = _create_space(cs_base, tok, user)  # created as the teammate
        U = _key(user)
        st = _state()
        _upsert_shell_vars(STATE_FILE, {
            "PW_%s" % U: password,
            "SPACE_%s" % U: space,
            "TEAMMATES": _roster_add(st.get("TEAMMATES", ""), user),
        }, "# matrix-master provisioning state (mode 600, gitignored). Do NOT commit.")
        toks = _tokens()
        _upsert_shell_vars(TOKENS_FILE, {
            "MASTER_%s_USER" % U: "@%s:%s" % (user, server),
            "MASTER_%s_TOKEN" % U: tok,
            "MASTER_SPACE_%s" % U: space,
            "MASTER_TEAMMATES": _roster_add(toks.get("MASTER_TEAMMATES", ""), user),
        }, "# matrix-master access tokens — mode 600, gitignored. Do NOT commit.")

    code = mint(user)   # reuses existing single-use / hashed-store mint logic
    if not enroll_url:
        enroll_url = (os.environ.get("ENROLL_PUBLIC_URL")
                      or ("http://127.0.0.1:%d" % DEFAULT_SERVE_PORT)).rstrip("/")
    redeem_cmd = "bash agents/uplink/link.sh '%s' '%s'" % (enroll_url, code)
    return {"username": user, "code": code,
            "enroll_url": enroll_url, "redeem_cmd": redeem_cmd}


# ------------------------------------------------------------------ HTTP serve
def _make_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet; avoid noisy stderr access logs
            pass

        # CORS: the admin + exchange endpoints are reachable from the manager
        # console browser origin ONLY (never "*"). Applied only on those
        # responses; health carries no CORS headers.
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", CONSOLE_ORIGIN)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")

        def _json(self, code, obj, cors=False):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if cors:
                self._cors()
            self.end_headers()
            self.wfile.write(payload)

        def _read_json_body(self):
            n = int(self.headers.get("Content-Length") or "0")
            return json.loads(self.rfile.read(n) or b"{}")

        def _bearer(self):
            auth = self.headers.get("Authorization", "") or ""
            if auth[:7].lower() == "bearer ":
                return auth[7:].strip()
            return None

        def _public_enroll_url(self):
            env = os.environ.get("ENROLL_PUBLIC_URL")
            if env:
                return env.rstrip("/")
            host = self.headers.get("Host")
            if host:
                return "http://" + host
            return "http://127.0.0.1:%d" % DEFAULT_SERVE_PORT

        def do_OPTIONS(self):
            if self.path in ("/admin/add-teammate", "/enroll/exchange"):
                self.send_response(204)
                self._cors()
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def do_GET(self):
            if self.path == "/enroll/health":
                self._json(200, {"status": "ok"})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path == "/admin/add-teammate":
                self._admin_add_teammate()
                return
            if self.path != "/enroll/exchange":
                self._json(404, {"error": "not found"})
                return
            try:
                data = self._read_json_body()
                code = data.get("code")
            except (ValueError, TypeError):
                self._json(400, {"error": "malformed request"}, cors=True)
                return
            if not code or not isinstance(code, str):
                self._json(400, {"error": "missing code"}, cors=True)
                return
            try:
                self._json(200, exchange(code), cors=True)
            except EnrollError as e:
                # refuse used/expired/invalid codes with 403 Forbidden
                self._json(403, {"error": str(e)}, cors=True)
            except Exception as e:  # e.g. master unreachable during login
                self._json(502, {"error": "issuance failed: %s" % e}, cors=True)

        def _admin_add_teammate(self):
            token = self._bearer()
            try:
                data = self._read_json_body()
            except (ValueError, TypeError):
                self._json(400, {"error": "malformed request"}, cors=True)
                return
            username = data.get("username") if isinstance(data, dict) else None
            try:
                result = add_teammate(token, username, self._public_enroll_url())
                self._json(200, result, cors=True)
            except HttpError as e:              # 401 no/invalid token, 403 not manager
                self._json(e.status, {"error": str(e)}, cors=True)
            except EnrollError as e:            # bad username / provisioning refusal
                self._json(400, {"error": str(e)}, cors=True)
            except Exception as e:              # e.g. master unreachable mid-provision
                self._json(502, {"error": "add-teammate failed: %s" % e}, cors=True)

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
