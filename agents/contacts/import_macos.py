"""macOS Contacts importer: reads the local Contacts.app address book via
`osascript -l JavaScript` and upserts it into the shared contacts store.

Durability contract: a failed or garbled OS read must never mutate the
store. `import_once` reads everything first, validates the whole batch, and
only calls `contacts_store.upsert_contacts` once it has a complete `seen`
list; any error along the way returns `{"error": ...}` and touches nothing.

Normalization uses the pinned libphonenumber core metadata in phone_numbers.py.
International numbers preserve their calling code; national numbers require the
configured PHONE_REGION or Mac region. Trunk prefixes are parsed by that region's
metadata. Extensions/post-dial targets are refused for automatic matching, not
collapsed into a potentially different person's base number. Missing metadata
fails the entire import before any store mutation.

No handle values or display names are ever logged.
"""

import json
import os
import re
import subprocess
import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from phone_numbers import normalize_phone, metadata

_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")

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
    """Compatibility helper; region metadata now owns calling-code selection."""
    region = os.environ.get('PHONE_REGION') or _get_system_region()
    code = metadata().country_code_for_region(region or '')
    return str(code) if code else None


def _normalize_phone(raw, region):
    value = normalize_phone(raw, region)
    ambiguous = value is None and isinstance(raw, str) and any(c.isdigit() for c in raw)
    return value, ambiguous


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
    metadata()  # fail closed before an unavailable library could erase old rows
    region = os.environ.get('PHONE_REGION') or _get_system_region()
    out = []
    dropped_ambiguous = 0
    for raw in raw_contacts:
        if not isinstance(raw, dict):
            raise RuntimeError("osascript output contained a non-dict contact entry")
        name = raw.get("name") or ""
        handles = []
        for phone in raw.get("phones") or []:
            norm, ambiguous = _normalize_phone(phone, region)
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
        metadata()
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
    default_db = os.environ.get('CONTACTS_DB', os.path.join(os.path.dirname(__file__), "contacts.db"))
    result = import_once(default_db)
    if "error" in result:
        print("import_macos: %s" % result["error"], file=sys.stderr)
        sys.exit(1)
    print("import_macos: added=%d updated=%d soft_deleted=%d dropped_ambiguous=%d" % (
        result["added"], result["updated"], result["soft_deleted"],
        result["dropped_ambiguous"]))
