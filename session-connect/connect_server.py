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

Run:  python3 connect_server.py --host 127.0.0.1 --port 8021
(usually via run-connect.sh under launchd — com.jkali.session-connect).
"""
import argparse
import json
import os
import re
import sys

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

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8021
MAX_BODY = 64 * 1024  # /input bodies are tiny (a passcode); cap defensively.

_PATH_RE = re.compile(r"^/connect/([a-z]+)/(start|input)$")


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
                    self._route()[0] is not None or self.path == ENRICH_NUMBERS_PATH):
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
    httpd = HTTPServer((host, port), _make_handler())
    sys.stderr.write("[connect] serving on http://%s:%d (loopback)\n" % (host, port))
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="one-click IG/LI/X connect helper")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
