#!/bin/sh
# Run unit tests inside the pinned node:20-alpine container (no local node dependency).
set -e
cd "$(dirname "$0")/.."
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/consent.test.js
