#!/bin/sh
# Native unit and consent-conformance checks; discovers new regressions automatically.
set -eu
cd "$(dirname "$0")/.."
TEST_PYTHON="${BEEPA_PYTHON:-}"
if [ -z "${TEST_PYTHON}" ]; then
  TEST_PYTHON="$(python3 - <<'PY'
import json, os
try:
    with open('.beepa-install.json') as f:
        candidate = json.load(f).get('python_path', '')
    print(candidate if isinstance(candidate, str) and os.path.isfile(candidate) and os.access(candidate, os.X_OK) else 'python3')
except (OSError, ValueError):
    print('python3')
PY
)"
fi
exec "${TEST_PYTHON}" tests/run.py "$@"
