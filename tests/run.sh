#!/bin/sh
# Run unit tests inside the pinned node:20-alpine container (no local node dependency).
set -e
cd "$(dirname "$0")/.."
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/consent.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/master_invites.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/user_invites.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/csp_parity.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/contact_consent.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/contacts_profile_handles.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/proposal_identifier.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/proposal_parse.test.js
python3 tests/unit/contacts_store.test.py
python3 tests/unit/import_macos.test.py
python3 tests/unit/contact_consent_py.test.py
python3 tests/unit/uplink_reconcile.test.py
