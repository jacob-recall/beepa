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
    assert result == {"added": 1, "updated": 0, "soft_deleted": 0}, result

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

    orig = im.read_macos_contacts
    im.read_macos_contacts = _boom
    try:
        result = im.import_once(db_path)
    finally:
        im.read_macos_contacts = orig

    assert "error" in result
    # the store must not have been created/mutated by a failed read
    assert not os.path.exists(db_path)


if __name__ == "__main__":
    test_normalize_drops_invalid_and_keeps_e164_and_email()
    test_import_once_writes_to_store()
    test_import_once_fails_closed_on_read_error()
    print("ok import_macos")
