#!/usr/bin/env bash
# Persistent enrollment/admin service runner for launchd
# (com.jkali.master-enroll). Execs `enroll.py serve` on loopback 8019 so the
# manager console can always reach POST /admin/add-teammate and the uplink can
# reach POST /enroll/exchange. launchd's KeepAlive restarts it if it exits.
#
# The endpoint binds 127.0.0.1 only; in production it sits behind the same TLS
# reverse proxy as the master CS API, so it adds no new public surface. It
# reads the shared secret / teammate facts from the already-mode-600 files
# under master/ (synapse/.secrets.local, tokens.local, .provision-state.local).
# See master/enroll.py and master/CLAUDE.md.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"
exec /usr/bin/python3 "${HERE}/enroll.py" serve --host 127.0.0.1 --port 8019
