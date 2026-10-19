"""Private control/notification backup material and conservative restore repair.

Call only while both control and enrollment writers are stopped. Never run a
restore against live accounts as a test. Canonical lists remain in Synapse.
"""
import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3

FEATURE_FILES = {
    'control.local': ('control.db', 'config.json', 'enabled'),
    'notifications.local': ('notifications.db', 'vapid.pem'),
}


def private_path(master, relative):
    master = Path(master).resolve()
    path = master / relative
    if master not in path.resolve().parents:
        raise ValueError('Control state path escapes master state')
    if any(part.is_symlink() for part in (path, *path.parents) if part != master and master in part.parents):
        raise ValueError('Control state must not use symbolic links')
    return path


def snapshot_features(master, staging):
    """Return normalized archive files; SQLite backup includes committed WAL.

    Logs, live WAL/SHM files, and unrelated directory contents are not archived.
    """
    result = []
    for directory, files in FEATURE_FILES.items():
        for name in files:
            relative = Path(directory)/name
            source = private_path(master, relative)
            if not source.exists():
                continue
            if not source.is_file():
                raise ValueError('Control backup source must be a regular file')
            target = Path(staging)/relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if name.endswith('.db'):
                # Open the existing DB without CREATE. SQLite may need to rebuild
                # WAL shared-memory after the stopped writer removed its SHM file.
                with closing(sqlite3.connect(source.as_uri()+'?mode=rw', uri=True)) as src, closing(sqlite3.connect(target)) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('Control database backup failed integrity check')
            else:
                shutil.copyfile(source, target)
            target.chmod(0o600)
            result.append((target, 'master/'+relative.as_posix()))
    return result


def feature_inventory(master):
    return {directory: private_path(master, Path(directory)/files[0]).is_file()
            for directory, files in FEATURE_FILES.items()}


def restore_features(master, staging, retained):
    """Prefer current operational evidence over a potentially older snapshot.

    Persist `retained` in the lifecycle journal BEFORE copying anything. A retry
    must not mistake a partly restored database for newer, current evidence.
    """
    for directory, files in FEATURE_FILES.items():
        folder = private_path(master, directory)
        if folder.exists():
            folder.chmod(0o700)
        if retained.get(directory):
            continue
        # Stale sidecars must never attach to the normalized snapshot database.
        db_path = private_path(master, Path(directory)/files[0])
        for suffix in ('-wal', '-shm'):
            private_path(master, str(Path(directory)/files[0])+suffix).unlink(missing_ok=True)
        for name in files:
            relative = Path(directory)/name
            source, target = Path(staging)/'master'/relative, private_path(master, relative)
            if not source.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.parent.chmod(0o700)
            shutil.copyfile(source, target)
            target.chmod(0o600)
        if db_path.exists():
            quarantine_database(db_path, directory, current=False)
    for directory, files in FEATURE_FILES.items():
        if retained.get(directory):
            quarantine_database(private_path(master, Path(directory)/files[0]), directory, current=True)


def quarantine_database(path, kind, current):
    """Keep evidence and review payloads; never replay restored pending work."""
    if not Path(path).is_file():
        return
    with closing(sqlite3.connect(path)) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Restored control database failed integrity check')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        def update(table, sql):
            if table in tables:
                db.execute(sql)
        if kind == 'control.local':
            update('drafts', "UPDATE drafts SET state=CASE WHEN state='pending' THEN 'review_required' ELSE 'unknown' END WHERE state IN ('pending','executing')")
            update('outbox', "UPDATE outbox SET state=CASE WHEN state='pending' THEN 'canceled' ELSE 'unknown' END WHERE state IN ('pending','sending')")
            update('inbox', "UPDATE inbox SET state='interrupted' WHERE state IN ('pending','processing')")
            update('api_commands', "UPDATE api_commands SET state=CASE WHEN state='queued' THEN 'canceled' ELSE 'unknown' END,text='',error='Master restored; review current context before submitting new work.' WHERE state IN ('queued','processing')")
            update('cancellations', "UPDATE cancellations SET state='cancellation_unconfirmed' WHERE state='canceling'")
            update('notification_route_rules', "UPDATE notification_route_rules SET state='canceled' WHERE state='active'")
            update('notification_route_attempts', "UPDATE notification_route_attempts SET state=CASE WHEN state='enqueueing' OR state='in_flight' THEN 'unknown' ELSE 'canceled' END WHERE state IN ('enqueueing','in_flight','pending')")
            # A restore can reintroduce historical mirror events. Start new
            # observation windows; retain existing event/attempt tombstones.
            update('cursors', 'DELETE FROM cursors')
            update('notification_import_cursors', 'DELETE FROM notification_import_cursors')
            if 'kv' in tables:
                db.execute("DELETE FROM kv WHERE k='pair'")
                if not current:
                    db.execute("DELETE FROM kv WHERE k='operator'")
                for key, raw in db.execute("SELECT k,v FROM kv WHERE k LIKE 'api:%'").fetchall():
                    value = json.loads(raw)
                    if value.get('state') in ('processing', 'queued'):
                        value['state'] = 'unknown' if value['state'] == 'processing' else 'canceled'
                        db.execute('UPDATE kv SET v=? WHERE k=?', (json.dumps(value), key))
        elif kind == 'notifications.local':
            update('deliveries', "UPDATE deliveries SET state=CASE WHEN state='sending' THEN 'unknown' ELSE 'canceled' END WHERE state IN ('sending','pending')")
            if not current:
                update('devices', 'UPDATE devices SET enabled=0')
            # Preserve handoff identity so the controller cannot import its old
            # frozen cursors again after restoring one database independently.
            if 'control_metadata' in tables:
                row = db.execute("SELECT value FROM control_metadata WHERE key='dispatcher_handoff'").fetchone()
                if row:
                    handoff = json.loads(row[0]);handoff['follows'] = []
                    db.execute("UPDATE control_metadata SET value=? WHERE key='dispatcher_handoff'", (json.dumps(handoff),))
            update('follows', 'DELETE FROM follows')
        else:
            raise ValueError('Unknown control recovery state')
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    os.chmod(path, 0o600)


def refresh_manager_credentials(master, tokens):
    path = private_path(master, 'control.local/config.json')
    if not path.exists():
        return
    config = json.loads(path.read_text())
    config.update(manager=tokens['MASTER_MANAGER_USER'], manager_token=tokens['MASTER_MANAGER_TOKEN'],
                  roster={tokens['MASTER_'+name.upper()+'_USER']: tokens['MASTER_SPACE_'+name.upper()]
                          for name in tokens.get('MASTER_TEAMMATES', '').split()})
    # The worker is stopped. Atomic replace prevents a retry observing a partial
    # credential file; Telegram/model credentials are retained, never printed.
    temporary = private_path(master, 'control.local/config.json.restore-tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600);json.dump(config, stream)
    os.replace(temporary, path)
