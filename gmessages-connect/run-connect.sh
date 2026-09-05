#!/usr/bin/env bash
# Persistent one-click Google Messages connect helper for launchd
# (com.jkali.gmessages-connect). Execs `connect_server.py` on loopback 8020 so
# the teammate app (apps/user, http://127.0.0.1:8011) can drive the Google
# Messages login in one click. launchd's KeepAlive restarts it if it exits.
#
# The endpoint binds 127.0.0.1 only. It reads Chrome cookies + the bridge's
# provisioning shared_secret ONLY inside an authorized POST /connect/gmessages/
# start — never at load. docker must be on PATH because connect.api() shells
# `docker compose exec` into the gmessages bridge container.
# See gmessages-connect/connect_server.py and gmessages-connect/CLAUDE.md.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"
exec "${BEEPA_PYTHON:-/usr/bin/python3}" "${HERE}/connect_server.py" --host 127.0.0.1 --port 8020
