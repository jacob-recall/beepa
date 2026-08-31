"""macOS Contacts importer: reads the local Contacts.app address book via
`osascript -l JavaScript` and upserts it into the shared contacts store.

Durability contract: a failed or garbled OS read must never mutate the
store. `import_once` reads everything first, validates the whole batch, and
only calls `contacts_store.upsert_contacts` once it has a complete `seen`
list; any error along the way returns `{"error": ...}` and touches nothing.

Normalization:
  - phones -> E.164 (`+` + digits only), validated against
    `^\\+[1-9]\\d{6,14}$`. A number that already carries a country code
    (leading `+`, or a `00` international prefix) is normalized as-is. A
    BARE national number (no country code in the source data) gets the
    Mac's system-region calling code prepended (see `_get_system_region` /
    `_REGION_CALLING_CODES`) — never minted from thin air. If no calling
    code can be resolved (unknown/unmapped region) or the number still
    doesn't validate afterward, the handle is dropped and counted as
    "ambiguous" rather than fabricated.
  - emails -> lowercased, strict-validated against a conservative regex;
    invalid ones are dropped.
  - a contact left with zero usable handles after normalization is dropped
    entirely (it can never be a `start-chat` target).

No handle values or display names are ever logged.
"""

import json
import os
import re
import subprocess
import sys

_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")

# Compact region -> calling-code map for the common case. This is
# deliberately not libphonenumber-grade: it covers a bare national number
# from the Mac's OWN region only. A bare number from a different region
# (e.g. a UK contact's local-format number on a US-region Mac) has no
# signal to disambiguate it and is dropped, never guessed. See
# agents/contacts/CLAUDE.md and bd issue for the multi-region follow-up.
_REGION_CALLING_CODES = {
    "US": "1", "CA": "1", "GB": "44", "AU": "61", "DE": "49", "FR": "33",
    "IN": "91", "JP": "81", "CN": "86", "BR": "55", "MX": "52", "ES": "34",
    "IT": "39", "NL": "31", "IE": "353", "NZ": "64",
}

# Test seam: set to a list of raw contact dicts
# ({"name": str, "phones": [str], "emails": [str]}) to bypass the OS call
# entirely. Unit tests set this; production code leaves it unset (None).
_RAW_FOR_TEST = None

# osascript wall-clock budget. Each person costs ~3 Apple Events (name, phones,
# emails); a few-thousand-entry address book can take minutes. The job is
# hourly and fail-closed, so a long read is safe and a hung one still errors.
_OSASCRIPT_TIMEOUT = 600

# The fixed JXA script. NO `//` comments inside it: passed through
# `osascript -e`, a script containing `//` line comments is SIGKILLed on
# macOS 26.6 (verified by bisection; the same text from a file runs fine), so
# the explanation lives here instead.
#   - `Contacts.running()` does not launch the app; `people()` does. The
#     running-state is captured BEFORE the launching call so the hourly
#     launchd job can put things back the way it found them and never leaves
#     Contacts.app open in the user's session.
#   - Everything is wrapped in try/catch so a single odd record (or a quit
#     refusal) can't turn a complete read into a failed one.
_JXA_SCRIPT = """
function run() {
  var Contacts = Application('Contacts');
  var wasRunning = false;
  try { wasRunning = Contacts.running(); } catch (e) {}
  var people = Contacts.people();
  var out = [];
  for (var i = 0; i < people.length; i++) {
    var p = people[i];
    var name;
    try { name = p.name(); } catch (e) { name = ""; }
    var phones = [];
    try {
      var ph = p.phones();
      for (var j = 0; j < ph.length; j++) {
        try { phones.push(ph[j].value()); } catch (e2) {}
      }
    } catch (e) {}
    var emails = [];
    try {
      var em = p.emails();
      for (var k = 0; k < em.length; k++) {
        try { emails.push(em[k].value()); } catch (e2) {}
      }
    } catch (e) {}
    out.push({ name: name, phones: phones, emails: emails });
  }
  var json = JSON.stringify(out);
  if (!wasRunning) { try { Contacts.quit(); } catch (e) {} }
  return json;
}
"""


def _get_system_region():
    """Reads the Mac's system region out of AppleLocale (e.g. "en_US" ->
    "US"; also tolerates a bare "en"). Returns None if it can't be
    determined. A thin, deliberately mockable seam: tests monkeypatch this
    function directly rather than depending on the real machine's locale,
    and production code calls it at most once per import (see
    `_get_calling_code`)."""
    try:
        proc = subprocess.run(
            ["defaults", "read", "-g", "AppleLocale"],
            shell=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    locale = proc.stdout.strip()
    if not locale:
        return None
    tail = locale.split("_", 1)[1] if "_" in locale else locale
    match = re.match(r"[A-Za-z]+", tail)
    return match.group(0).upper() if match else None


def _get_calling_code():
    """Resolves the system region's calling code, or None if the region
    can't be determined or isn't in `_REGION_CALLING_CODES`."""
    region = _get_system_region()
    if region is None:
        return None
    return _REGION_CALLING_CODES.get(region)


def _normalize_phone(raw, calling_code):
    """Returns (normalized_e164_or_None, ambiguous_bool).

    `ambiguous` is True only for the "bare national number, no country
    code resolvable" case — the one place this function would otherwise
    have to guess. Every other invalid input (garbage text, a number that
    already has a country code but still doesn't validate) is just
    silently dropped, same as before.
    """
    if not isinstance(raw, str):
        return None, False
    digits = re.sub(r"[^0-9+]", "", raw)
    if not digits:
        return None, False
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+"):
        # already carries a country code -> normalize, never fabricate one.
        candidate = "+" + digits[1:].replace("+", "")
        if _PHONE_RE.match(candidate):
            return candidate, False
        return None, False

    # Bare national number: no country code in the source data itself.
    bare = digits.replace("+", "")
    if calling_code:
        candidate = "+" + calling_code + bare
        if _PHONE_RE.match(candidate):
            return candidate, False
    # No calling code resolvable (unknown/unmapped region), or the result
    # still doesn't validate -> ambiguous. Drop it, never mint it.
    return None, True


def _normalize_email(raw):
    if not isinstance(raw, str):
        return None
    email = raw.strip().lower()
    if _EMAIL_RE.match(email):
        return email
    return None


# The Contacts read intermittently fails with a non-zero exit after the
# 2-minute Apple-event timeout (observed 2026-08-30 on macOS 26.6, from a
# terminal and under launchd alike, with Contacts.app both closed and open;
# not TCC). One retry after a short pause turns "this hour's import is lost"
# into a rare event without weakening fail-closed: a read that fails twice
# still returns an error and touches nothing.
_RETRY_DELAY = 15
_time_sleep = __import__("time").sleep  # seam: tests stub it out


def _run_osascript_once():
    """One osascript run. Returns (stdout_text, error_or_None)."""
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _JXA_SCRIPT],
            shell=False,
            capture_output=True,
            text=True,
            timeout=_OSASCRIPT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "osascript failed to run: %s" % (type(e).__name__,)

    if proc.returncode != 0:
        return None, "osascript exited %d" % (proc.returncode,)
    return proc.stdout, None


def _run_osascript():
    """Runs the inline JXA script, retrying ONCE on a failed run.
    Returns (stdout_text, error_or_None)."""
    stdout, err = _run_osascript_once()
    if err is None:
        return stdout, None
    _time_sleep(_RETRY_DELAY)
    return _run_osascript_once()


def _load_raw_contacts():
    """Returns the raw `[{name, phones, emails}, ...]` list, from
    `_RAW_FOR_TEST` if set, else from a live osascript run. Raises
    RuntimeError on any read/parse failure or non-list top level."""
    if _RAW_FOR_TEST is not None:
        return _RAW_FOR_TEST

    stdout, err = _run_osascript()
    if err is not None:
        raise RuntimeError(err)
    if stdout is None or not stdout.strip():
        raise RuntimeError("osascript produced empty output")
    try:
        raw_contacts = json.loads(stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise RuntimeError("osascript produced unparseable output")
    if not isinstance(raw_contacts, list):
        raise RuntimeError("osascript output was not a JSON array")
    return raw_contacts


def _normalize_raw_contacts(raw_contacts):
    """Flattens+validates raw contact dicts into
    ([{"display_name","handles"}], dropped_ambiguous_count). Raises
    RuntimeError if an element isn't a dict (a garbled-but-list payload),
    so a caller relying on the {"error": ...} fail-closed contract never
    sees a bare traceback instead."""
    calling_code = _get_calling_code()  # resolved once per run, not per number
    out = []
    dropped_ambiguous = 0
    for raw in raw_contacts:
        if not isinstance(raw, dict):
            raise RuntimeError("osascript output contained a non-dict contact entry")
        name = raw.get("name") or ""
        handles = []
        for phone in raw.get("phones") or []:
            norm, ambiguous = _normalize_phone(phone, calling_code)
            if norm is not None:
                handles.append({"kind": "phone", "value": norm})
            elif ambiguous:
                dropped_ambiguous += 1
        for email in raw.get("emails") or []:
            norm = _normalize_email(email)
            if norm is not None:
                handles.append({"kind": "email", "value": norm})
        if not handles:
            continue
        out.append({"display_name": name, "handles": handles})
    return out, dropped_ambiguous


def read_macos_contacts():
    """Returns [{"display_name": str, "handles": [{"kind","value"}]}, ...].

    Raises RuntimeError if the OS read fails or produces unparseable /
    malformed output; callers (import_once) must catch this and fail
    closed.
    """
    raw_contacts = _load_raw_contacts()
    contacts, _dropped_ambiguous = _normalize_raw_contacts(raw_contacts)
    return contacts


def import_once(db_path):
    """Reads macOS Contacts, upserts into the store, returns counts plus
    "dropped_ambiguous" (bare national numbers dropped for lack of a
    resolvable country code).

    Fail-closed: any read/parse/shape error returns {"error": ...} without
    ever calling upsert_contacts with a partial list.
    """
    sys.path.insert(0, os.path.dirname(__file__))
    import contacts_store as cs

    try:
        raw_contacts = _load_raw_contacts()
        contacts, dropped_ambiguous = _normalize_raw_contacts(raw_contacts)
    except RuntimeError as e:
        return {"error": str(e)}

    seen = []
    for contact in contacts:
        display_name = contact["display_name"]
        for handle in contact["handles"]:
            seen.append({
                "network_id": handle["value"],
                "kind": handle["kind"],
                "display_name": display_name,
            })

    conn = cs.open_store(db_path)
    result = cs.upsert_contacts(conn, "imessage", seen)
    result["dropped_ambiguous"] = dropped_ambiguous
    return result


if __name__ == "__main__":
    default_db = os.path.join(os.path.dirname(__file__), "contacts.db")
    result = import_once(default_db)
    if "error" in result:
        print("import_macos: %s" % result["error"], file=sys.stderr)
        sys.exit(1)
    print("import_macos: added=%d updated=%d soft_deleted=%d dropped_ambiguous=%d" % (
        result["added"], result["updated"], result["soft_deleted"],
        result["dropped_ambiguous"]))
