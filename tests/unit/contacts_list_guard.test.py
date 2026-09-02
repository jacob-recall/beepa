#!/usr/bin/env python3
"""Unit test for the session-connect helper's POST /contacts/list gate
(per-contact-share plan, C3.1 / F1 / F1b).

The endpoint returns the teammate's whole imported address book — a BROADER
read than /enrich/numbers — so every refusal path must reject before the
sqlite read. This test only exercises the REFUSALS on purpose: an authorized
call would read the real agents/contacts/contacts.db, and a unit test must
never pull the user's live address book into a test process.

Why a socket: the gates are HTTP-level (Origin / Content-Type / custom header /
Host), so they are exercised as HTTP. We call _make_handler() directly, NOT
serve(), which would bind the 8021-8025 range and rewrite
apps/user/connect.local.json as a side effect.

Run: python3 tests/unit/contacts_list_guard.test.py
"""
import http.server
import os
import sys
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "session-connect"))
import connect_server  # noqa: E402

APP = "http://127.0.0.1:8011"
GOOD = {"Content-Type": "application/json", "X-Beepa-Connect": "1"}

fails = 0


def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails += 1


httpd = http.server.HTTPServer(("127.0.0.1", 0), connect_server._make_handler())
PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

# The handler pins Host to the port serve() bound; this test binds its own, so
# point the allowlist at it (serve() does exactly this assignment).
connect_server._BOUND_PORT = PORT
HOST_OK = "127.0.0.1:%d" % PORT


def call(headers, method="POST", path=None):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path or connect_server.CONTACTS_LIST_PATH),
        data=b"{}" if method == "POST" else None, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


try:
    # ---- F1: the same gate as every other POST, before any DB read ----
    check("no Origin -> 403", call(dict(GOOD, Host=HOST_OK))[0] == 403)
    check("wrong Origin -> 403",
          call(dict(GOOD, Origin="http://evil.example", Host=HOST_OK))[0] == 403)
    check("missing X-Beepa-Connect -> 403",
          call({"Origin": APP, "Content-Type": "application/json", "Host": HOST_OK})[0] == 403)
    check("wrong Content-Type -> 415",
          call({"Origin": APP, "Content-Type": "text/plain",
                "X-Beepa-Connect": "1", "Host": HOST_OK})[0] == 415)

    # ---- F1b: the Host allowlist (anti-DNS-rebinding defence-in-depth) ----
    check("rebound Host -> 403",
          call(dict(GOOD, Origin=APP, Host="attacker.example"))[0] == 403)
    check("right host, wrong port -> 403",
          call(dict(GOOD, Origin=APP, Host="127.0.0.1:1"))[0] == 403)
    check("_host_allowed accepts both loopback aliases",
          connect_server._host_allowed(HOST_OK)
          and connect_server._host_allowed("localhost:%d" % PORT))
    check("_host_allowed rejects a bare host and a non-string",
          not connect_server._host_allowed("127.0.0.1")
          and not connect_server._host_allowed(None))

    # ---- F1/F5: a GET is NOT a way in (the helper's GETs are liveness-only) ----
    check("GET /contacts/list -> 404 (never an ungated read)",
          call({"Origin": APP, "Host": HOST_OK}, method="GET")[0] == 404)

    # ---- the CORS preflight is offered for this path, to the app origin only ----
    check("OPTIONS from the app origin -> 204",
          call({"Origin": APP, "Host": HOST_OK}, method="OPTIONS")[0] == 204)
    check("OPTIONS from a foreign origin -> 404",
          call({"Origin": "http://evil.example", "Host": HOST_OK}, method="OPTIONS")[0] == 404)
finally:
    httpd.shutdown()

print("PASS" if not fails else "FAILED")
sys.exit(1 if fails else 0)
