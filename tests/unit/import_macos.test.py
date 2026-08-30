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


if __name__ == "__main__":
    test_normalize_drops_invalid_and_keeps_e164_and_email()
    test_import_once_writes_to_store()
    test_import_once_fails_closed_on_read_error()
    test_bare_national_number_uses_system_region_calling_code()
    test_phone_with_existing_country_code_is_unchanged_regardless_of_region()
    test_00_international_prefix_converts_to_plus()
    test_bare_number_with_unmapped_region_is_dropped_and_counted()
    test_import_once_fails_closed_on_non_dict_contact_entries()
    print("ok import_macos")
