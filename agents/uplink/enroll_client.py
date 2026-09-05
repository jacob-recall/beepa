#!/usr/bin/env python3
"""agents/uplink/enroll_client.py — teammate-side enrollment redemption.

One-time step a teammate runs on their OWN machine to onboard the uplink,
replacing the manual scoped-token copy/paste (PLAN-MASTER-SYNC.md §5.3 v1.5).

It POSTs a one-time enrollment code to the master's loopback/TLS-fronted
exchange endpoint (master/enroll.py serve) and writes the returned scoped
credentials to a local, mode-600, shell-sourceable env file that uplink.py
consumes:

    MASTER_HS_URL / MASTER_USER / MASTER_TOKEN / MANAGER_MXID / MASTER_SPACE

Then, to run the daemon:

    set -a; . uplink.env.local; set +a
    LOCAL_HS_URL=... LOCAL_USER=... LOCAL_TOKEN=... python3 uplink.py

Outbound-only, stdlib only. Nothing here can send externally or read the master
beyond this single exchange.

Usage:
    python3 enroll_client.py --enroll-url https://master.example/  \
        --code <CODE>  [--out ./uplink.env.local]
"""
import argparse
import json
import os
import shlex
import sys
import urllib.error
import urllib.request

FIELDS = ("MASTER_HS_URL", "MASTER_USER", "MASTER_TOKEN",
          "MANAGER_MXID", "MASTER_SPACE")

# map the exchange JSON keys -> the env var names uplink.py reads
_KEY_TO_ENV = {
    "master_hs_url": "MASTER_HS_URL",
    "master_user": "MASTER_USER",
    "master_token": "MASTER_TOKEN",
    "manager_mxid": "MANAGER_MXID",
    "master_space": "MASTER_SPACE",
}
OPTIONAL_KEY_TO_ENV = {
    "master_authority_id": "MASTER_AUTHORITY_ID",
    "master_data_epoch": "MASTER_DATA_EPOCH",
    "master_enroll_url": "MASTER_ENROLL_URL",
}


def exchange(enroll_url, code, timeout=30):
    """POST the code to the master exchange endpoint -> the enrollment dict.

    Raises RuntimeError with the server's reason on a refusal (403) or any
    non-200 response.
    """
    body = json.dumps({"code": code}).encode()
    req = urllib.request.Request(
        enroll_url.rstrip("/") + "/enroll/exchange", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        reason = e.read().decode(errors="replace")
        try:
            reason = json.loads(reason).get("error", reason)
        except ValueError:
            pass
        raise RuntimeError("enrollment refused (HTTP %s): %s" % (e.code, reason))


def write_env(data, out_path):
    """Write the scoped credentials to a mode-600 shell-sourceable env file."""
    missing = [k for k in _KEY_TO_ENV if not data.get(k)]
    # MANAGER_MXID is the only field that may legitimately be empty; require the rest
    hard = [k for k in missing if k != "manager_mxid"]
    if hard:
        raise RuntimeError("exchange response missing fields: %s" % ", ".join(hard))
    lines = ["# uplink enrollment credentials — mode 600, do NOT commit.",
             "# Written by enroll_client.py; source before running uplink.py."]
    for jkey, env in {**_KEY_TO_ENV, **OPTIONAL_KEY_TO_ENV}.items():
        lines.append("%s=%s" % (env, shlex.quote(str(data.get(jkey, "")))))
    payload = ("\n".join(lines) + "\n").encode()
    tmp = out_path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(payload)
    os.replace(tmp, out_path)
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass
    return out_path


def exchange_and_store(enroll_url, code, out_path):
    data = exchange(enroll_url, code)
    data["master_enroll_url"] = enroll_url.rstrip("/")
    write_env(data, out_path)
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="redeem a uplink enrollment code")
    ap.add_argument("--enroll-url", required=True,
                    help="master exchange base URL (e.g. https://master.example)")
    ap.add_argument("--code", required=True, help="the one-time enrollment code")
    ap.add_argument("--out", default="uplink.env.local",
                    help="env file to write (mode 600; default ./uplink.env.local)")
    args = ap.parse_args(argv)
    try:
        exchange_and_store(args.enroll_url, args.code, args.out)
    except RuntimeError as e:
        sys.stderr.write("enroll_client: %s\n" % e)
        return 3
    sys.stderr.write("enroll_client: wrote scoped credentials to %s (mode 600)\n"
                     % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
