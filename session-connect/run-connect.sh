#!/usr/bin/env bash
# Persistent one-click IG/LI/X connect helper for launchd
# (com.jkali.session-connect). Execs `connect_server.py` on loopback 8021 so the
# teammate app (apps/user, http://127.0.0.1:8011) can drive the Instagram /
# LinkedIn / X login in one click. launchd's KeepAlive restarts it if it exits.
#
# The endpoint binds 127.0.0.1 only. It reads Chrome cookies + each bridge's
# provisioning shared_secret ONLY inside an authorized POST /connect/<net>/start
# — never at load. docker must be on PATH because connect.api() shells
# `docker compose exec` into the bridge container.
# See session-connect/connect_server.py and session-connect/connect.py.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"
exec /usr/bin/python3 "${HERE}/connect_server.py" --host 127.0.0.1 --port 8021
