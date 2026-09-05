#!/bin/sh
# Create disposable local + master stacks, run scenarios, clean up only those stacks.
set -e
export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"
cd "$(dirname "$0")/../.."
exec python3 tests/integration/sandbox.py "$@"
