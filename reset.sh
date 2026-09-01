#!/usr/bin/env bash
# reset.sh — COMPLETE local teardown of the Beepa deployment on THIS machine, so
# the next  ./master-setup.sh + ./install.sh  is a true from-scratch deploy.
#
# Takes down everything it can locally:
#   - both Docker stacks (matrix-wa + matrix-master): containers, VOLUMES, IMAGES
#   - any leftover test stacks (matrix-synctest, beepa-drytest*)
#   - the launchd agents (connect helpers, uplink, contacts, imessage, enroll)
#   - the Tailscale serve exposure
#   - ALL generated config + secrets + runtime state
#   - the built iMessage CLI + the Beeper source clone
#
# DESTRUCTIVE: message history + every bridge login live in the Docker volumes
# and are wiped. Back up first. Only gitignored/generated files are removed —
# tracked source is never touched (the git check at the end proves it).
#
#   ./reset.sh          # prompts for a typed confirmation
#   ./reset.sh --yes    # skip the prompt
set -uo pipefail   # intentionally NOT -e: teardown must continue past missing things
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"
log() { printf '[reset] %s\n' "$*" >&2; }

if [ "${1:-}" != "--yes" ]; then
  cat >&2 <<'WARN'
[reset] This PERMANENTLY deletes, on this machine:
  - both Docker stacks (matrix-wa + matrix-master): containers, VOLUMES, and images
    -> all message history and every bridge login are wiped
  - leftover test stacks (matrix-synctest, beepa-drytest*)
  - the launchd agents, the Tailscale serve exposure
  - all generated config + secrets (synapse/, bridge dirs, master/synapse, tokens, .env, …)
  - the built iMessage CLI + the Beeper source clone
  Make sure you have a backup.
WARN
  printf "[reset] Type 'reset' to proceed: " >&2
  read -r ans || true
  [ "${ans:-}" = "reset" ] || { log "aborted."; exit 1; }
fi

cd "${HERE}"

# --- 1. Docker: stacks + volumes + images ---
if command -v docker >/dev/null 2>&1; then
  log "tearing down Docker stacks (containers + volumes + images)…"
  docker compose -f docker-compose.yml --profile bridge --profile client --profile escape down --rmi all -v 2>/dev/null || true
  docker compose -p matrix-master  -f master/docker-compose.master.yml            down --rmi all -v 2>/dev/null || true
  # leftover test stacks (best-effort; harmless if absent)
  docker compose -p matrix-synctest -f tests/integration/docker-compose.test.yml   down --rmi all -v 2>/dev/null || true
  for proj in beepa-drytest beepa-drytest2 beepa-drytest3; do
    docker compose -p "${proj}" down --rmi all -v 2>/dev/null || true
  done
  log "docker stacks removed"
else
  log "docker not found — skipping stack teardown"
fi

# --- 2. launchd agents ---
for a in session-connect gmessages-connect uplink contacts-import imessage-daemon master-enroll; do
  p="${HOME}/Library/LaunchAgents/com.jkali.${a}.plist"
  launchctl unload "${p}" 2>/dev/null || true
  [ -f "${p}" ] && rm -f "${p}" && log "removed agent com.jkali.${a}"
done

# --- 3. Tailscale exposure ---
if command -v tailscale >/dev/null 2>&1; then
  tailscale serve reset 2>/dev/null && log "tailscale serve reset"
fi

# --- 4. generated config, secrets, runtime state (all gitignored) ---
rm -rf "${HERE}/synapse" "${HERE}/whatsapp" "${HERE}/meta" "${HERE}/gmessages" \
       "${HERE}/linkedin" "${HERE}/twitter" "${HERE}/element/config.json" "${HERE}/.env"
rm -rf "${HERE}/master/synapse" "${HERE}/master/.env" "${HERE}/master/tokens.local" \
       "${HERE}/master/.provision-state.local" "${HERE}/master/enrollments.local" "${HERE}/master/logs"
rm -rf "${HERE}/agents/uplink/uplink.env.local" "${HERE}/agents/uplink/local.env.local" \
       "${HERE}/agents/uplink/state.db" "${HERE}/agents/uplink/logs" "${HERE}/hub/.local-user.local"
rm -rf "${HERE}/agents/contacts/contacts.db" "${HERE}/agents/contacts/logs"
rm -rf "${HERE}/session-connect/logs" "${HERE}/gmessages-connect/logs"
log "removed generated hub + master config/secrets/state"

# --- 5. iMessage: built binary + Beeper clone + local state ---
rm -rf "${HERE}/imessage/bin" "${HERE}/imessage/platform-imessage" "${HERE}/imessage/daemon.json" \
       "${HERE}/imessage/logs" "${HERE}/imessage/tmp"
rm -f "${HERE}"/imessage/*.db
log "removed iMessage binary + Beeper source clone"

# --- 6. safety: restore any TRACKED file the rm's caught (e.g. a logs/.gitignore
#        dir-keeper) — only generated/ignored state should actually be gone ---
deleted="$(git -C "${HERE}" diff --name-only --diff-filter=D 2>/dev/null || true)"
if [ -n "${deleted}" ]; then
  printf '%s\n' "${deleted}" | while IFS= read -r f; do
    [ -n "$f" ] && git -C "${HERE}" checkout -- "$f" 2>/dev/null || true
  done
  log "restored tracked dir-keeper(s): $(printf '%s ' ${deleted})"
fi
if git -C "${HERE}" status --short 2>/dev/null | grep -qE '^ ?D '; then
  log "WARNING: a tracked file is still deleted — check 'git status'"
else
  log "clean — only generated/ignored files were removed ✓"
fi

log "COMPLETE. Deploy from scratch with:"
log "  ./master-setup.sh   ->   python3 master/enroll.py mint jkali   ->   ./install.sh"
