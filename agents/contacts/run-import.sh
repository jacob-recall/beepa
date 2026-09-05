#!/usr/bin/env bash
# One-shot runner for launchd (com.jkali.contacts-import). Runs the
# importer once and exits; launchd's StartInterval re-invokes it on the
# next tick rather than this script looping or backgrounding anything.
# No venv, no pip dependencies — stdlib only.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${BEEPA_PYTHON:-/usr/bin/python3}" "${HERE}/import_macos.py"
