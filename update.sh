#!/usr/bin/env bash
# Apply an already-pulled release. Never resets, provisions or logs out accounts.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${BEEPA_PYTHON:-python3}" "${HERE}/beepa_update.py" --root "${HERE}" "$@"
