#!/usr/bin/env python3
"""session-connect/connect_server.py — loopback one-click connect helper for
Instagram / LinkedIn / X.

The teammate app (apps/user, http://127.0.0.1:8011) can drive the login in one
click: the browser cannot read Chrome's cookie store or `docker compose exec`
the bridge, so this small local service does it on the browser's behalf. It is
the exact shape and security posture of gmessages-connect/connect_server.py.

Per network:
  * twitter / linkedin — completed SERVER-SIDE via the bridge provisioning API.
    The session is read, submitted, and discarded inside this process; nothing
    but a generic status (+ the linked account localpart) is returned (F6).
  * instagram — the Meta bridge exposes NO provisioning login (the 'instagram'
    flow is 404), so it cannot be completed server-side. This endpoint returns
    the IG cookie blob to the user's OWN Hub tab (loopback + origin-gated), which
    submits it through the existing management-room paste path and redacts it —
    the SAME credential the paste box already handles, minus the manual paste.
    This is the one deliberate deviation from the gmessages helper's "cookies
    never leave the process"; it is confined to Instagram and to the one allowed
    loopback origin, and is documented as such.

Security invariants (do not weaken) — identical to gmessages-connect:
  * Binds EXACTLY 127.0.0.1:8021 — loopback only, never 0.0.0.0 / "".
  * F1 — every do_POST is gated by _authorized() BEFORE any side effect: (a)
    Origin ∈ the two loopback aliases of the user's own app; (b) Content-Type ==
    application/json; (c) X-Beepa-Connect: 1. (b)+(c) force a cross-origin page
    into a CORS preflight that fails.
  * F5 — GET /connect/health has ZERO side effects and no CORS. No path reads
    cookies / Keychain / the bridge at import, start, on a timer, or from health
    — ONLY an authorized POST does.
  * F6 — the provisioning shared_secret and raw bridge bodies are NEVER returned
    or logged; failures map to fixed generic messages. (Instagram's cookie blob
    is returned ONLY to the authorized loopback origin, by design, above.)
  * F2 — bridge-returned login_id/step_id are validated (connect.ID_RE) before
    being interpolated into a provisioning-API path.

Run:  python3 connect_server.py --host 127.0.0.1 --port 8021
(usually via run-connect.sh under launchd — com.jkali.session-connect).
"""
import argparse
import json
import re
import sys

import connect  # session-connect/connect.py — same dir; imported side-effect-free

APP_ORIGINS = ("http://127.0.0.1:8011", "http://localhost:8011")
SERVER_NETWORKS = ("twitter", "linkedin", "instagram")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8021

_PATH_RE = re.compile(r"^/connect/([a-z]+)/start$")


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

        def _discard_body(self):
            try:
                n = int(self.headers.get("Content-Length") or "0")
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                try:
                    self.rfile.read(n)
                except Exception:
                    pass

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

        def _network(self):
            m = _PATH_RE.match(self.path)
            if m and m.group(1) in SERVER_NETWORKS:
                return m.group(1)
            return None

        def do_OPTIONS(self):
            if self._origin() is not None and self._network() is not None:
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
            name = self._network()
            if name is None:
                self._json(404, {"error": "not found"})
                return
            if not self._authorized():   # F1: gate BEFORE any side effect
                return
            self._discard_body()
            if name == "instagram":
                self._start_instagram()
            else:
                self._start_provisioning(name)

        # ---- twitter / linkedin: completed server-side ----
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
            if '"type":"complete"' in resp or '"complete"' in resp:
                who = re.search(r'"user_login_id":"([^"]+)"', resp)
                account = who.group(1).split("/")[0] if who else ""
                self._json(200, {"status": "complete", "account": account}, cors=True)
            else:
                # session likely stale (re-sign-in) — never echo the raw body (F6)
                self._json(200, {"status": "failed"}, cors=True)

        # ---- instagram: hand the cookie blob to the user's own Hub tab ----
        def _start_instagram(self):
            net = connect.NETWORKS["instagram"]
            try:
                jar = connect.ck.read(net["domain"])
            except connect.ck.CookieError:
                self._json(400, {"error":
                    "Instagram session not found — sign into instagram.com in Chrome and try again."},
                    cors=True)
                return
            if not jar.get("sessionid"):
                self._json(400, {"error":
                    "Instagram session not found — sign into instagram.com in Chrome and try again."},
                    cors=True)
                return
            # Returned ONLY to the authorized loopback origin; the browser submits
            # it via the existing management-room paste path and redacts it.
            self._json(200, {"status": "cookies", "cookies": json.dumps(jar)}, cors=True)

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
