"""Durable, incremental address-book store for a Matrix bridge hub.

Schema and semantics:
  - `contacts` rows are keyed by (source, network_id) and never hard-deleted.
  - `import_meta['version_seq']` is a single monotonic counter; every real
    row change (add / update / soft-delete / person_id change) consumes the
    next value and stamps it onto that row's `version`.
  - `upsert_contacts` never writes `person_id` — `set_person_id` is the only
    writer of that column, so a re-import can never drop a grouping.
  - An empty `seen` list is treated as "import produced nothing": it changes
    nothing and returns all-zeros. The "contacts missing from a complete
    import get soft-deleted" pass only runs when `seen` is non-empty.
  - All mutations for one `upsert_contacts` call happen inside a single
    transaction, so a crash mid-import rolls back cleanly.
"""

import os
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
  source        TEXT    NOT NULL,
  network_id    TEXT    NOT NULL,
  kind          TEXT    NOT NULL,
  display_name  TEXT,
  person_id     TEXT,
  first_seen_ts INTEGER NOT NULL,
  last_seen_ts  INTEGER NOT NULL,
  deleted       INTEGER NOT NULL DEFAULT 0,
  version       INTEGER NOT NULL,
  PRIMARY KEY (source, network_id)
);
CREATE INDEX IF NOT EXISTS idx_contacts_person ON contacts(person_id);
CREATE TABLE IF NOT EXISTS import_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def open_store(path):
    # isolation_level="IMMEDIATE" makes every `with conn:` transaction begin
    # with a RESERVED write lock immediately, instead of the sqlite3 default
    # (deferred: SHARED first, upgraded to RESERVED only at the first write).
    # contacts.db is written by TWO processes (the hourly importer and the
    # uplink's set_person_id loop); two deferred writers can each hold SHARED
    # and then deadlock trying to upgrade to RESERVED, which the busy_timeout
    # cannot resolve and which surfaces as sqlite3.OperationalError. Taking
    # RESERVED up front serializes the writers under busy_timeout instead.
    conn = sqlite3.connect(path, isolation_level="IMMEDIATE")
    conn.row_factory = sqlite3.Row
    # contacts.db is written by both this store's callers and the uplink
    # process; serialize concurrent writers instead of raising "database
    # is locked".
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    conn.commit()
    os.chmod(path, 0o600)
    # SQLite's transient -journal/-wal/-shm siblings are created next to the
    # db with the process umask (often 0644) and briefly hold contact rows.
    # chmod the PARENT DIRECTORY to 0700 so those siblings are unreadable by
    # other users regardless of their own mode; the db file itself stays 0600.
    os.chmod(os.path.dirname(os.path.abspath(path)), 0o700)
    return conn


def _next_version(conn):
    row = conn.execute(
        "SELECT value FROM import_meta WHERE key = 'version_seq'"
    ).fetchone()
    current = int(row["value"]) if row else 0
    nxt = current + 1
    conn.execute(
        "INSERT INTO import_meta (key, value) VALUES ('version_seq', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(nxt),),
    )
    return nxt


def upsert_contacts(conn, source, seen):
    result = {"added": 0, "updated": 0, "soft_deleted": 0}
    if not seen:
        return result

    now = int(time.time())
    seen_ids = set()

    with conn:
        for item in seen:
            network_id = item["network_id"]
            kind = item["kind"]
            display_name = item.get("display_name")
            seen_ids.add(network_id)

            existing = conn.execute(
                "SELECT kind, display_name, deleted FROM contacts "
                "WHERE source = ? AND network_id = ?",
                (source, network_id),
            ).fetchone()

            if existing is None:
                version = _next_version(conn)
                conn.execute(
                    "INSERT INTO contacts "
                    "(source, network_id, kind, display_name, person_id, "
                    " first_seen_ts, last_seen_ts, deleted, version) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?, 0, ?)",
                    (source, network_id, kind, display_name, now, now, version),
                )
                result["added"] += 1
                continue

            changed = (
                existing["kind"] != kind
                or existing["display_name"] != display_name
                or existing["deleted"] != 0
            )
            if changed:
                version = _next_version(conn)
                conn.execute(
                    "UPDATE contacts SET kind = ?, display_name = ?, "
                    "last_seen_ts = ?, deleted = 0, version = ? "
                    "WHERE source = ? AND network_id = ?",
                    (kind, display_name, now, version, source, network_id),
                )
                result["updated"] += 1
            else:
                conn.execute(
                    "UPDATE contacts SET last_seen_ts = ? "
                    "WHERE source = ? AND network_id = ?",
                    (now, source, network_id),
                )

        # Complete-import soft-delete pass: only runs because seen is
        # non-empty (guaranteed by the early return above).
        rows_present = conn.execute(
            "SELECT network_id FROM contacts WHERE source = ? AND deleted = 0",
            (source,),
        ).fetchall()
        for row in rows_present:
            network_id = row["network_id"]
            if network_id not in seen_ids:
                version = _next_version(conn)
                conn.execute(
                    "UPDATE contacts SET deleted = 1, version = ? "
                    "WHERE source = ? AND network_id = ?",
                    (version, source, network_id),
                )
                result["soft_deleted"] += 1

    return result


def shared_since(conn, source, after_version):
    rows = conn.execute(
        "SELECT source, network_id, kind, display_name, person_id, deleted, version "
        "FROM contacts WHERE source = ? AND version > ? ORDER BY version",
        (source, after_version),
    ).fetchall()
    return [dict(row) for row in rows]


def set_person_id(conn, source, network_id, person_id):
    with conn:
        existing = conn.execute(
            "SELECT person_id FROM contacts WHERE source = ? AND network_id = ?",
            (source, network_id),
        ).fetchone()
        if existing is None:
            return False
        if existing["person_id"] == person_id:
            return False
        version = _next_version(conn)
        conn.execute(
            "UPDATE contacts SET person_id = ?, version = ? "
            "WHERE source = ? AND network_id = ?",
            (person_id, version, source, network_id),
        )
    return True
