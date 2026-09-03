#!/usr/bin/env python3
"""Unit test for master/enroll.py's _advertised_hs_url() loopback refusal.

Regression for the works-macbook-pro enrollment failure: with
MASTER_PUBLIC_URL unset, exchange() silently advertised the master's own
http://127.0.0.1:8018 as master_hs_url — a successful-LOOKING enrollment
that was guaranteed broken on any other machine. The guard must refuse the
SILENT fallback to a loopback base (with actionable guidance) while still
allowing an EXPLICIT loopback (env or tokens.local) so single-host/local
test setups keep working — the gate is on provenance, not the string.

_advertised_hs_url() reads env + tokens.local through two seams
(os.environ, enroll._tokens); we stub both and assert the pure decision.

Run: python3 tests/unit/enroll_public_url.test.py  (exit 0 = all pass).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "master"))
import enroll  # noqa: E402

_pass = 0
_fail = 0
_failures = []

_ENV_KEYS = ("MASTER_PUBLIC_URL", "MASTER_CS_BASE")
_saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}


def _setup(env=None, tokens=None):
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in (env or {}).items():
        os.environ[k] = v
    enroll._tokens = lambda: dict(tokens or {})


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        _failures.append(name)


# 1. Silent fallback to loopback -> refused, with actionable guidance.
_setup()
try:
    enroll._advertised_hs_url()
    check("silent loopback fallback refused", False)
except enroll.EnrollError as e:
    check("silent loopback fallback refused", True)
    check("refusal names tailscale-serve.sh", "tailscale-serve.sh" in str(e))

# 2. Explicit loopback via env -> allowed (single-host/local test setups).
_setup(env={"MASTER_PUBLIC_URL": "http://127.0.0.1:8018"})
check("explicit env loopback allowed",
      enroll._advertised_hs_url() == "http://127.0.0.1:8018")

# 3. Explicit loopback via tokens.local -> allowed.
_setup(tokens={"MASTER_PUBLIC_URL": "http://localhost:8018"})
check("explicit tokens loopback allowed",
      enroll._advertised_hs_url() == "http://localhost:8018")

# 4. tokens.local tailnet URL -> returned verbatim (trailing slash stripped).
_setup(tokens={"MASTER_PUBLIC_URL": "https://master.example.ts.net/"})
check("tokens tailnet url returned",
      enroll._advertised_hs_url() == "https://master.example.ts.net")

# 5. env wins over tokens.
_setup(env={"MASTER_PUBLIC_URL": "https://env.example.ts.net"},
       tokens={"MASTER_PUBLIC_URL": "https://tok.example.ts.net"})
check("env wins over tokens",
      enroll._advertised_hs_url() == "https://env.example.ts.net")

# 6. Non-loopback _cs_base fallback (MASTER_CS_BASE pointing off-host) is not
#    refused — only a LOOPBACK silent fallback is the broken case.
_setup(env={"MASTER_CS_BASE": "https://cs.example.ts.net"})
check("non-loopback cs_base fallback allowed",
      enroll._advertised_hs_url() == "https://cs.example.ts.net")

# 7. ::1 is loopback too.
_setup(env={"MASTER_CS_BASE": "http://[::1]:8018"})
try:
    enroll._advertised_hs_url()
    check("silent ::1 fallback refused", False)
except enroll.EnrollError:
    check("silent ::1 fallback refused", True)

# restore env for any later in-process user
for k, v in _saved_env.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

print("%d passed, %d failed" % (_pass, _fail))
if _failures:
    print("FAILED: " + ", ".join(_failures))
sys.exit(1 if _fail else 0)
