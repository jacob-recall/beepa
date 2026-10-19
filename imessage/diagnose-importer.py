#!/usr/bin/env python3
"""Read-only importer counters for rollout handoff; no IDs, text, paths or secrets."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import time


PENDING = ('pending', 'source_unavailable', 'attachment_unavailable', 'delivery_failed', 'reconciliation_failed')
REFUSED = ('refused_invalid_path', 'refused_path')
OUTBOUND = ('processing', 'dispatching', 'confirmed', 'refused', 'ambiguous', 'retryable', 'ignored')


def groups(db, table, column, allowed):
    # Status strings are private data too; emit only known enum values.
    result = {}
    for value, count in db.execute('SELECT '+column+',count(*) FROM '+table+' GROUP BY '+column):
        key = value if value in allowed else 'other'
        result[key] = result.get(key, 0) + count
    return result


def snapshot(state_dir, now=None):
    path = Path(state_dir).resolve() / 'state.db'
    if not path.is_file():
        raise ValueError('Existing iMessage state database not found; nothing was created')
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=5)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        pending = groups(db, 'inbound_pending', 'status', PENDING)
        refused = {k: v for k, v in groups(db, 'inbound_component', 'status', REFUSED).items() if k in REFUSED}
        result = {'inbound_by_reason': pending, 'inbound_total': sum(pending.values()),
                  'terminal_attachment_refusals': refused,
                  'outbound_by_state': groups(db, 'outbound_event', 'state', OUTBOUND),
                  'affected_chats': db.execute('SELECT count(DISTINCT chat_id) FROM inbound_pending').fetchone()[0],
                  'pending_without_portal': db.execute('SELECT count(*) FROM inbound_pending p LEFT JOIN map m USING(chat_id) WHERE m.room_id IS NULL').fetchone()[0]}
        migrated = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbound_retry'").fetchone())
        result['retry_scheduler_installed'] = migrated
        if migrated:
            now = time.time() if now is None else now
            due, deferred = db.execute('SELECT coalesce(sum(coalesce(r.next_attempt,0)<=?),0),'
                                      'coalesce(sum(r.next_attempt>?),0) FROM inbound_pending p '
                                      'LEFT JOIN inbound_retry r USING(chat_id,msg_id)', (now, now)).fetchone()
            result.update(retry_due=due, retry_deferred=deferred)
        return result
    finally:
        db.close()


def resolve_state(root, state_dir=None):
    if state_dir:
        return Path(state_dir).expanduser().resolve()
    root = Path(root).expanduser().resolve()
    manifest = root / '.beepa-install.json'
    if manifest.exists():
        data = json.loads(manifest.read_text())
        state = Path(data['state_root']) if data.get('state_initialized') else root
        if not state.is_absolute():
            raise ValueError('Installation state root must be absolute')
        return state / 'imessage'
    return root / 'imessage'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=os.environ.get('BEEPA_INSTALL_ROOT', str(Path(__file__).resolve().parents[1])))
    parser.add_argument('--state-dir', default=os.environ.get('IMESSAGE_STATE_DIR'))
    args = parser.parse_args()
    try:
        print(json.dumps(snapshot(resolve_state(args.root, args.state_dir)), indent=2, sort_keys=True))
    except (OSError, ValueError, KeyError, sqlite3.Error):
        parser.exit(1, 'Cannot read existing importer state. Verify --root/--state-dir in the owner session; no state was modified.\n')


if __name__ == '__main__':
    main()
