#!/usr/bin/env bash
# Persistent uplink runner for launchd (com.jkali.uplink). Sources the mode-600
# env file (which holds the LOCAL_/MASTER_ tokens) and execs the daemon;
# launchd's KeepAlive restarts it if it exits (e.g. master briefly unreachable).
#
# The env file (agents/uplink/uplink.env.local, gitignored) is written by the
# operator / enrollment flow and defines: LOCAL_HS_URL, LOCAL_USER, LOCAL_TOKEN,
# MASTER_HS_URL, MASTER_USER, MASTER_TOKEN, MANAGER_MXID, MASTER_SPACE, and
# optionally UPLINK_DB / UPLINK_BACKFILL. See agents/uplink/CLAUDE.md.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENVFILE="${HERE}/uplink.env.local"
if [ ! -f "${ENVFILE}" ]; then
  echo "run-uplink: missing ${ENVFILE} (tokens); not starting" >&2
  exit 78
fi
set -a
# shellcheck disable=SC1090
source "${ENVFILE}"
set +a
exec /usr/bin/python3 "${HERE}/uplink.py"
