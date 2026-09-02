#!/usr/bin/env python3
"""session-connect/connect_server.py — loopback one-click connect helper for
Instagram / LinkedIn / X.

The teammate app (apps/user, http://127.0.0.1:8011) can drive the login in one
click: the browser cannot read Chrome's cookie store or `docker compose exec`
the bridge, so this small local service does it on the browser's behalf. It is
the exact shape and security posture of gmessages-connect/connect_server.py.

All three networks (twitter / linkedin / instagram) are completed SERVER-SIDE
via the bridge provisioning API: the session is read, submitted, and discarded
inside this process; nothing but a generic status (+ the linked account
localpart) is returned (F6). Instagram uses the mautrix-meta `ig-` build, which
exposes an `instagram` cookies flow (sessionid/csrftoken/ds_user_id/mid) just
like the others — the session never leaves this process.

Some logins need an extra INTERACTIVE step after the cookies (X's XChat
passcode: the bridge returns type=user_input). The browser cannot be handed the
session, but it CAN supply that one value: POST /connect/<net>/start returns
{status:input_required, login_id, step_id, fields}, the Hub renders the field(s),
and POST /connect/<net>/input submits them here. The value (a short passcode) is
a credential: it rides browser -> loopback -> bridge only, and is never logged
or returned (F6). login_id/step_id come from the bridge, are echoed by the
browser, and are re-validated (connect.ID_RE) here before use (F2).

POST /enrich/numbers (F1-gated, CORS to the same loopback origin) calls the
read-only agents/enrich/number_resolver.resolve_all() and returns
{numbers: {room_id: {value, kind, source}}}. The values are real phone
numbers / emails: like the cookie return, they go ONLY to the authorized
loopback origin and are NEVER logged (F6). The resolver is SELECT-only — no
write happens anywhere on this path.

POST /contacts/list (F1-gated) returns the teammate's OWN imported address book
(agents/contacts/contacts.db, read-only) so the app can offer a per-contact
share override for rows that have no conversation — e.g. "Datadog Alerting".
It is deliberately a POST, not a GET: this helper's GETs are ungated
liveness-only by design (F5), and adding a GET would also widen the shared CORS
methods header (F1). Parameterless (the path is fixed and carries no query —
_diag logs self.path), rows filtered to the known source ids, response capped
at CONTACTS_MAX rows, deleted rows excluded. The values are real phone
numbers/emails: like the cookie and /enrich/numbers returns they go ONLY to the
authorized loopback origin and are NEVER logged (F6). See session-connect's
CLAUDE.md for the ACCEPTED local-process residual.

POST /enroll/exchange (F1-gated) is the server-side leg of the app's "Connect
to organization" flow. The browser cannot fetch a REMOTE master origin (the
app's CSP connect-src is loopback-only), so it hands us {master_url, code} and
we POST {code} to <master_url>/enroll/exchange here, returning ONLY the five
known credential fields (ENROLL_FIELDS). SSRF containment: only https (or http
to a loopback master), no redirects (a 3xx could carry the code elsewhere),
bounded timeout. The one-time code and the returned scoped credentials are
NEVER logged (F6) — the app writes them into its own local account-data.

Security invariants (do not weaken) — identical to gmessages-connect:
  * Binds EXACTLY 127.0.0.1:8021 — loopback only, never 0.0.0.0 / "".
  * F1 — every do_POST is gated by _authorized() BEFORE any side effect: (a)
    Origin ∈ the two loopback aliases of the user's own app; (b) Content-Type ==
    application/json; (c) X-Beepa-Connect: 1. (b)+(c) force a cross-origin page
    into a CORS preflight that fails.
  * F5 — GET /connect/health has ZERO side effects and no CORS. No path reads
    cookies / Keychain / the bridge at import, start, on a timer, or from health
    — ONLY an authorized POST does.
  * F6 — the provisioning shared_secret, cookies, passcodes, and raw bridge
    bodies are NEVER returned or logged; failures map to fixed generic messages.
  * F2 — bridge-returned login_id/step_id are validated (connect.ID_RE) before
    being interpolated into a provisioning-API path — on both /start and /input.
  * F1b (per-contact-share plan) — /contacts/list additionally requires a Host
    header naming this loopback listener (HOST_ALLOWED), as defence-in-depth
    against DNS rebinding: a rebound name reaches us with the attacker's Host.

Run:  python3 connect_server.py --host 127.0.0.1 --port 8021
(usually via run-connect.sh under launchd — com.jkali.session-connect).
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request

import connect  # session-connect/connect.py — same dir; imported side-effect-free

# Read-only number resolver lives in the sibling agents/enrich package. Add
# that dir to sys.path so we import the module across the boundary rather than
# copying its code; the import is side-effect-free (no DB read at import).
_ENRICH_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents", "enrich")
if _ENRICH_DIR not in sys.path:
    sys.path.insert(0, _ENRICH_DIR)
import number_resolver  # agents/enrich/number_resolver.py — SELECT-only, fail-soft

APP_ORIGINS = ("http://127.0.0.1:8011", "http://localhost:8011")
SERVER_NETWORKS = ("twitter", "linkedin", "instagram")
ENRICH_NUMBERS_PATH = "/enrich/numbers"
ENROLL_EXCHANGE_PATH = "/enroll/exchange"
CONTACTS_LIST_PATH = "/contacts/list"

# The teammate's own imported address book (agents/contacts/, mode 600). Opened
# READ-ONLY (sqlite `mode=ro`) so this helper can never create, migrate, or
# write it — the importer and the uplink are its only writers.
CONTACTS_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "agents", "contacts", "contacts.db")
# Must stay in step with agents/uplink/uplink.py's SOURCE_ID_TO_LABEL: a row
# whose source is not here is not a source this system mirrors, so it can never
# become a valid override key and is filtered out before the response is built.
CONTACTS_SOURCES = ("whatsapp", "imessage", "gmessages",
                    "instagram", "linkedin", "twitter")
CONTACTS_MAX = 2000   # P3: this server is single-threaded; bound the response.

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8021
MAX_BODY = 64 * 1024  # /input bodies are tiny (a passcode); cap defensively.
ENROLL_TIMEOUT = 15   # seconds — per-socket timeout on the outbound exchange
ENROLL_MAX_RESP = 16 * 1024  # the exchange returns 5 tiny fields; cap tightly so
                             # a rogue master can't slow-drip 64KB to stall the
                             # single-threaded helper (review residual, LOW).
# Fields the master's /enroll/exchange returns; we relay ONLY these, never the
# raw upstream body (F6). Must match master/enroll.py's exchange() response.
ENROLL_FIELDS = ("master_hs_url", "master_user", "master_token",
                 "manager_mxid", "master_space")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects on the enroll exchange: a redirect could carry the
    one-time code to a different host. Any 3xx becomes a failure instead."""
    def redirect_request(self, *a, **k):
        return None

_PATH_RE = re.compile(r"^/connect/([a-z]+)/(start|input)$")

# The port actually bound by serve() (the default may be held by a foreign
# process, see serve()'s fallback range). Used ONLY to build the Host allowlist
# for /contacts/list — the security boundary is still the loopback bind.
_BOUND_PORT = DEFAULT_PORT


def _host_allowed(host):
    """F1b: the Host header must name this loopback listener.

    Anti-rebinding defence-in-depth — a DNS-rebound name resolves to 127.0.0.1
    but arrives carrying the ATTACKER's Host, so pinning it costs nothing and
    removes the rebinding path. Not a replacement for _authorized(): both run."""
    if not isinstance(host, str):
        return False
    return host in ("127.0.0.1:%d" % _BOUND_PORT, "localhost:%d" % _BOUND_PORT)


def _make_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # F6: no access log
            pass

        def _diag(self, status):
            try:
                sys.stderr.write("[connect] %s %s origin=%r -> %d\n"
                                 % (self.command, self.path,
                                    self.headers.get("Origin"), status))
                sys.stderr.flush()
            except Exception:
                pass

        def _origin(self):
            og = self.headers.get("Origin")
            return og if og in APP_ORIGINS else None

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", self._origin() or APP_ORIGINS[0])
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Beepa-Connect")

        def _json(self, code, obj, cors=False):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if cors:
                self._cors()
            self.end_headers()
            self.wfile.write(payload)

        def _body_len(self):
            try:
                return int(self.headers.get("Content-Length") or "0")
            except (TypeError, ValueError):
                return 0

        def _discard_body(self):
            # Drain in bounded chunks (never allocate a caller-declared size) so
            # an oversized Content-Length can't balloon this single-threaded
            # server's memory before the connection is closed.
            n = self._body_len()
            while n > 0:
                try:
                    chunk = self.rfile.read(min(n, 65536))
                except Exception:
                    break
                if not chunk:
                    break
                n -= len(chunk)

        def _read_json_body(self):
            """Read and parse a small JSON body; drain and return None on any
            problem (oversized, malformed, not an object). Never logged."""
            n = self._body_len()
            if n <= 0 or n > MAX_BODY:
                self._discard_body()
                return None
            try:
                raw = self.rfile.read(n)
                obj = json.loads(raw.decode("utf-8"))
            except Exception:
                return None
            return obj if isinstance(obj, dict) else None

        # ---- F1: authorization gate, run at the TOP of every do_POST ----
        def _authorized(self):
            if self._origin() is None:
                self._json(403, {"error": "forbidden"}, cors=True); self._diag(403)
                return False
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._json(415, {"error": "unsupported media type"}, cors=True); self._diag(415)
                return False
            if self.headers.get("X-Beepa-Connect") != "1":
                self._json(403, {"error": "forbidden"}, cors=True); self._diag(403)
                return False
            return True

        def _route(self):
            """(network, verb) for a valid /connect/<net>/(start|input) path,
            else (None, None)."""
            m = _PATH_RE.match(self.path)
            if m and m.group(1) in SERVER_NETWORKS:
                return m.group(1), m.group(2)
            return None, None

        def do_OPTIONS(self):
            if self._origin() is not None and (
                    self._route()[0] is not None
                    or self.path == ENRICH_NUMBERS_PATH
                    or self.path == ENROLL_EXCHANGE_PATH
                    or self.path == CONTACTS_LIST_PATH):
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
            if self.path == "/connect/health":  # F5: liveness only, no CORS
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            self._diag(-1)
            if self.path == ENRICH_NUMBERS_PATH:
                if not self._authorized():   # F1: gate BEFORE any DB read
                    return
                self._discard_body()
                self._enrich_numbers()
                return
            if self.path == CONTACTS_LIST_PATH:
                if not self._authorized():   # F1: gate BEFORE any DB read
                    return
                if not _host_allowed(self.headers.get("Host")):  # F1b
                    self._json(403, {"error": "forbidden"}, cors=True); self._diag(403)
                    return
                self._discard_body()         # parameterless by design
                self._contacts_list()
                return
            if self.path == ENROLL_EXCHANGE_PATH:
                if not self._authorized():   # F1: gate BEFORE any outbound call
                    return
                body = self._read_json_body()   # reads + drains the body itself
                self._enroll_exchange(body)
                return
            name, verb = self._route()
            if name is None:
                self._json(404, {"error": "not found"})
                return
            if not self._authorized():   # F1: gate BEFORE any side effect
                return
            if verb == "input":
                body = self._read_json_body()   # reads + drains the body itself
                self._submit_input(name, body)
            else:
                self._discard_body()
                self._start_provisioning(name)

        # ---- read each 1:1 conversation's real phone/email, LOCAL-only ----
        def _enrich_numbers(self):
            # F6 posture: this returns PII (real E.164 numbers / emails) but,
            # exactly like the Instagram cookie return, ONLY to the authorized
            # loopback origin — the numbers/map are NEVER logged (the sole
            # log line, _diag, carries only method/path/origin/status). The
            # resolver is SELECT-only, so this is a pure read: no writes.
            try:
                numbers = number_resolver.resolve_all()
            except Exception:  # fail closed on an unexpected raise; leak nothing
                self._json(500, {"error": "Could not resolve numbers."}, cors=True)
                return
            self._json(200, {"numbers": numbers}, cors=True)

        # ---- the teammate's OWN imported address book, LOCAL-only ----
        def _contacts_list(self):
            # F6 posture, identical to /enrich/numbers: this returns PII (real
            # E.164 numbers / emails / display names) but ONLY to the authorized
            # loopback origin, and NEVER to a log — the sole log line (_diag)
            # carries method/path/origin/status, and the path here is a fixed
            # parameterless literal. Read-only: `mode=ro` means this process
            # cannot create or migrate contacts.db even if it is absent.
            rows = []
            try:
                con = sqlite3.connect("file:%s?mode=ro" % CONTACTS_DB, uri=True, timeout=15)
            except sqlite3.Error:
                # No store yet (the hourly importer has not run) — an empty list
                # is the truthful answer, not an error the UI must special-case.
                self._json(200, {"contacts": []}, cors=True)
                return
            try:
                cur = con.execute(
                    "SELECT source, network_id, kind, display_name FROM contacts "
                    "WHERE deleted = 0 ORDER BY source, network_id LIMIT ?",
                    (CONTACTS_MAX,))
                for source, network_id, kind, display_name in cur:
                    if source not in CONTACTS_SOURCES:
                        continue  # not a source this system mirrors -> not offerable
                    rows.append({"source": source, "network_id": network_id,
                                 "kind": kind, "display_name": display_name})
            except sqlite3.Error:
                self._json(500, {"error": "Could not read contacts."}, cors=True)
                return
            finally:
                con.close()
            self._json(200, {"contacts": rows}, cors=True)

        # ---- server-side leg of the GUI "Connect to organization" flow ----
        def _enroll_exchange(self, body):
            # The browser CANNOT fetch a remote master origin — apps/user's CSP
            # connect-src is loopback-only by design. So the app hands us
            # {master_url, code} and we perform the master's /enroll/exchange on
            # its behalf, over the network the CSP can't police. F6 posture: the
            # one-time code and the returned credentials are NEVER logged; we
            # return ONLY the five known credential fields (ENROLL_FIELDS),
            # never the raw upstream body.
            murl = (body or {}).get("master_url")
            code = (body or {}).get("code")
            if not isinstance(murl, str) or not isinstance(code, str) or not murl or not code:
                self._json(400, {"status": "failed"}, cors=True); return
            murl = murl.rstrip("/")
            try:
                u = urllib.parse.urlparse(murl)
            except ValueError:
                self._json(400, {"status": "failed"}, cors=True); return
            host = (u.hostname or "").lower()
            loopback = host in ("127.0.0.1", "localhost", "::1")
            # SSRF containment: https to the (user-chosen, tailnet) master, or
            # http ONLY to a loopback master for local testing — no other shape.
            if not host or not (u.scheme == "https" or (u.scheme == "http" and loopback)):
                self._json(400, {"status": "failed"}, cors=True); return
            payload = json.dumps({"code": code}).encode()
            req = urllib.request.Request(
                murl + "/enroll/exchange", data=payload, method="POST",
                headers={"Content-Type": "application/json"})
            # No redirects (a 3xx could carry the code to another host); bounded time.
            opener = urllib.request.build_opener(_NoRedirect())
            try:
                with opener.open(req, timeout=ENROLL_TIMEOUT) as r:
                    raw = r.read(ENROLL_MAX_RESP + 1)
                data = json.loads(raw.decode("utf-8"))
            except BaseException:      # unreachable, TLS error, 4xx/5xx, bad JSON
                self._json(502, {"status": "failed"}, cors=True); return
            if not isinstance(data, dict):
                self._json(502, {"status": "failed"}, cors=True); return
            out = {k: data.get(k) for k in ENROLL_FIELDS}
            if not out.get("master_token") or not out.get("master_hs_url"):
                self._json(502, {"status": "failed"}, cors=True); return
            self._json(200, out, cors=True)

        # ---- turn a bridge step response into what the browser gets back ----
        # complete -> {status:complete, account}; user_input -> {status:
        # input_required, login_id, step_id, fields}; anything else -> failed.
        # Never echoes the raw bridge body (F6); validates ids (F2).
        def _finish(self, resp):
            try:
                data = json.loads(resp)
            except (ValueError, TypeError):
                self._json(200, {"status": "failed"}, cors=True); return
            if data.get("type") == "complete":
                who = re.search(r'"user_login_id":"([^"]+)"', resp)
                account = who.group(1).split("/")[0] if who else ""
                self._json(200, {"status": "complete", "account": account}, cors=True)
                return
            if data.get("type") == "user_input":
                lid, step = data.get("login_id"), data.get("step_id")
                if not lid or not step or not connect.ID_RE.match(lid) or not connect.ID_RE.match(step):
                    self._json(200, {"status": "failed"}, cors=True); return
                fields = []
                for f in (data.get("user_input") or {}).get("fields", []):
                    if not isinstance(f, dict) or not f.get("id"):
                        continue
                    fields.append({"id": str(f.get("id")),
                                   "name": str(f.get("name") or f.get("id")),
                                   "type": str(f.get("type") or "text"),
                                   "description": str(f.get("description") or "")})
                self._json(200, {"status": "input_required", "login_id": lid,
                                 "step_id": step,
                                 "instructions": str(data.get("instructions") or ""),
                                 "fields": fields}, cors=True)
                return
            # session likely stale (re-sign-in) — never echo the raw body (F6)
            self._json(200, {"status": "failed"}, cors=True)

        # ---- all networks: read cookies, submit them, hand back the outcome ----
        def _start_provisioning(self, name):
            net = connect.NETWORKS[name]
            try:
                secret = connect.shared_secret(net["config"])
            except SystemExit:
                self._json(500, {"error": "Could not start login."}, cors=True)
                return
            try:
                start = connect.api(net, "/login/start/%s" % net["flow"], secret, body={})
                data = json.loads(start)
            except BaseException:
                self._json(502, {"error": "Could not start login."}, cors=True)
                return
            lid, step = data.get("login_id"), data.get("step_id")
            fields = (data.get("cookies") or {}).get("fields", [])
            if not lid or not step or not connect.ID_RE.match(lid) or not connect.ID_RE.match(step):
                self._json(502, {"error": "Could not start login."}, cors=True)
                return
            try:
                values = connect.resolve_fields(name, net, fields)  # reads Chrome cookies
            except connect.ck.CookieError:
                self._json(400, {"error":
                    "Session not found — sign into the site in Chrome and try again."}, cors=True)
                return
            except BaseException:  # SystemExit(die) on a missing required field, etc.
                self._json(400, {"error":
                    "Session not found — sign into the site in Chrome and try again."}, cors=True)
                return
            try:
                resp = connect.api(net, "/login/step/%s/%s/cookies" % (lid, step),
                                   secret, body=values)
            except BaseException:
                self._json(502, {"error": "Could not complete login."}, cors=True)
                return
            self._finish(resp)

        # ---- submit one interactive step (e.g. X's XChat passcode) ----
        def _submit_input(self, name, body):
            net = connect.NETWORKS[name]
            lid = (body or {}).get("login_id")
            step = (body or {}).get("step_id")
            values = (body or {}).get("values")
            if (not lid or not step or not isinstance(values, dict)
                    or not connect.ID_RE.match(str(lid)) or not connect.ID_RE.match(str(step))):
                self._json(400, {"error": "Could not complete login."}, cors=True)
                return
            # Coerce to a flat {str: str} map; the passcode value is never logged.
            payload = {str(k): str(v) for k, v in values.items()}
            try:
                secret = connect.shared_secret(net["config"])
            except SystemExit:
                self._json(500, {"error": "Could not complete login."}, cors=True)
                return
            try:
                resp = connect.api(net, "/login/step/%s/%s/user_input" % (lid, step),
                                   secret, body=payload)
            except BaseException:
                self._json(502, {"error": "Could not complete login."}, cors=True)
                return
            self._finish(resp)

    return Handler


def serve(host, port):
    from http.server import HTTPServer  # single-threaded: serial, no state race
    # A managed Mac can have the default port held by a foreign (often root-owned)
    # process; try a small fallback range so the helper doesn't crash-loop on
    # EADDRINUSE. The security boundary is the loopback HOST (127.0.0.1) — never
    # 0.0.0.0 — not the specific port.
    global _BOUND_PORT
    httpd, chosen = None, port
    for p in range(port, port + 5):
        try:
            httpd = HTTPServer((host, p), _make_handler())
            chosen = p
            break
        except OSError as e:
            sys.stderr.write("[connect] port %d unavailable (%s); trying next\n" % (p, e))
    if httpd is None:
        sys.stderr.write("[connect] no free port in %d-%d; exiting\n" % (port, port + 4))
        return 1
    _BOUND_PORT = chosen   # F1b: pins /contacts/list's Host allowlist
    # Publish the chosen loopback base where the app (served same-origin from
    # :8011) can read it, so the browser knows which port to fetch — the CSP
    # whitelists the whole range. Best-effort; the app falls back to the default.
    try:
        pf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "apps", "user", "connect.local.json")
        with open(pf, "w") as f:
            f.write('{"base": "http://127.0.0.1:%d"}\n' % chosen)
        os.chmod(pf, 0o600)
    except OSError:
        pass
    sys.stderr.write("[connect] serving on http://%s:%d (loopback)\n" % (host, chosen))
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="one-click IG/LI/X connect helper")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    return serve(args.host, args.port) or 0


if __name__ == "__main__":
    sys.exit(main())
