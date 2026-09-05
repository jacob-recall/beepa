#!/usr/bin/env bash
# agents/uplink/link.sh <enroll-url> <code>
#
# One-command linker the new teammate runs on THEIR OWN install to join the
# org. It:
#   1. collects this install's LOCAL hub details (LOCAL_HS_URL / LOCAL_USER /
#      LOCAL_TOKEN) from env vars or an interactive prompt — the teammate
#      supplies their OWN local hub token here, never the master's;
#   2. redeems the one-time enrollment code via enroll_client.py, which returns
#      the MASTER_* scoped credentials (master base URL, the teammate's scoped
#      master token, manager mxid, master space);
#   3. writes agents/uplink/uplink.env.local (mode 600) combining LOCAL_* +
#      MASTER_*;
#   4. installs + loads the launchd uplink daemon.
#
# Safe to re-run. The enrollment code is single-use: if a prior run already
# consumed it, step 2 fails cleanly (the existing env file is left untouched)
# and the manager can issue a fresh code. Nothing here can send externally.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
ENROLL_URL="${1:-}"
CODE="${2:-}"

if [ -z "${ENROLL_URL}" ] || [ -z "${CODE}" ]; then
  cat >&2 <<'USAGE'
usage: agents/uplink/link.sh <enroll-url> <code>

  <enroll-url>  the master enrollment endpoint (e.g. https://master.example
                or, for a local test, http://127.0.0.1:8019)
  <code>        the one-time enrollment code the manager gave you

Local hub details are taken from the environment if set, else prompted for:
  LOCAL_HS_URL  your local homeserver base URL (e.g. http://127.0.0.1:8008)
  LOCAL_USER    your local mxid            (e.g. @you:localhost)
  LOCAL_TOKEN   your local access token    (kept only in this mode-600 file)
USAGE
  exit 2
fi

log() { printf '[link] %s\n' "$*" >&2; }
fail() { printf '[link] ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. collect LOCAL hub details (env or interactive prompt) ---
LOCAL_HS_URL="${LOCAL_HS_URL:-}"
LOCAL_USER="${LOCAL_USER:-}"
LOCAL_TOKEN="${LOCAL_TOKEN:-}"

if [ -z "${LOCAL_HS_URL}" ]; then
  printf 'Local homeserver URL [http://127.0.0.1:8008]: ' >&2
  read -r LOCAL_HS_URL || true
  LOCAL_HS_URL="${LOCAL_HS_URL:-http://127.0.0.1:8008}"
fi
if [ -z "${LOCAL_USER}" ]; then
  printf 'Local mxid (e.g. @you:localhost): ' >&2
  read -r LOCAL_USER || true
fi
if [ -z "${LOCAL_TOKEN}" ]; then
  printf 'Local access token (input hidden): ' >&2
  read -rs LOCAL_TOKEN || true
  printf '\n' >&2
fi

[ -n "${LOCAL_HS_URL}" ] || fail "LOCAL_HS_URL is required"
[ -n "${LOCAL_USER}" ]   || fail "LOCAL_USER is required"
[ -n "${LOCAL_TOKEN}" ]  || fail "LOCAL_TOKEN is required"

case "${LOCAL_USER}" in
  @*:*) : ;;
  *) fail "LOCAL_USER must be a full mxid like @you:localhost" ;;
esac

# --- 2. redeem the code -> MASTER_* scoped creds (to a temp file) ---
if [ -z "${BEEPA_INSTALL_ROOT:-}" ]; then
  BEEPA_INSTALL_ROOT="$(python3 - "${REPO_ROOT}" <<'PYROOT'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from install_config import read_manifest
root = Path(sys.argv[1]).resolve()
manifest = read_manifest(root)
print(manifest['state_root'] if manifest and manifest.get('state_initialized') else root)
PYROOT
)"
fi
export BEEPA_INSTALL_ROOT
ENVFILE="${BEEPA_INSTALL_ROOT}/agents/uplink/uplink.env.local"
mkdir -p "$(dirname "${ENVFILE}")"
umask 077
MASTER_ENV="$(mktemp "${TMPDIR:-/tmp}/uplink.master.XXXXXX")"
trap 'rm -f "${MASTER_ENV}"' EXIT

log "redeeming enrollment code against ${ENROLL_URL} ..."
python3 "${HERE}/enroll_client.py" \
  --enroll-url="${ENROLL_URL}" --code="${CODE}" --out="${MASTER_ENV}" \
  || fail "enrollment redemption failed (code may be used/expired); env file left unchanged"

# Pull the MASTER_* vars the exchange returned into this shell.
set -a
# shellcheck disable=SC1090
. "${MASTER_ENV}"
set +a

[ -n "${MASTER_HS_URL:-}" ] || fail "exchange did not return MASTER_HS_URL"
[ -n "${MASTER_USER:-}" ]   || fail "exchange did not return MASTER_USER"
[ -n "${MASTER_TOKEN:-}" ]  || fail "exchange did not return MASTER_TOKEN"
[ -n "${MASTER_SPACE:-}" ]  || fail "exchange did not return MASTER_SPACE"

# --- 3. write the combined uplink env file (mode 600), atomically ---
export LOCAL_HS_URL LOCAL_USER LOCAL_TOKEN
export MASTER_HS_URL MASTER_USER MASTER_TOKEN MASTER_SPACE
export MANAGER_MXID MASTER_AUTHORITY_ID MASTER_DATA_EPOCH MASTER_ENROLL_URL
python3 - "${ENVFILE}" <<'PYCODE'
import json, os, shlex, sys, tempfile, urllib.parse, urllib.request
out = sys.argv[1]
keys = ('LOCAL_HS_URL','LOCAL_USER','LOCAL_TOKEN','MASTER_HS_URL','MASTER_USER',
        'MASTER_TOKEN','MANAGER_MXID','MASTER_SPACE','MASTER_AUTHORITY_ID',
        'MASTER_DATA_EPOCH','MASTER_ENROLL_URL')
fd, tmp = tempfile.mkstemp(prefix='.uplink-env-', dir=os.path.dirname(out))
with os.fdopen(fd, 'w') as stream:
    for key in keys:
        stream.write(key + '=' + shlex.quote(os.environ.get(key, '')) + '\n')
os.replace(tmp, out)
# Explicitly reconnect through the same control record used by the UI.
# Merely writing environment credentials cannot undo a persisted disconnect.
link = {key.lower().replace('master_hs_url', 'master_hs_url'): os.environ.get(key, '')
        for key in keys if key.startswith('MASTER_') or key == 'MANAGER_MXID'}
path = '/_matrix/client/v3/user/' + urllib.parse.quote(os.environ['LOCAL_USER'], safe='') + '/account_data/com.jkali.master_link'
request = urllib.request.Request(os.environ['LOCAL_HS_URL'].rstrip('/') + path,
    data=json.dumps(link).encode(), method='PUT', headers={
        'Authorization': 'Bearer ' + os.environ['LOCAL_TOKEN'], 'Content-Type': 'application/json'})
with urllib.request.urlopen(request, timeout=30) as response:
    response.read()
PYCODE
log "wrote ${ENVFILE} (mode 600) and connected this local hub"

# --- 4. install the same portable launchd service used by setup ---
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
if command -v launchctl >/dev/null 2>&1; then
  python3 "${REPO_ROOT}/install_config.py" --root "${REPO_ROOT}" install-agent uplink
else
  log "launchctl unavailable; run bash '${HERE}/run-uplink.sh'"
fi

cat >&2 <<DONE

[link] Done. Next steps:
  - The uplink daemon is installed and syncing your SHARED conversations up to
    the master. Nothing you have not shared ever leaves this machine.
  - Choose what to share in your teammate app (apps/user): per-conversation,
    using explicit Private, Share, or Direct levels.
  - Logs: ${HERE}/logs/uplink.log and uplink.err
  - To disconnect: use Settings > Disconnect in Beepa.
DONE
