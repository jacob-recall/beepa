#!/usr/bin/env python3
"""Read + decrypt cookies from Chrome's on-disk store (macOS, Default profile).

Shared by the session-connect helpers. Reads whatever account is already
signed into Chrome — the Hub never has a login form. Cookie VALUES are never
printed or logged; only names/counts are surfaced.

Same decryption approach as gmessages-connect/connect.py: copy the cookie DB,
derive the AES key from the "Chrome Safe Storage" Keychain item, decrypt the
v10 blobs (stripping the sha256(host) prefix Chrome prepends).
"""
import sqlite3, subprocess, hashlib, os, shutil, string, tempfile


class CookieError(Exception):
    pass


def _cookie_db():
    base = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")
    for rel in ("Network/Cookies", "Cookies"):
        p = os.path.join(base, rel)
        if os.path.exists(p):
            return p
    return None


def _key():
    pw = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"]).strip()
    return hashlib.pbkdf2_hmac("sha1", pw, b"saltysalt", 1003, 16)


def _dec(enc, host, key):
    if enc[:3] != b"v10":
        return enc.decode("utf-8", "replace")
    p = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-d", "-K", key.hex(),
         "-iv", "20" * 16, "-nopad"], input=enc[3:], capture_output=True)
    pt = p.stdout
    if pt:
        pad = pt[-1]
        if 1 <= pad <= 16:
            pt = pt[:-pad]
    # Chrome (v10+) prepends sha256(host_key) to the plaintext; strip it.
    for h in (host, host.lstrip(".")):
        if pt[:32] == hashlib.sha256(h.encode()).digest():
            return pt[32:].decode("utf-8", "replace")
    if len(pt) > 32 and not all(chr(b) in string.printable for b in pt[:8]):
        pt = pt[32:]
    return pt.decode("utf-8", "replace")


def read(domain_like, names=None):
    """Return {name: value} for cookies whose host_key matches the SQL LIKE
    pattern domain_like (e.g. '%instagram.com'). If names is given, keep only
    those. Raises CookieError with an actionable message on any failure."""
    src = _cookie_db()
    if not src:
        raise CookieError(
            "Chrome cookie store not found — is Chrome installed and using the Default profile?")
    # Unpredictable, atomically-created 0600 name (never a guessable /tmp path a
    # co-resident process could pre-plant as a symlink) — matches gmessages-connect.
    fd, tmp = tempfile.mkstemp(prefix="sc_ck_", suffix=".db")
    os.close(fd)
    shutil.copy2(src, tmp)
    try:
        try:
            key = _key()
        except subprocess.CalledProcessError:
            raise CookieError(
                "Keychain access denied — click Allow on the 'Chrome Safe Storage' prompt and re-run.")
        con = sqlite3.connect(tmp)
        try:
            rows = con.execute(
                "SELECT name, host_key, encrypted_value FROM cookies WHERE host_key LIKE ?",
                (domain_like,)).fetchall()
        finally:
            con.close()
    finally:
        os.remove(tmp)
    out = {}
    for name, host, enc in rows:
        if names and name not in names:
            continue
        if enc:
            v = _dec(enc, host, key)
            if v:
                out[name] = v
    return out
