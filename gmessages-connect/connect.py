#!/usr/bin/env python3
"""
Connect Google Messages to the bridge — one command, no cookie pasting.

Prereq: you're signed into your Google account in Chrome (open the sign-in link
the Hub shows, or https://accounts.google.com/AccountChooser?continue=https://messages.google.com/web/config).

What it does:
  1. Reads + decrypts the Google session cookies from Chrome's store
     (you approve one macOS Keychain prompt — "Chrome Safe Storage").
  2. Submits them to the bridge's provisioning API (never as a chat message).
  3. Shows the emoji to tap in the Google Messages app on your phone.
  4. Waits for you to tap it, then confirms "Connected".

Run from the repo root:  python3 gmessages-connect/connect.py
"""
import sqlite3, subprocess, hashlib, json, os, sys, shutil, string, re, time, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(REPO, "gmessages", "config.yaml")
USER_ID = "@jkali:localhost"
SERVICE = "mautrix-gmessages"
BASE = "http://localhost:29336/_matrix/provision/v3"
STEP_ID = "fi.mau.gmessages.google_account"

def die(msg):
    print("ERROR:", msg); sys.exit(1)

def valid_login_id(s):
    """F2: True only for a bridge login_id of ^[A-Za-z0-9_-]+$.

    The server validates any bridge-returned login_id with this BEFORE it is
    interpolated into a provisioning-API path, so a hostile/garbled bridge
    response can never inject path segments or query into the URL.
    """
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", s or ""))

def shared_secret():
    with open(CONFIG) as f:
        for line in f:
            m = re.match(r"\s*shared_secret:\s*(\S+)", line)
            if m and m.group(1) not in ("generate", "null", "disable", '""'):
                return m.group(1)
    die("provisioning shared_secret not found in gmessages/config.yaml")

def api(path, secret, body=None, method="POST", timeout=15):
    """Call the provisioning API inside the bridge container via docker compose exec."""
    url = f"{BASE}{path}?user_id={USER_ID}"
    wget = ("wget -qO- -T %d --header='Authorization: Bearer %s' "
            "--header='Content-Type: application/json' " % (timeout, secret))
    if body is None and method == "GET":
        wget += f"'{url}'"
        inp = None
    else:
        wget += f"--post-file=/dev/stdin '{url}'"
        inp = (json.dumps(body) if body is not None else "{}").encode()
    p = subprocess.run(
        ["docker", "compose", "exec", "-T", SERVICE, "sh", "-c", wget],
        input=inp, capture_output=True, cwd=REPO, timeout=timeout + 20,
    )
    out = p.stdout.decode("utf-8", "replace").strip()
    return out

def decrypt_cookies():
    ch = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    src = os.path.join(ch, "Network", "Cookies")
    if not os.path.exists(src):
        src = os.path.join(ch, "Cookies")
    if not os.path.exists(src):
        die("Chrome cookie store not found — is Chrome installed / the Default profile used?")
    try:
        pw = subprocess.check_output(
            ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"]
        ).strip()
    except subprocess.CalledProcessError:
        die("Keychain access denied — click Allow on the 'Chrome Safe Storage' prompt and re-run.")
    key = hashlib.pbkdf2_hmac("sha1", pw, b"saltysalt", 1003, 16)

    def dec(enc, host):
        if enc[:3] != b"v10":
            return enc.decode("utf-8", "replace")
        p = subprocess.run(
            ["openssl", "enc", "-aes-128-cbc", "-d", "-K", key.hex(),
             "-iv", "20" * 16, "-nopad"],
            input=enc[3:], capture_output=True)
        pt = p.stdout
        if pt:
            pad = pt[-1]
            if 1 <= pad <= 16:
                pt = pt[:-pad]
        for h in (host, host.lstrip(".")):
            if pt[:32] == hashlib.sha256(h.encode()).digest():
                return pt[32:].decode("utf-8", "replace")
        if len(pt) > 32 and not all(chr(b) in string.printable for b in pt[:8]):
            pt = pt[32:]
        return pt.decode("utf-8", "replace")

    targets = [(".google.com", n) for n in
               ("SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSIDTS")]
    targets.append(("messages.google.com", "OSID"))
    # F3: copy the store to a PRIVATE 0600 temp file (mkstemp), and wrap
    # copy -> query -> delete in try/finally so a crash never orphans a
    # readable copy of the (decrypted-adjacent) cookie DB in a world-listable
    # /tmp with a predictable, guessable name.
    fd, tmp = tempfile.mkstemp(prefix="gm_ck_", suffix=".db")
    os.close(fd)  # mkstemp file is already mode 0600; copyfile preserves it
    out = {}
    try:
        shutil.copyfile(src, tmp)  # content only — keeps the 0600 of the dest
        con = sqlite3.connect(tmp)
        try:
            for host, name in targets:
                r = con.execute(
                    "SELECT encrypted_value FROM cookies WHERE host_key=? AND name=?",
                    (host, name)).fetchone()
                if r and r[0]:
                    v = dec(r[0], host)
                    if v:
                        out[name] = v
        finally:
            con.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    missing = [n for n in ("SID", "HSID", "SSID", "APISID", "SAPISID", "OSID")
               if not out.get(n)]
    if missing:
        die("missing cookies %s — sign into messages.google.com in Chrome first, then re-run."
            % missing)
    return out

def main():
    secret = shared_secret()
    print("• Reading your Google session from Chrome (approve the Keychain prompt if it appears)…")
    cookies = decrypt_cookies()
    print("  got %d cookies." % len(cookies))

    print("• Starting login…")
    start = api("/login/start/google", secret, body={})
    m = re.search(r'"login_id":"([^"]+)"', start)
    if not m:
        die("could not start login: %s" % start[:300])
    lid = m.group(1)

    print("• Submitting session to the bridge…")
    resp = api(f"/login/step/{lid}/{STEP_ID}/cookies", secret, body=cookies)
    if '"display_and_wait"' not in resp:
        die("cookie submit failed (session may be stale — re-sign-in to Google): %s" % resp[:300])
    em = re.search(r'"data":"([^"]+)"', resp)
    emoji = em.group(1) if em else "(shown on your phone)"

    print("\n  ┌─────────────────────────────────────────────┐")
    print("  │  On your phone: open Google Messages,         │")
    print("  │  and TAP THIS EMOJI:   %-4s                   │" % emoji)
    print("  └─────────────────────────────────────────────┘\n")
    print("• Waiting for you to tap it (up to ~2 min)…")

    resp = api(f"/login/step/{lid}/fi.mau.gmessages.emoji/display_and_wait",
               secret, body={}, timeout=110)
    if '"type":"complete"' in resp:
        who = re.search(r'"user_login_id":"([^"]+)"', resp)
        print("\n✅ Connected%s. Your chats will sync into the Google Messages space shortly."
              % (" as " + who.group(1).split("/")[0] if who else ""))
    else:
        die("did not complete (did you tap the emoji in time?): %s" % resp[:300])

if __name__ == "__main__":
    main()
