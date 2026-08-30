#!/bin/sh
# Integration suite for the uplink one-way sync (the 12 Phase-2 scenarios).
#
# Prereqs (both are SEPARATE from the live matrix-wa hub, which this suite never
# touches):
#   1. matrix-master up + provisioned (master/tokens.local present):
#        docker compose -p matrix-master --env-file master/.env \
#          -f master/docker-compose.master.yml up -d && master/provision.sh
#   2. the throwaway TEST-USER hub up (this dir's compose):
#        docker compose -p matrix-synctest \
#          -f tests/integration/docker-compose.test.yml up -d
#
# Then:  tests/integration/run.sh   [scenario-filter ...]
# e.g.:  tests/integration/run.sh            # all 8
#        tests/integration/run.sh 3_offline  # just the catch-up scenario
set -e
export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"
cd "$(dirname "$0")/../.."
exec python3 tests/integration/harness.py "$@"
