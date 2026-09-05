#!/usr/bin/env python3
"""gmessages-connect/connect_server.py — the loopback one-click connect helper.

Wraps gmessages-connect/connect.py so the teammate app (apps/user, served at
http://127.0.0.1:8011) can drive the Google Messages login in one click: the
browser cannot read Chrome's cookie store or `docker compose exec` the bridge,
so this small local service does it on the browser's behalf. It mirrors the
shape of master/enroll.py's `serve`: a single-threaded loopback HTTPServer,
silent access log, and a CORS preflight locked to exactly one browser origin.

Security invariants (do not weaken)
-----------------------------------
  * Binds EXACTLY 127.0.0.1:8020 — loopback only, never 0.0.0.0 / "". Nothing
    off this machine can reach it.
  * F1 — the load-bearing control: EVERY do_POST is gated by `_authorized()`
    BEFORE any side effect (before a single cookie is read or any bridge call
    is made). The gate requires all three, fail-closed:
      (a) Origin request header == http://127.0.0.1:8011 exactly (the primary
          gate; a missing / "null" / different Origin is refused 403);
      (b) Content-Type == application/json;
      (c) a custom X-Beepa-Connect: 1 header is present.
    (b)+(c) are not simple-request headers, so a cross-origin page is forced
    into a CORS preflight, which fails because the server only ever echoes the
    one allowed origin — so a hostile web page cannot even reach the POST body.
    (a) is the real authorization: only the local app's origin passes.
  * The health endpoint (GET /connect/health) has ZERO side effects: it never
    reads cookies, never touches the Keychain, never calls the bridge, and
    carries no CORS headers (F5). Pure liveness.
  * No path decrypts cookies, reads the Keychain, or starts a login at import,
    at process start, on a timer, or from health — ONLY an authorized POST to
    /connect/gmessages/start does (F5/F7).
  * Cookies, the provisioning shared_secret, and raw bridge response bodies are
    NEVER returned to the client and NEVER written to the logs (F6). Bridge /
    cookie failures are mapped to fixed, generic messages. The only values that
    leave this process are the tap-emoji and the account localpart.
  * The bridge-returned login_id is validated with connect.valid_login_id()
    before it is interpolated into a provisioning-API path (F2), and is held
    ONLY server-side in a module global — the /wait endpoint takes NO body
    params, so the client can never supply or override it.

Single in-flight login (single-user machine): a second /start simply overwrites
the stored login_id; the previous in-progress login is abandoned.

Run:  python3 connect_server.py --host 127.0.0.1 --port 8020
(usually via run-connect.sh under launchd — com.jkali.gmessages-connect).
"""
import argparse
import json
import re
import sys
import time

import connect  # gmessages-connect/connect.py — same dir; imported side-effect-free

from http_limits import BoundedBodyMixin

# The teammate app's browser origin (apps/user, served by the `views` nginx).
# 127.0.0.1 and localhost are the same loopback app — a viewer may open either,
# so BOTH are allowed; nothing else is (never "*"). An off-machine or foreign
# origin is still refused. These are aliases of the user's OWN app, not a
# widening to any other site.
APP_ORIGINS = ("http://127.0.0.1:8011", "http://localhost:8011")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8020

# Server-held state for the single in-flight login. The client never sees or
# supplies this — /wait uses ONLY what /start stored here (F2).
_login_id = None


def _make_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BoundedBodyMixin, BaseHTTPRequestHandler):
        def log_message(self, *a):  # F6: no access log (never risk logging a header/body)
            pass

        # Diagnostic line: METHOD PATH origin -> status. Safe — the Origin
        # header is not a secret and NO body/cookie/header value beyond Origin
        # is ever written. Lets a silent 403/415 be seen when diagnosing.
        def _diag(self, status):
            try:
                sys.stderr.write("[connect] %s %s origin=%r -> %d\n"
                                 % (self.command, self.path,
                                    self.headers.get("Origin"), status))
                sys.stderr.flush()
            except Exception:
                pass

        def _origin(self):
            """The request Origin if it is one of the allowed loopback aliases,
            else None (fail closed)."""
            og = self.headers.get("Origin")
            return og if og in APP_ORIGINS else None

        # CORS echoed ONLY for an allowed origin, only on the POST endpoints
        # (health carries none — F5). Never "*".
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
            return self._bounded_body(65536) is not None


        # ---- F1: the authorization gate, run at the TOP of every do_POST,
        # BEFORE any cookie read or bridge call. Fails closed on all three.
        def _authorized(self):
            if self._origin() is None:                     # (a) primary gate
                self._json(403, {"error": "forbidden"}, cors=True); self._diag(403)
                return False
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":               # (b)
                self._json(415, {"error": "unsupported media type"}, cors=True); self._diag(415)
                return False
            if self.headers.get("X-Beepa-Connect") != "1":  # (c)
                self._json(403, {"error": "forbidden"}, cors=True); self._diag(403)
                return False
            return True

        def do_OPTIONS(self):
            # Preflight only for the allowed origin + the POST endpoints.
            if (self._origin() is not None
                    and self.path in ("/connect/gmessages/start",
                                      "/connect/gmessages/wait")):
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
            # F5: liveness only — no cookies, no Keychain, no bridge, no CORS.
            if self.path == "/connect/health":
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            self._diag(-1)   # arrival (before auth) — origin visible for diagnosis
            if self.path == "/connect/gmessages/start":
                if not self._authorized():   # F1: gate BEFORE any side effect
                    return
                if not self._discard_body():
                    return
                self._start()
            elif self.path == "/connect/gmessages/wait":
                if not self._authorized():   # F1: gate BEFORE any side effect
                    return
                if not self._discard_body():
                    return
                self._wait()
            else:
                self._json(404, {"error": "not found"})

        # ---- POST /connect/gmessages/start ---------------------------------
        def _start(self):
            global _login_id
            # (1) provisioning secret (bridge auth). connect.die() raises
            # SystemExit — catch it so a config problem never kills the server.
            try:
                secret = connect.shared_secret()
            except SystemExit:
                self._json(500, {"error": "Could not start Google Messages login."}, cors=True)
                return
            # (2) read + decrypt the Google session cookies from Chrome. Any
            # failure here (no store, missing cookies, Keychain denied) maps to
            # the one generic "sign into Google" message — never the raw cause.
            try:
                cookies = connect.decrypt_cookies()
            except BaseException:  # SystemExit(die) or any unexpected error
                self._json(400, {"error":
                    "Google session not found — sign into Google in Chrome and try again."},
                    cors=True)
                return
            # (3) start the bridge login and read back a login_id.
            try:
                start = connect.api("/login/start/google", secret, body={})
            except BaseException:
                self._json(502, {"error": "Could not start Google Messages login."}, cors=True)
                return
            m = re.search(r'"login_id":"([^"]+)"', start)
            if not m:
                self._json(502, {"error": "Could not start Google Messages login."}, cors=True)
                return
            lid = m.group(1)
            if not connect.valid_login_id(lid):  # F2: validate before path interpolation
                self._json(502, {"error": "Could not start Google Messages login."}, cors=True)
                return
            _login_id = lid  # F2: hold server-side ONLY (never sent to the client)
            # (4) submit the cookies to the bridge; it replies with the emoji.
            try:
                resp = connect.api(
                    "/login/step/%s/%s/cookies" % (lid, connect.STEP_ID),
                    secret, body=cookies)
            except BaseException:
                self._json(502, {"error": "Could not start Google Messages login."}, cors=True)
                return
            if '"display_and_wait"' not in resp:
                self._json(502, {"error": "Could not start Google Messages login."}, cors=True)
                return
            em = re.search(r'"data":"([^"]+)"', resp)
            emoji = em.group(1) if em else "(shown on your phone)"
            # Return ONLY the tap-emoji — never cookies, secret, or raw body (F6).
            self._json(200, {"emoji": emoji}, cors=True)

        # ---- POST /connect/gmessages/wait ----------------------------------
        def _wait(self):
            global _login_id
            lid = _login_id
            if not lid:  # F2: no client-supplied id — only the stored one
                self._json(409, {"error": "no login in progress"}, cors=True)
                return
            try:
                secret = connect.shared_secret()
            except SystemExit:
                self._json(500, {"error": "Could not start Google Messages login."}, cors=True)
                return
            t0 = time.time()
            try:
                resp = connect.api(
                    "/login/step/%s/fi.mau.gmessages.emoji/display_and_wait" % lid,
                    secret, body={}, timeout=110)
            except BaseException:
                _login_id = None  # terminal: give up on this login
                self._json(200, {"status": "failed"}, cors=True)
                return
            elapsed = time.time() - t0
            if '"type":"complete"' in resp:
                who = re.search(r'"user_login_id":"([^"]+)"', resp)
                # The localpart before "/" is safe to surface (it's the account
                # they just linked); the rest of the body is never echoed (F6).
                account = who.group(1).split("/")[0] if who else ""
                _login_id = None
                self._json(200, {"status": "complete", "account": account}, cors=True)
                return
            _login_id = None  # any non-complete result is terminal — clear it
            # Server-side wall-clock guard: an empty reply or a near-timeout wait
            # is a timeout (emoji not tapped in time); anything else is a failure.
            if not resp or elapsed >= 100:
                self._json(200, {"status": "timeout"}, cors=True)
            else:
                self._json(200, {"status": "failed"}, cors=True)

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
    ap = argparse.ArgumentParser(description="one-click Google Messages connect helper")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
