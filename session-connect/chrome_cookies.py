#!/usr/bin/env python3
"""Read + decrypt cookies from Chrome's on-disk store (macOS, every profile).

Shared by the session-connect helpers. Reads whatever account is already
signed into Chrome — the Hub never has a login form. Cookie VALUES are never
printed or logged; only names/counts are surfaced.

Chrome keeps one cookie store PER profile (Default, "Profile 1", …); the user's
login for a given site may live in any of them, so read() scans them all and
picks the profile that actually holds the logged-in session (see `prefer`).

Same decryption approach as gmessages-connect/connect.py: copy the cookie DB,
derive the AES key from the "Chrome Safe Storage" Keychain item, decrypt the
v10 blobs (stripping the sha256(host) prefix Chrome prepends). The Safe Storage
key is per-user, not per-profile, so one key decrypts every profile.
"""
import sqlite3, subprocess, hashlib, os, shutil, string, tempfile


class CookieError(Exception):
    pass


def _cookie_dbs():
    """Every profile's cookie DB path (Default + "Profile N"), newest-first is
    not implied — selection happens in read(). "System Profile" is skipped."""
    base = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    dbs = []
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return dbs
    for prof in entries:
        if prof != "Default" and not prof.startswith("Profile "):
            continue
        for rel in ("Network/Cookies", "Cookies"):
            p = os.path.join(base, prof, rel)
            if os.path.exists(p):
                dbs.append(p)
                break
    return dbs


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


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _read_one(src, host, names, key):
    """Decrypt one profile's cookies for `host` (the site itself and its
    subdomains, never a bare '...host' suffix) into {name: value}."""
    # Unpredictable, atomically-created 0600 name (never a guessable /tmp path a
    # co-resident process could pre-plant as a symlink) — matches gmessages-connect.
    fd, tmp = tempfile.mkstemp(prefix="sc_ck_", suffix=".db")
    os.close(fd)
    try:
        # copyfile (not copy2) so the temp keeps its 0600 mode instead of
        # inheriting the source's; keep the copy inside the try so a failure
        # can't leak the file.
        shutil.copyfile(src, tmp)
        con = sqlite3.connect(tmp)
        try:
            rows = con.execute(
                "SELECT name, host_key, encrypted_value FROM cookies "
                "WHERE host_key = ? OR host_key LIKE ?",
                (host, "%." + host)).fetchall()
        finally:
            con.close()
    finally:
        _rm(tmp)
        _rm(tmp + "-wal")   # WAL/SHM siblings sqlite may have created alongside
        _rm(tmp + "-shm")
    out = {}
    for name, khost, enc in rows:
        if names and name not in names:
            continue
        if enc:
            v = _dec(enc, khost, key)
            if v:
                out[name] = v
    return out


def read(domain_like, names=None, prefer=None):
    """Return {name: value} for cookies of the exact site host `domain_like`
    (e.g. 'instagram.com' — the host itself plus its subdomains, so a same-named
    cookie from an unrelated '...x.com' host can never shadow the real one),
    across ALL Chrome profiles. If names is given, keep only those. `prefer`
    names the cookie that marks a logged-in session (e.g. 'auth_token'): when
    set, the profile whose jar carries it wins, so a stray logged-out profile
    can't shadow the real one. Falls back to the profile with the most matching
    cookies. Raises CookieError with an actionable message on any failure. Never
    merges across profiles — a returned jar is one profile's session, so it
    can't mix two accounts."""
    dbs = _cookie_dbs()
    if not dbs:
        raise CookieError(
            "Chrome cookie store not found — is Chrome installed?")
    try:
        key = _key()
    except subprocess.CalledProcessError:
        raise CookieError(
            "Keychain access denied — click Allow on the 'Chrome Safe Storage' prompt and re-run.")
    jars = []
    for src in dbs:
        try:
            jar = _read_one(src, domain_like, names, key)
        except (sqlite3.Error, OSError):
            continue   # one locked/corrupt profile must not abort the scan
        if jar:
            jars.append(jar)
    if not jars:
        return {}
    if prefer:
        signed_in = [j for j in jars if j.get(prefer)]
        if signed_in:
            return max(signed_in, key=len)
    return max(jars, key=len)
