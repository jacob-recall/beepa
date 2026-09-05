#!/usr/bin/env python3
"""Preserve operator configuration with a conservative three-way text merge.

No YAML reserialization: bridge-specific tags, comments and formatting survive.
An ambiguous edit blocks the entire activation and writes review candidates.
Persistent whole-file overrides may be supplied under .beepa-config/overrides/.
"""
import argparse
import difflib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from install_config import atomic_write


class ConfigConflict(ValueError):
    pass


def merge(base, local, incoming):
    if local == base or local == incoming:
        return incoming
    if incoming == base:
        return local
    old = base.splitlines(keepends=True)

    def edits(text):
        lines = text.splitlines(keepends=True)
        return [(i, j, lines[a:b]) for op, i, j, a, b in
                difflib.SequenceMatcher(a=old, b=lines, autojunk=False).get_opcodes() if op != 'equal']

    left, right = edits(local), edits(incoming)
    combined = list(left)
    for change in right:
        if change in combined:
            continue
        start, end, _ = change
        for a, b, _ in left:
            overlap = max(start, a) < min(end, b)
            insertion = (start == end and a <= start <= b) or (a == b and start <= a <= end)
            if overlap or insertion:
                raise ConfigConflict('Operator and upstream changed the same configuration region')
        combined.append(change)
    for start, end, replacement in sorted(combined, key=lambda x: (x[0], x[1]), reverse=True):
        old[start:end] = replacement
    return ''.join(old)


def activate(root, stage):
    root, stage = Path(root).resolve(), Path(stage).resolve()
    meta = root / '.beepa-config'
    baseline = meta / 'defaults'
    overrides = meta / 'overrides'
    plans, conflicts, adopted = [], [], []
    for source in sorted(stage.rglob('*')):
        if not source.is_file():
            continue
        relative = source.relative_to(stage)
        dest, prior, override = root / relative, baseline / relative, overrides / relative
        if dest.is_symlink() or prior.is_symlink() or override.is_symlink():
            raise ConfigConflict('Refusing symlinked configuration: ' + str(relative))
        incoming = source.read_text()
        local = override.read_text() if override.exists() else dest.read_text() if dest.exists() else incoming
        if not prior.exists():
            # We cannot reconstruct a legacy template version reliably. Preserve
            # its effective file, snapshot this release's defaults, and report.
            merged = local
            if local != incoming:
                adopted.append(str(relative))
        else:
            try:
                merged = merge(prior.read_text(), local, incoming)
            except ConfigConflict:
                conflicts.append(str(relative))
                atomic_write(meta / 'conflicts' / (str(relative) + '.incoming'), incoming)
                continue
        plans.append((dest, prior, override, incoming, merged))
    if conflicts:
        raise ConfigConflict('Configuration conflicts: %s. Review .beepa-config/conflicts; effective files unchanged.' % ', '.join(conflicts))
    # Persist a recoverable activation journal before any target changes. Each
    # individual replacement is atomic; rerun finishes interrupted activation.
    pending = meta / 'activation.json'
    if pending.exists():
        recover(root)
        return activate(root, stage)
    journal = [{'dest': str(d.relative_to(root)), 'default': new, 'effective': effective,
                'override': o.exists()} for d, _, o, new, effective in plans]
    atomic_write(pending, json.dumps(journal))
    changed = [str(d.relative_to(root)) for d, _, _, _, effective in plans if not d.exists() or d.read_text() != effective]
    recover(root)
    atomic_write(meta / 'last-render.json', json.dumps({'changed': changed, 'adopted_legacy_overrides': adopted}, indent=2) + '\n')
    for relative in adopted:
        print('render: preserved legacy operator configuration: ' + relative, file=sys.stderr)
    return changed


def recover(root):
    root = Path(root).resolve()
    meta = root / '.beepa-config'
    pending = meta / 'activation.json'
    if not pending.exists():
        return
    for item in json.loads(pending.read_text()):
        relative = Path(item['dest'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ConfigConflict('Invalid configuration activation journal path')
        atomic_write(root / relative, item['effective'])
        atomic_write(meta / 'defaults' / relative, item['default'])
        if item.get('override'):
            atomic_write(meta / 'overrides' / relative, item['effective'])
    pending.unlink()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('root')
    p.add_argument('stage', nargs='?')
    p.add_argument('--accept-local', metavar='RELATIVE_FILE', help='explicitly retain the local edit for a reported upstream conflict')
    args = p.parse_args()
    try:
        recover(args.root)
        if args.accept_local:
            relative = Path(args.accept_local)
            if relative.is_absolute() or '..' in relative.parts:
                raise ConfigConflict('Expected a relative configuration file')
            meta = Path(args.root) / '.beepa-config'
            candidate = meta / 'conflicts' / (str(relative) + '.incoming')
            if not candidate.is_file() or not (Path(args.root) / relative).is_file():
                raise ConfigConflict('No reported conflict for this configuration file')
            atomic_write(meta / 'defaults' / relative, candidate.read_text())
            candidate.unlink()
            print('Local override accepted; rerun setup/update to apply the remaining changes.')
        elif args.stage:
            activate(args.root, args.stage)
        else:
            p.error('stage or --accept-local is required')
    except (ValueError, OSError) as exc:
        p.exit(1, 'render: %s\n' % exc)
