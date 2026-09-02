#!/usr/bin/env python3
"""Unit test for the session-connect helper's /enroll/exchange endpoint (the
GUI "Connect to organization" server-side leg). Drives the REAL handler over a
loopback ephemeral port — no docker, no external network. Proves the F1 gate
and the SSRF containment reject before any outbound call, and that an
authorized-but-doomed exchange fails generically (never leaks / never hangs).

Why a socket: the F1 guard is HTTP-level (Origin / Content-Type / custom
header), so we exercise it as HTTP. We call _make_handler() directly (NOT
serve(), which binds the 8021-8025 range and writes apps/user/connect.local.json
as a side effect)."""
import http.server
import json
import os
import sys
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "session-connect"))
import connect_server  # noqa: E402

P = connect_server.ENROLL_EXCHANGE_PATH
APP = "http://127.0.0.1:8011"
FULL = {"Origin": APP, "Content-Type": "application/json", "X-Beepa-Connect": "1"}

fails = 0


def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1


def start():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), connect_server._make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def call(httpd, headers, body=b"{}", method="POST"):
    port = httpd.server_address[1]
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, P),
                                 data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


httpd = start()
try:
    # ---- F1 gate: every rejection happens BEFORE any outbound call ----
    check("no Origin -> 403",
          call(httpd, {"Content-Type": "application/json", "X-Beepa-Connect": "1"})[0] == 403)
    check("wrong Origin -> 403",
          call(httpd, {"Origin": "http://evil.example", "Content-Type": "application/json",
                       "X-Beepa-Connect": "1"})[0] == 403)
    check("missing X-Beepa-Connect -> 403",
          call(httpd, {"Origin": APP, "Content-Type": "application/json"})[0] == 403)
    check("wrong Content-Type -> 415",
          call(httpd, {"Origin": APP, "Content-Type": "text/plain", "X-Beepa-Connect": "1"})[0] == 415)

    # ---- authorized, but malformed / contained: fail without egress ----
    check("missing master_url -> 400",
          call(httpd, FULL, json.dumps({"code": "x"}).encode())[0] == 400)
    check("missing code -> 400",
          call(httpd, FULL, json.dumps({"master_url": "https://m.example"}).encode())[0] == 400)
    # SSRF containment: plain http to a NON-loopback host is refused at the gate,
    # so no outbound request is ever made to it.
    check("http non-loopback master rejected -> 400",
          call(httpd, FULL, json.dumps({"master_url": "http://198.51.100.7", "code": "x"}).encode())[0] == 400)
    check("non-http(s) scheme rejected -> 400",
          call(httpd, FULL, json.dumps({"master_url": "file:///etc/passwd", "code": "x"}).encode())[0] == 400)
    # Allowed shape but unreachable master -> generic 502 failure (no hang/leak).
    code, body = call(httpd, FULL, json.dumps({"master_url": "https://127.0.0.1:1", "code": "x"}).encode())
    check("unreachable https master -> 502 failed", code == 502 and json.loads(body).get("status") == "failed")

    # ---- OPTIONS preflight from the app origin is allowed ----
    check("OPTIONS from app origin -> 204",
          call(httpd, {"Origin": APP}, body=None, method="OPTIONS")[0] == 204)
finally:
    httpd.shutdown()

print("PASS" if fails == 0 else "FAILED (%d)" % fails)
sys.exit(1 if fails else 0)
