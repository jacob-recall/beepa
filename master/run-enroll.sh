#!/usr/bin/env bash
# Persistent enrollment/admin service runner for launchd
# (com.jkali.master-enroll). Execs `enroll.py serve` on loopback 8019 so the
# manager console can always reach POST /admin/add-teammate and the uplink can
# reach POST /enroll/exchange. launchd's KeepAlive restarts it if it exits.
#
# The endpoint binds 127.0.0.1 only; in production it sits behind the same TLS
# reverse proxy as the master CS API, so it adds no new public surface. It
# reads the shared secret + password-derivation key from synapse/.secrets.local
# and teammate facts from tokens.local (both mode 600). No passwords are
# stored anywhere; they are derived per login (master/CLAUDE.md). This
# service's stdout/stderr land in launchd logs — nothing here may ever print
# a password or key. See master/enroll.py.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"
exec "${BEEPA_PYTHON:-/usr/bin/python3}" "${HERE}/enroll.py" serve --host 127.0.0.1 --port 8019
