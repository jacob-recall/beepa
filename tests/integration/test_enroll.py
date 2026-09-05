#!/usr/bin/env python3
"""Enrollment-flow integration test (PLAN-MASTER-SYNC.md §5.3 v1.5 / IMPL P3.2).

Proves the one-time enrollment-code exchange against a disposable master
homeserver on an allocated loopback port, end-to-end through the loopback serve endpoint the
uplink actually talks to (master/enroll.py serve) plus the teammate-side client
(agents/uplink/enroll_client.py):

  1. valid code  -> a FRESH token SCOPED to that teammate: it works for that
     teammate (whoami == the teammate, reads their own space) and is REFUSED
     (403) when it tries to write another teammate's space  -> cross-user
     isolation holds. The client stores it to a mode-600 env file.
  2. reused code -> refused (403).
  3. expired code -> refused (403).
  4. invalid code -> refused (403).

Never touches the live matrix-wa stack (8008/8009/8010) and does not re-run or
weaken provisioning; it only mints/redeems codes and logs in as existing
teammate accounts.

Run:  tests/integration/run.sh --enrollment
Exit: 0 all pass, 1 otherwise.
"""
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
MASTER_DIR = os.path.join(REPO, "master")
ENROLL_PY = os.path.join(MASTER_DIR, "enroll.py")
CLIENT_PY = os.path.join(REPO, "agents", "uplink", "enroll_client.py")
from sandbox import load_manifest
SANDBOX = load_manifest()
MASTER_HS = SANDBOX["master_url"]
STATE_DIR = SANDBOX["state_dir"]
STORE = os.path.join(STATE_DIR, "enrollments.local")
sys.path.insert(0, MASTER_DIR)
os.environ["ENROLL_STORE"] = STORE
os.environ["BEEPA_MASTER_STATE_DIR"] = SANDBOX["master_dir"]
os.environ["MASTER_CS_BASE"] = MASTER_HS
os.environ["MASTER_PUBLIC_URL"] = MASTER_HS
import enroll  # noqa: E402
sys.path.insert(0, REPO)
from install_config import read_env


# ------------------------------------------------------------------ helpers
def load_tokens():
    return read_env(os.path.join(SANDBOX["master_dir"], "tokens.local"))


TOK = load_tokens()
SPACE_BOB = TOK["MASTER_SPACE_BOB"]
SPACE_ALICE = TOK["MASTER_SPACE_ALICE"]


def http_json(url, method="GET", token=None, body=None, timeout=15):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, method=method, data=data)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def serve():
    port = free_port()
    env = dict(os.environ, ENROLL_STORE=STORE)
    proc = subprocess.Popen(
        [sys.executable, ENROLL_PY, "serve", "--host", "127.0.0.1",
         "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                code, _ = http_json(base + "/enroll/health", timeout=2)
                if code == 200:
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                raise RuntimeError("enroll serve exited early")
            time.sleep(0.1)
        else:
            raise RuntimeError("enroll serve never became healthy")
        yield base
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def client_exchange(base, code, out):
    """Drive the real teammate-side client; returns (rc, stored_dict_or_None)."""
    env = dict(os.environ)
    rc = subprocess.call(
        [sys.executable, CLIENT_PY, "--enroll-url", base, "--code", code,
         "--out", out], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if rc != 0:
        return rc, None
    return rc, read_env(out)


# ------------------------------------------------------------------ the test
def run():
    # fresh store
    with contextlib.suppress(FileNotFoundError):
        os.remove(STORE)

    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print("  %-42s %s  %s" % (name, "PASS" if ok else "FAIL", detail))

    with serve() as base:
        # ---- 1. valid code -> scoped token, works for alice, 403 for bob ----
        code = enroll.mint("alice")
        out = os.path.join(STATE_DIR, "uplink.env.local")
        rc, stored = client_exchange(base, code, out)
        check("valid_code_exchange_rc0", rc == 0, "rc=%s" % rc)

        token = (stored or {}).get("MASTER_TOKEN", "")
        check("scoped_creds_returned",
              bool(token) and (stored or {}).get("MASTER_USER") == "@alice:master"
              and (stored or {}).get("MASTER_SPACE") == SPACE_ALICE,
              "user=%s" % (stored or {}).get("MASTER_USER"))

        # env file must be mode 600
        mode = os.stat(out).st_mode & 0o777
        check("stored_env_mode_600", mode == 0o600, "mode=%o" % mode)

        # the fresh token IS alice
        wc, who = http_json(MASTER_HS + "/_matrix/client/v3/account/whoami",
                            token=token)
        check("token_whoami_is_alice",
              wc == 200 and who.get("user_id") == "@alice:master",
              "whoami=%s" % who.get("user_id"))

        # ... and can read alice's OWN space (works for that teammate)
        rc_own, _ = http_json(
            MASTER_HS + "/_matrix/client/v3/rooms/"
            + urllib.parse.quote(SPACE_ALICE, safe="")
            + "/state/m.room.create/", token=token)
        check("token_reads_own_space", rc_own == 200, "http=%s" % rc_own)

        # ... but is REFUSED (403) writing bob's space -> isolation holds
        wc2, _ = http_json(
            MASTER_HS + "/_matrix/client/v3/rooms/"
            + urllib.parse.quote(SPACE_BOB, safe="")
            + "/state/m.space.child/"
            + urllib.parse.quote(SPACE_ALICE, safe=""),
            method="PUT", token=token, body={"via": ["master"]})
        check("token_refused_bob_space_403", wc2 == 403, "http=%s" % wc2)

        # ---- 2. reused code -> refused ----
        rc2, _ = http_json(base + "/enroll/exchange", method="POST",
                           body={"code": code})
        check("reused_code_refused_403", rc2 == 403, "http=%s" % rc2)

        # ---- 3. expired code -> refused ----
        exp = enroll.mint("alice", ttl=600)
        store = enroll._load_store()          # backdate its expiry deterministically
        store["codes"][enroll._hash(exp)]["expires_at"] = int(time.time()) - 5
        enroll._save_store(store)
        rc3, body3 = http_json(base + "/enroll/exchange", method="POST",
                              body={"code": exp})
        check("expired_code_refused_403",
              rc3 == 403 and "expired" in json.dumps(body3), "http=%s" % rc3)

        # ---- 4. invalid code -> refused ----
        rc4, _ = http_json(base + "/enroll/exchange", method="POST",
                          body={"code": "not-a-real-code-xxxxxxxxxxxxxxxx"})
        check("invalid_code_refused_403", rc4 == 403, "http=%s" % rc4)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\nenroll: %d/%d checks passed" % (passed, total))
    return passed == total


if __name__ == "__main__":
    print("== disposable enrollment flow ==")
    ok = run()
    print("\nRESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
