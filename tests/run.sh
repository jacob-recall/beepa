#!/bin/sh
# Run unit tests inside the pinned node:20-alpine container (no local node dependency).
set -e
cd "$(dirname "$0")/.."
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/consent.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/master_invites.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/master_hidden.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/user_invites.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/csp_parity.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/contact_consent.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/contacts_profile_handles.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/proposal_identifier.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/proposal_parse.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/proposal_row.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/auto_merge_number.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/proposal_classification.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/share_bulk_action.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/direct_send_reconfirm.test.js
python3 tests/unit/contacts_store.test.py
python3 tests/unit/import_macos.test.py
python3 tests/unit/contact_consent_py.test.py
python3 tests/unit/uplink_reconcile.test.py
python3 tests/unit/enroll_require_manager.test.py
python3 tests/unit/enroll_delete_teammate.test.py
python3 tests/unit/enroll_password_derivation.test.py
python3 tests/conformance/consent_conformance.py
python3 tests/unit/uplink_proposal_sanitize.test.py
python3 tests/unit/number_resolver.test.py
python3 tests/unit/enroll_proxy_guard.test.py
python3 tests/unit/consent_py.test.py
python3 tests/unit/uplink_proposals.test.py
python3 tests/unit/uplink_migration.test.py
python3 tests/unit/uplink_direct_send.test.py
python3 tests/unit/uplink_share_level.test.py
python3 tests/unit/uplink_sources.test.py
