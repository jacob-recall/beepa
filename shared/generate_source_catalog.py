#!/usr/bin/env python3
"""Regenerate the browser adapter after editing source_catalog.json."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def javascript():
    catalog = json.loads((ROOT / 'source_catalog.json').read_text())
    return ('// Generated from shared/source_catalog.json; run shared/generate_source_catalog.py.\n'
            '// Pure metadata: no network, session, DOM or send-capable imports.\n'
            'export const SOURCES = Object.freeze(' + json.dumps(catalog, indent=2, ensure_ascii=False)
            + '.map(source => Object.freeze(source)));\n'
            + 'export const PLATFORM_ICON = Object.freeze(Object.fromEntries(SOURCES.filter(s => s.kind === "source").map(s => [s.id, s.icon])));\n'
            + 'export const PLATFORM_LABEL = Object.freeze(Object.fromEntries(SOURCES.filter(s => s.kind === "source").map(s => [s.id, s.label])));\n')


if __name__ == '__main__':
    (ROOT / 'model/source_catalog.js').write_text(javascript())
