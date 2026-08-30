"""macOS Contacts importer: reads the local Contacts.app address book via
`osascript -l JavaScript` and upserts it into the shared contacts store.

Durability contract: a failed or garbled OS read must never mutate the
store. `import_once` reads everything first, validates the whole batch, and
only calls `contacts_store.upsert_contacts` once it has a complete `seen`
list; any error along the way returns `{"error": ...}` and touches nothing.

Normalization:
  - phones -> E.164 (`+` + digits only), validated against
    `^\\+[1-9]\\d{6,14}$`; anything that doesn't normalize cleanly is
    dropped.
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

# Test seam: set to a list of raw contact dicts
# ({"name": str, "phones": [str], "emails": [str]}) to bypass the OS call
# entirely. Unit tests set this; production code leaves it unset (None).
_RAW_FOR_TEST = None

_JXA_SCRIPT = """
ObjC.import('stdlib');
function run() {
  var Contacts = Application('Contacts');
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
  return JSON.stringify(out);
}
"""


def _normalize_phone(raw):
    if not isinstance(raw, str):
        return None
    digits = re.sub(r"[^0-9+]", "", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        digits = "+" + digits.lstrip("+")
    # collapse any stray internal '+' left by odd input
    digits = "+" + digits[1:].replace("+", "")
    if _PHONE_RE.match(digits):
        return digits
    return None


def _normalize_email(raw):
    if not isinstance(raw, str):
        return None
    email = raw.strip().lower()
    if _EMAIL_RE.match(email):
        return email
    return None


def _run_osascript():
    """Runs the inline JXA script. Returns (stdout_text, error_or_None)."""
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _JXA_SCRIPT],
            shell=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "osascript failed to run: %s" % (type(e).__name__,)

    if proc.returncode != 0:
        return None, "osascript exited %d" % (proc.returncode,)
    return proc.stdout, None


def read_macos_contacts():
    """Returns [{"display_name": str, "handles": [{"kind","value"}]}, ...].

    Raises RuntimeError if the OS read fails or produces unparseable
    output; callers (import_once) must catch this and fail closed.
    """
    if _RAW_FOR_TEST is not None:
        raw_contacts = _RAW_FOR_TEST
    else:
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

    out = []
    for raw in raw_contacts:
        name = raw.get("name") or ""
        handles = []
        for phone in raw.get("phones") or []:
            norm = _normalize_phone(phone)
            if norm is not None:
                handles.append({"kind": "phone", "value": norm})
        for email in raw.get("emails") or []:
            norm = _normalize_email(email)
            if norm is not None:
                handles.append({"kind": "email", "value": norm})
        if not handles:
            continue
        out.append({"display_name": name, "handles": handles})
    return out


def import_once(db_path):
    """Reads macOS Contacts, upserts into the store, returns counts.

    Fail-closed: any read/parse error returns {"error": ...} without ever
    calling upsert_contacts with a partial list.
    """
    sys.path.insert(0, os.path.dirname(__file__))
    import contacts_store as cs

    try:
        contacts = read_macos_contacts()
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
    return cs.upsert_contacts(conn, "imessage", seen)


if __name__ == "__main__":
    default_db = os.path.join(os.path.dirname(__file__), "contacts.db")
    result = import_once(default_db)
    if "error" in result:
        print("import_macos: %s" % result["error"], file=sys.stderr)
        sys.exit(1)
    print("import_macos: added=%d updated=%d soft_deleted=%d" % (
        result["added"], result["updated"], result["soft_deleted"]))
