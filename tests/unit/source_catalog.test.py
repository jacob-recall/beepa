import importlib.util
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('catalog_generator', ROOT/'shared/generate_source_catalog.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)
assert (ROOT/'shared/model/source_catalog.js').read_text() == generator.javascript(), 'Regenerate source catalog adapter'
catalog = json.loads((ROOT/'shared/source_catalog.json').read_text())
assert len({item['id'] for item in catalog}) == len(catalog)
assert len([item for item in catalog if item['kind'] == 'source']) == 6
assert all('jkali' not in item.get('botMxid','') for item in catalog)
print('Canonical source catalog and generated adapter agree.')
