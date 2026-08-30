# tests/unit/contacts_store.test.py
import os, tempfile, sqlite3, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "contacts"))
import contacts_store as cs

def _fresh():
    d = tempfile.mkdtemp()
    return cs.open_store(os.path.join(d, "contacts.db"))

def test_add_then_incremental_then_soft_delete_and_never_wipe():
    conn = _fresh()
    # first import: two contacts
    r = cs.upsert_contacts(conn, "imessage", [
        {"network_id": "+15551234567", "kind": "phone", "display_name": "Alice"},
        {"network_id": "bob@example.com", "kind": "email", "display_name": "Bob"},
    ])
    assert r == {"added": 2, "updated": 0, "soft_deleted": 0}
    v_after_first = max(row["version"] for row in cs.shared_since(conn, "imessage", 0))

    # re-import identical set: no version churn, nothing "shared" as new
    r = cs.upsert_contacts(conn, "imessage", [
        {"network_id": "+15551234567", "kind": "phone", "display_name": "Alice"},
        {"network_id": "bob@example.com", "kind": "email", "display_name": "Bob"},
    ])
    assert r == {"added": 0, "updated": 0, "soft_deleted": 0}
    assert cs.shared_since(conn, "imessage", v_after_first) == []

    # rename Alice: one update, one new version > watermark
    r = cs.upsert_contacts(conn, "imessage", [
        {"network_id": "+15551234567", "kind": "phone", "display_name": "Alice Smith"},
        {"network_id": "bob@example.com", "kind": "email", "display_name": "Bob"},
    ])
    assert r == {"added": 0, "updated": 1, "soft_deleted": 0}
    changed = cs.shared_since(conn, "imessage", v_after_first)
    assert [c["network_id"] for c in changed] == ["+15551234567"]

    # EMPTY import must change nothing (a failed/partial fetch must never wipe)
    v_before_empty = max(row["version"] for row in cs.shared_since(conn, "imessage", 0))
    r = cs.upsert_contacts(conn, "imessage", [])
    assert r == {"added": 0, "updated": 0, "soft_deleted": 0}
    assert max(row["version"] for row in cs.shared_since(conn, "imessage", 0)) == v_before_empty

    # Bob genuinely gone from a COMPLETE import -> soft-deleted, not removed
    r = cs.upsert_contacts(conn, "imessage", [
        {"network_id": "+15551234567", "kind": "phone", "display_name": "Alice Smith"},
    ])
    assert r == {"added": 0, "updated": 0, "soft_deleted": 1}
    bob = [c for c in cs.shared_since(conn, "imessage", 0) if c["network_id"] == "bob@example.com"][0]
    assert bob["deleted"] == 1

def test_store_file_is_0600():
    conn = _fresh()
    path = conn.execute("PRAGMA database_list").fetchall()[0][2]
    assert (os.stat(path).st_mode & 0o777) == 0o600

def test_reimport_preserves_person_id_grouping():
    conn = _fresh()
    cs.upsert_contacts(conn, "imessage", [
        {"network_id": "+15551234567", "kind": "phone", "display_name": "Alice"}])
    # the uplink links this handle to a person (from account-data)
    assert cs.set_person_id(conn, "imessage", "+15551234567", "cp_alice") is True
    v_after_link = max(r["version"] for r in cs.shared_since(conn, "imessage", 0))
    # a later macOS re-import (even with a renamed display) must NOT drop the link
    cs.upsert_contacts(conn, "imessage", [
        {"network_id": "+15551234567", "kind": "phone", "display_name": "Alice Smith"}])
    row = cs.shared_since(conn, "imessage", 0)[0]
    assert row["person_id"] == "cp_alice", "re-import must preserve the grouping"
    # setting the same person_id again is a no-op (no version churn)
    assert cs.set_person_id(conn, "imessage", "+15551234567", "cp_alice") is False

if __name__ == "__main__":
    test_add_then_incremental_then_soft_delete_and_never_wipe()
    test_store_file_is_0600()
    test_reimport_preserves_person_id_grouping()
    print("ok")
