"""Canonical code-owned network metadata for Python services."""
import json
from pathlib import Path

SOURCES = tuple(json.loads(Path(__file__).with_name('source_catalog.json').read_text()))
SPACE_SOURCES = {s['spaceName']: s['id'] for s in SOURCES if s['kind'] == 'source'}
