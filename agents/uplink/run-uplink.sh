#!/usr/bin/env bash
# Persistent uplink runner for launchd (com.jkali.uplink). Sources the mode-600
# env file(s) that hold the LOCAL_/MASTER_ tokens and execs the daemon;
# launchd's KeepAlive restarts it if it exits (e.g. master briefly unreachable).
#
# Two env files, either or both:
#   local.env.local  — LOCAL_HS_URL/LOCAL_USER/LOCAL_TOKEN, written by
#                      hub/provision-user.sh at setup time. With ONLY this, the
#                      daemon starts and idles, polling the local hub's account-
#                      data for com.jkali.master_link — so a teammate can enroll
#                      entirely from the app ("Connect to organization") and the
#                      already-running daemon picks the master creds up on its
#                      next loop, with no terminal step.
#   uplink.env.local — LOCAL_* + MASTER_* (MASTER_HS_URL/MASTER_USER/
#                      MASTER_TOKEN/MANAGER_MXID/MASTER_SPACE) written by the
#                      enrollment flow (agents/uplink/link.sh). Sourced AFTER
#                      local.env.local so its values win when already enrolled.
# See agents/uplink/CLAUDE.md.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_ENV="${HERE}/local.env.local"
ENVFILE="${HERE}/uplink.env.local"
if [ ! -f "${LOCAL_ENV}" ] && [ ! -f "${ENVFILE}" ]; then
  echo "run-uplink: no local.env.local or uplink.env.local (no hub creds); not starting" >&2
  exit 78
fi
set -a
# shellcheck disable=SC1090
[ -f "${LOCAL_ENV}" ] && source "${LOCAL_ENV}"   # LOCAL_* — idle-until-connected
# shellcheck disable=SC1090
[ -f "${ENVFILE}" ] && source "${ENVFILE}"       # full creds if already enrolled (overrides)
set +a
exec /usr/bin/python3 "${HERE}/uplink.py"
