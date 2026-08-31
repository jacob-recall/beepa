# tests/unit/import_macos.test.py
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "contacts"))
import import_macos as im


def test_normalize_drops_invalid_and_keeps_e164_and_email():
    raw = [
        {"name": "Alice", "phones": ["+1 (555) 123-4567", "notaphone"], "emails": ["Alice@Example.COM"]},
        {"name": "NoHandles", "phones": [], "emails": []},
    ]
    im._RAW_FOR_TEST = raw            # test seam: read_macos_contacts uses this if set
    out = im.read_macos_contacts()
    alice = [c for c in out if c["display_name"] == "Alice"][0]
    kinds = {(h["kind"], h["value"]) for h in alice["handles"]}
    assert ("phone", "+15551234567") in kinds
    assert ("email", "alice@example.com") in kinds
    assert all(h["value"] != "notaphone" for h in alice["handles"])
    assert all(c["display_name"] != "NoHandles" for c in out)  # no usable handle -> dropped
    im._RAW_FOR_TEST = None


def test_import_once_writes_to_store():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, "contacts.db")
    im._RAW_FOR_TEST = [
        {"name": "Alice", "phones": ["+1 555 123 4567"], "emails": []},
    ]
    result = im.import_once(db_path)
    im._RAW_FOR_TEST = None
    assert result == {"added": 1, "updated": 0, "soft_deleted": 0, "dropped_ambiguous": 0}, result

    import contacts_store as cs
    conn = cs.open_store(db_path)
    rows = cs.shared_since(conn, "imessage", 0)
    assert len(rows) == 1
    assert rows[0]["network_id"] == "+15551234567"


def test_import_once_fails_closed_on_read_error():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, "contacts.db")

    def _boom():
        raise RuntimeError("osascript exited 1")

    orig = im._load_raw_contacts
    im._load_raw_contacts = _boom
    try:
        result = im.import_once(db_path)
    finally:
        im._load_raw_contacts = orig

    assert "error" in result
    # the store must not have been created/mutated by a failed read
    assert not os.path.exists(db_path)


# --- fix round 1: I1 (country-code inference for bare national numbers) ---

def test_bare_national_number_uses_system_region_calling_code():
    im._RAW_FOR_TEST = [
        {"name": "Carol", "phones": ["(555) 987-6543"], "emails": []},
    ]
    orig = im._get_system_region
    im._get_system_region = lambda: "US"
    try:
        out = im.read_macos_contacts()
    finally:
        im._get_system_region = orig
        im._RAW_FOR_TEST = None
    carol = [c for c in out if c["display_name"] == "Carol"][0]
    values = {h["value"] for h in carol["handles"]}
    assert "+15559876543" in values, values


def test_phone_with_existing_country_code_is_unchanged_regardless_of_region():
    im._RAW_FOR_TEST = [
        {"name": "Dave", "phones": ["+1 555 123 4567"], "emails": []},
    ]
    orig = im._get_system_region
    im._get_system_region = lambda: "GB"  # deliberately different; must not matter
    try:
        out = im.read_macos_contacts()
    finally:
        im._get_system_region = orig
        im._RAW_FOR_TEST = None
    dave = [c for c in out if c["display_name"] == "Dave"][0]
    values = {h["value"] for h in dave["handles"]}
    assert "+15551234567" in values, values


def test_00_international_prefix_converts_to_plus():
    im._RAW_FOR_TEST = [
        {"name": "Eve", "phones": ["00441617496213"], "emails": []},
    ]
    out = im.read_macos_contacts()
    im._RAW_FOR_TEST = None
    eve = [c for c in out if c["display_name"] == "Eve"][0]
    values = {h["value"] for h in eve["handles"]}
    assert "+441617496213" in values, values


def test_bare_number_with_unmapped_region_is_dropped_and_counted():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, "contacts.db")
    im._RAW_FOR_TEST = [
        {"name": "Frank", "phones": ["5559876543"], "emails": []},
    ]
    orig = im._get_system_region
    im._get_system_region = lambda: "ZZ"  # unmapped region
    try:
        result = im.import_once(db_path)
    finally:
        im._get_system_region = orig
        im._RAW_FOR_TEST = None

    assert result.get("dropped_ambiguous") == 1, result
    assert result["added"] == 0, result  # Frank had no other usable handle -> dropped entirely

    import contacts_store as cs
    conn = cs.open_store(db_path)
    rows = cs.shared_since(conn, "imessage", 0)
    assert rows == [], rows


# --- fix round 1: I2 (garbled non-dict contact entries fail closed) ---

def test_import_once_fails_closed_on_non_dict_contact_entries():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, "contacts.db")
    im._RAW_FOR_TEST = ["not-a-dict", 123, None]
    try:
        result = im.import_once(db_path)
    finally:
        im._RAW_FOR_TEST = None

    assert "error" in result, result
    # a garbled read must never mutate/create the store
    assert not os.path.exists(db_path)


# --- real subprocess path: a fake `osascript` first on PATH (no Contacts.app) ---
# _RAW_FOR_TEST is left None so _load_raw_contacts really shells out; the fake
# stands in for the OS so the subprocess/JSON/fail-closed path is exercised
# without touching (or needing) a real address book.

def _with_fake_osascript(script_body, fn):
    """Run fn() with a fake executable `osascript` first on PATH."""
    import stat
    d = tempfile.mkdtemp()
    fake = os.path.join(d, "osascript")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\n" + script_body + "\n")
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
    orig_path = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + orig_path
    assert im._RAW_FOR_TEST is None
    try:
        return fn()
    finally:
        os.environ["PATH"] = orig_path


def test_subprocess_path_reads_fake_osascript_output():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, "contacts.db")
    payload = '[{"name":"Gina","phones":["+1 555 000 1111"],"emails":["G@Example.com"]}]'
    result = _with_fake_osascript("printf '%s' '" + payload + "'",
                                  lambda: im.import_once(db_path))
    assert result == {"added": 2, "updated": 0, "soft_deleted": 0, "dropped_ambiguous": 0}, result
    import contacts_store as cs
    conn = cs.open_store(db_path)
    ids = {r["network_id"] for r in cs.shared_since(conn, "imessage", 0)}
    assert ids == {"+15550001111", "g@example.com"}, ids


def test_subprocess_path_nonzero_exit_fails_closed():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, "contacts.db")
    sleeps = []
    orig_sleep = im._time_sleep
    im._time_sleep = sleeps.append
    try:
        result = _with_fake_osascript("echo 'boom' >&2; exit 1",
                                      lambda: im.import_once(db_path))
    finally:
        im._time_sleep = orig_sleep
    assert result == {"error": "osascript exited 1"}, result
    assert not os.path.exists(db_path)
    assert sleeps == [im._RETRY_DELAY], sleeps  # exactly one retry, then give up


def test_subprocess_path_retries_once_after_a_transient_failure():
    # fake osascript: first call exits 1 (the intermittent Apple-event
    # timeout), second call prints the payload. Counter kept in a temp file.
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, "contacts.db")
    marker = os.path.join(d, "first_call_done")
    payload = '[{"name":"Hal","phones":["+1 555 000 2222"],"emails":[]}]'
    body = ("if [ ! -f '%s' ]; then touch '%s'; exit 1; fi\nprintf '%%s' '%s'"
            % (marker, marker, payload))
    sleeps = []
    orig_sleep = im._time_sleep
    im._time_sleep = sleeps.append
    try:
        result = _with_fake_osascript(body, lambda: im.import_once(db_path))
    finally:
        im._time_sleep = orig_sleep
    assert result == {"added": 1, "updated": 0, "soft_deleted": 0, "dropped_ambiguous": 0}, result
    assert sleeps == [im._RETRY_DELAY], sleeps


def test_subprocess_path_unparseable_output_fails_closed():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, "contacts.db")
    result = _with_fake_osascript("echo 'not json'",
                                  lambda: im.import_once(db_path))
    assert result == {"error": "osascript produced unparseable output"}, result
    assert not os.path.exists(db_path)


def test_osascript_timeout_allows_large_address_books():
    # ~3 Apple Events per person; a few-thousand-entry book exceeds a minute.
    assert im._OSASCRIPT_TIMEOUT >= 600, im._OSASCRIPT_TIMEOUT


def test_jxa_script_quits_contacts_only_if_it_launched_it():
    # The fixed script must record running-state BEFORE any launching call and
    # only quit when it was not running before (hourly launchd job must not
    # leave Contacts.app open in the user's session).
    s = im._JXA_SCRIPT
    assert s.index("Contacts.running()") < s.index("Contacts.people()"), "running() must precede people()"
    assert "if (!wasRunning)" in s and "Contacts.quit()" in s, s
    # `osascript -e` SIGKILLs a script containing `//` line comments on
    # macOS 26.6 (bisected 2026-08-30). Keep the constant comment-free.
    assert "//" not in s, "no // comments inside the JXA constant"


if __name__ == "__main__":
    test_subprocess_path_reads_fake_osascript_output()
    test_subprocess_path_nonzero_exit_fails_closed()
    test_subprocess_path_retries_once_after_a_transient_failure()
    test_subprocess_path_unparseable_output_fails_closed()
    test_osascript_timeout_allows_large_address_books()
    test_jxa_script_quits_contacts_only_if_it_launched_it()
    test_normalize_drops_invalid_and_keeps_e164_and_email()
    test_import_once_writes_to_store()
    test_import_once_fails_closed_on_read_error()
    test_bare_national_number_uses_system_region_calling_code()
    test_phone_with_existing_country_code_is_unchanged_regardless_of_region()
    test_00_international_prefix_converts_to_plus()
    test_bare_number_with_unmapped_region_is_dropped_and_counted()
    test_import_once_fails_closed_on_non_dict_contact_entries()
    print("ok import_macos")
