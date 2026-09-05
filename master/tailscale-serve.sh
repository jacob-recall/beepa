#!/usr/bin/env bash
# master/tailscale-serve.sh — expose THIS machine's master stack to teammates
# over Tailscale, and record the tailnet URLs so enroll.py hands them out.
#
# The master Synapse (127.0.0.1:8018) and the enroll service (127.0.0.1:8019)
# stay bound to loopback; `tailscale serve` is the tailnet ingress in front of
# them (TLS-terminated with a real cert for this node's MagicDNS name, reachable
# only by your tailnet — never the public internet, and never the LAN). This is
# the "TLS reverse proxy" the master/CLAUDE.md notes assume.
#
#   Console + API : https://<magicdns>        (443)  -> 127.0.0.1:8017
#   Enrollment     : https://<magicdns>:8443          -> 127.0.0.1:8019
#
# Idempotent. Re-run after a reboot or when the enroll/Synapse stack restarts.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Resolve mutable state when called directly from a pulled checkout too.
if [ -n "${BEEPA_MASTER_STATE_DIR:-}" ]; then
  MASTER_STATE="${BEEPA_MASTER_STATE_DIR}"
elif [ -n "${BEEPA_INSTALL_ROOT:-}" ]; then
  MASTER_STATE="${BEEPA_INSTALL_ROOT}/master"
elif [ -f "${HERE}/../install_config.py" ]; then
  MASTER_STATE="$(python3 - "${HERE}/.." <<'PYROOT'
import pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from install_config import read_manifest
manifest = read_manifest(root) or {}
state = pathlib.Path(manifest['state_root']) if manifest.get('state_initialized') else root
print(state / 'master')
PYROOT
)"
else
  MASTER_STATE="${HERE}"
fi
log()  { printf '[ts-serve] %s\n' "$*" >&2; }
fail() { printf '[ts-serve] ERROR: %s\n' "$*" >&2; exit 1; }

TS="$(command -v tailscale || true)"
[ -n "${TS}" ] || TS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
command -v "${TS}" >/dev/null 2>&1 || [ -x "${TS}" ] || fail "tailscale CLI not found (install Tailscale, then re-run)."

"${TS}" status >/dev/null 2>&1 || fail "tailscale is not connected — run 'tailscale up' first."

# MagicDNS name of THIS node (strip trailing dot)
DNS="$("${TS}" status --json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))')"
[ -n "${DNS}" ] || fail "could not read this node's MagicDNS name (is MagicDNS enabled on your tailnet?)"
log "this master node: ${DNS}"

SYNAPSE_URL="https://${DNS}"
ENROLL_URL="https://${DNS}:8443"
GATEWAY_PORT="${MASTER_GATEWAY_PORT:-8017}"
case "${GATEWAY_PORT}" in
  ''|*[!0-9]*) fail "MASTER_GATEWAY_PORT must be a port number" ;;
esac
curl -fsS "http://127.0.0.1:${GATEWAY_PORT}/health" >/dev/null \
  || fail "master gateway is not ready on ${GATEWAY_PORT}; install/start org.beepa.master-gateway first"

# Put serve in front of the two loopback services. --yes = no prompt; --bg =
# persist in the background. If HTTPS certs aren't enabled for the tailnet these
# fail — surface the fix rather than dying silently.
serve() { # <https-port> <target> <label>
  if "${TS}" serve --bg --yes --https="$1" "$2" 2>/tmp/ts-serve.$$ ; then
    log "serving $3  https port $1  -> $2"
  else
    log "FAILED to serve $3 on :$1 — $(cat /tmp/ts-serve.$$ 2>/dev/null | head -1)"
    log "  If this is a certs error, enable HTTPS for your tailnet:"
    log "  https://login.tailscale.com/admin/dns  (toggle 'HTTPS Certificates'), then re-run."
    rm -f /tmp/ts-serve.$$; return 1
  fi
  rm -f /tmp/ts-serve.$$
}

ok=1
serve 443  "http://127.0.0.1:${GATEWAY_PORT}" "master console and Matrix API" || ok=0
serve 8443 http://127.0.0.1:8019 "enrollment"     || ok=0
[ "${ok}" = 1 ] || fail "serve mapping failed; advertised endpoints were not updated"

# Record the tailnet URLs so enroll.py advertises them (read from tokens.local
# by _public_hs_url() / the add-teammate enroll_url). URLs are not secrets, but
# tokens.local is 600 and already holds them alongside real tokens.
set_kv() { # key value
  local f="${MASTER_STATE}/tokens.local"
  [ -f "${f}" ] || fail "master/tokens.local missing — run master/provision.sh first."
  grep -v "^$1=" "${f}" > "${f}.tmp" 2>/dev/null || true
  # enroll.py's parser only reads KEY='value' (single-quoted) lines.
  printf "%s='%s'\n" "$1" "$2" >> "${f}.tmp"
  mv -f "${f}.tmp" "${f}"; chmod 600 "${f}"
}
set_kv MASTER_PUBLIC_URL "${SYNAPSE_URL}"
set_kv ENROLL_PUBLIC_URL "${ENROLL_URL}"
log "recorded MASTER_PUBLIC_URL + ENROLL_PUBLIC_URL in master/tokens.local"

cat >&2 <<DONE

[ts-serve] ============================================================
[ts-serve] Master is reachable on your tailnet:
    Synapse CS API :  ${SYNAPSE_URL}
    Manager console:  ${SYNAPSE_URL}/apps/master/
    Enrollment     :  ${ENROLL_URL}

  A teammate onboards (from their own machine, on the same tailnet):
    1. git clone <repo> && cd <repo>
    2. ./install.sh          # stands up their hub, renders config, provisions
    3. when it asks for the master enrollment URL, give them:
         ${ENROLL_URL}
       and the one-time code from:  python3 master/enroll.py mint <name>
       (or the manager console's "add teammate")

  Their uplink then mirrors up to  ${SYNAPSE_URL}  over Tailscale.
[ts-serve] ============================================================
DONE
[ "${ok}" = 1 ] || { log "one or more serve mappings failed — see above."; exit 1; }
