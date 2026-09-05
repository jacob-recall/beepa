#!/usr/bin/env bash
# master/provision.sh — idempotent provisioning of the matrix-master homeserver.
#
# Creates @manager:master plus one account PER TEAMMATE (server-side, via the
# registration shared secret); logs each in for an access token; creates one
# Matrix SPACE per teammate ("space:<user>", which apps/master strips to the
# display label) owned by that teammate with @manager invited at a read-only
# power level (events_default 50, teammate PL 100, manager PL 0).
#
# The teammate roster is the real people who sync to this master — set via the
# TEAMMATES env var (space-separated additions). Existing roster entries are
# always retained. A new master may have only the manager until teammates join.
#
# Re-running is safe: existing accounts are skipped, tokens refreshed, and a
# space already recorded (and still joined) is not recreated.
#
# Outputs:
#   master/tokens.local            access tokens as shell-sourceable vars (600)
#   master/.provision-state.local  passwords + space room ids (600)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"

CS_BASE="${MASTER_CS_BASE:-http://127.0.0.1:8018}"
COMPOSE=(docker compose -p matrix-master -f "${HERE}/docker-compose.master.yml")
HS_YAML="/data/homeserver.yaml"          # path inside the synapse container
DATA_DIR="${BEEPA_MASTER_STATE_DIR:-${BEEPA_INSTALL_ROOT:-$(dirname "${HERE}")}/master}"
TOKENS_FILE="${DATA_DIR}/tokens.local"
STATE_FILE="${DATA_DIR}/.provision-state.local"
SERVER="master"

MANAGER_LP="manager"
# Real teammate roster (space-separated usernames). An explicit TEAMMATES env
# var always wins (the integration harness passes TEAMMATES="alice bob"). We
# capture that override BEFORE sourcing the state file below, which may carry a
# persisted TEAMMATES roster from a prior provision OR from console
# "Add teammate" actions — so a reprovision keeps everyone the console added.
ENV_TEAMMATES="${TEAMMATES-}"

umask 077
touch "${STATE_FILE}"; chmod 600 "${STATE_FILE}"

log() { printf '[provision] %s\n' "$*" >&2; }
fail() { printf '[provision] ERROR: %s\n' "$*" >&2; exit 1; }

upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_'; }

# --- passwords + persisted roster: read persisted values (if any) ---
# shellcheck disable=SC1090
[ -s "${STATE_FILE}" ] && source "${STATE_FILE}" || true
PERSISTED_TEAMMATES="${TEAMMATES:-}"
[ -s "${TOKENS_FILE}" ] && source "${TOKENS_FILE}" || true

# Roster resolution: explicit env override wins; else the persisted roster the
# last provision / console-add wrote (sourced just above as a scalar TEAMMATES);
# else the single real user "jkali".
ROSTER="$(python3 "${HERE}/roster.py" "${PERSISTED_TEAMMATES}" "${MASTER_TEAMMATES:-}" "${ENV_TEAMMATES}")" \
  || fail "invalid teammate roster"
read -r -a TEAMMATES <<< "${ROSTER}"

# Passwords are DERIVED, never stored: master/enroll.py provision-account
# registers/logs-in each account from TEAMMATE_PASSWORD_KEY (synapse/
# .secrets.local, written by master/setup.sh) and migrates any legacy stored
# password on the way. MANAGER_PW in the environment is only a legacy-
# migration input for a manager account that predates derivation; it is never
# defaulted and never persisted. The manager's console login password is
# `python3 master/enroll.py password manager --manager`.
ENROLL_PY="${HERE}/enroll.py"

# provision-account prints {"mxid","token","migrated"} on stdout; nothing
# secret crosses argv or a shell variable. Plain assignment (never
# `local x=$(...)`, which masks the exit status) + validated fields.
provision_account() {
  # $1 = localpart, $2 = optional --manager; prints the JSON on stdout
  /usr/bin/python3 "${ENROLL_PY}" provision-account "$1" ${2:+"$2"}
}
json_field() { python3 -c 'import sys,json;print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

# Keep saved scoped tokens stable when they still authenticate as the expected
# account. provision-account still validates/migrates the derived password.
token_valid() {
  local token="$1" expected="$2"
  [ -n "${token}" ] || return 1
  curl -fsS --max-time 15 -H "Authorization: Bearer ${token}" \
    "${CS_BASE}/_matrix/client/v3/account/whoami" \
    | python3 -c 'import json,sys;sys.exit(json.load(sys.stdin).get("user_id") != sys.argv[1])' "${expected}" 2>/dev/null
}

# --- wait for Synapse health ---
log "waiting for Synapse CS API at ${CS_BASE} ..."
for i in $(seq 1 60); do
  if curl -fsS "${CS_BASE}/health" >/dev/null 2>&1; then log "Synapse healthy"; break; fi
  [ "$i" -eq 60 ] && fail "Synapse did not become healthy at ${CS_BASE}"
  sleep 2
done

# (register/login_token removed: master/enroll.py provision-account owns
# registration + login + password migration — see the block above.)

# --- does a recorded room still exist with the teammate joined? ---
space_valid() {
  local token="$1" room="$2" code
  [ -z "${room}" ] && return 1
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${token}" \
    "${CS_BASE}/_matrix/client/v3/rooms/${room}/state/m.room.create/") || return 1
  [ "${code}" = "200" ]
}

# --- create a "space:<user>" owned by the teammate, manager invited read-only --
create_space() {
  local user="$1" token="$2" resp room
  resp=$(curl -fsS -XPOST "${CS_BASE}/_matrix/client/v3/createRoom" \
    -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
    -d "$(cat <<JSON
{
  "name": "space:${user}",
  "topic": "Read-only master space for @${user}:${SERVER}",
  "preset": "private_chat",
  "creation_content": { "type": "m.space" },
  "invite": ["@${MANAGER_LP}:${SERVER}"],
  "power_level_content_override": {
    "events_default": 50, "state_default": 50, "invite": 50,
    "kick": 50, "ban": 50, "redact": 50, "users_default": 0,
    "users": { "@${user}:${SERVER}": 100, "@${MANAGER_LP}:${SERVER}": 0 }
  }
}
JSON
)") || fail "createRoom (space:${user}) failed"
  room=$(printf '%s' "${resp}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["room_id"])') \
    || fail "no room_id for space:${user}"
  printf '%s' "${room}"
}

# =========================== run ===========================
log "provisioning @${MANAGER_LP}:${SERVER} (derived password; migrating any legacy one)"
acct=$(provision_account "${MANAGER_LP}" --manager) || fail "manager provisioning failed — see enroll.py stderr above"
MANAGER_TOKEN=$(printf '%s' "${acct}" | json_field token) || fail "manager: no token in provision-account output"
if token_valid "${MASTER_MANAGER_TOKEN:-}" "@${MANAGER_LP}:${SERVER}"; then
  MANAGER_TOKEN="${MASTER_MANAGER_TOKEN}"
fi
[ -n "${MANAGER_TOKEN}" ] || fail "manager: empty token"
[ "$(printf '%s' "${acct}" | json_field migrated)" = "True" ] && log "  @${MANAGER_LP}: migrated to derived password (existing sessions kept)"

# Per-teammate: account+token via provision-account (derived password, legacy
# migrated + its PW_ line dropped from the state file), then the space. bash
# 3.2 has no associative arrays, so dynamic var names go through eval (values
# are our own tokens/room ids, never external input).
for t in "${TEAMMATES[@]}"; do
  case "${t}" in
    manager) fail "roster entry 'manager' is reserved" ;;
    *[!a-z0-9]*|'') fail "invalid roster entry '${t}' (use lowercase letters and digits)" ;;
  esac
  U=$(upper "$t")
  log "provisioning @${t}:${SERVER}"
  acct=$(provision_account "$t") || fail "provisioning @${t} failed — see enroll.py stderr above"
  tok=$(printf '%s' "${acct}" | json_field token) || fail "@${t}: no token in provision-account output"
  eval "oldtok=\${MASTER_${U}_TOKEN:-}"
  if token_valid "${oldtok}" "@${t}:${SERVER}"; then tok="${oldtok}"; fi
  [ -n "${tok}" ] || fail "@${t}: empty token"
  [ "$(printf '%s' "${acct}" | json_field migrated)" = "True" ] && log "  @${t}: migrated to derived password (existing sessions kept)"
  eval "TOKEN_${U}=\${tok}"
  eval "sp=\${SPACE_${U}:-}"
  if space_valid "${tok}" "${sp}"; then
    log "space:${t} already exists (${sp}) — skipping"
  else
    sp=$(create_space "$t" "${tok}"); log "created space:${t} = ${sp}"
    eval "SPACE_${U}=\${sp}"
  fi
done

# --- persist state (600): roster + space ids. NO SECRETS: passwords are
# derived from TEAMMATE_PASSWORD_KEY, never written anywhere. ---
{
  echo "# matrix-master provisioning state (mode 600, gitignored). Do NOT commit."
  echo "TEAMMATES='${TEAMMATES[*]}'"
  for t in "${TEAMMATES[@]}"; do
    U=$(upper "$t"); eval "sp=\${SPACE_${U}:-}"
    [ -n "${sp}" ] && echo "SPACE_${U}='${sp}'"
  done
} > "${STATE_FILE}"
chmod 600 "${STATE_FILE}"

# --- write sourceable tokens file (600) ---
{
  echo "# matrix-master access tokens — mode 600, gitignored. Do NOT commit."
  echo "# Regenerate any time with: master/provision.sh"
  echo "MASTER_CS_BASE='${CS_BASE}'"
  echo "MASTER_MANAGER_USER='@${MANAGER_LP}:${SERVER}'"
  echo "MASTER_MANAGER_TOKEN='${MANAGER_TOKEN}'"
  echo "MASTER_TEAMMATES='${TEAMMATES[*]}'"
  for t in "${TEAMMATES[@]}"; do
    U=$(upper "$t"); eval "tok=\${TOKEN_${U}}"; eval "sp=\${SPACE_${U}:-}"
    echo "MASTER_${U}_USER='@${t}:${SERVER}'"
    echo "MASTER_${U}_TOKEN='${tok}'"
    echo "MASTER_SPACE_${U}='${sp}'"
  done
  # Convenience aliases for the FIRST teammate (the default single-user slot),
  # so callers can source stable MASTER_TEAMMATE_* without knowing the name.
  if [ -n "${TEAMMATES[*]:-}" ]; then
    FT=$(upper "${TEAMMATES[0]}"); eval "ftok=\${TOKEN_${FT}}"; eval "fsp=\${SPACE_${FT}:-}"
    echo "MASTER_TEAMMATE_USER='@${TEAMMATES[0]}:${SERVER}'"
    echo "MASTER_TEAMMATE_TOKEN='${ftok}'"
    echo "MASTER_SPACE_TEAMMATE='${fsp}'"
  fi
  # PRESERVE keys other scripts record into tokens.local: this rewrite used to
  # silently drop MASTER_PUBLIC_URL/ENROLL_PUBLIC_URL (written by
  # tailscale-serve.sh), so any later provisioning run — e.g. adding a
  # teammate — broke enrollment handout back to the master's loopback URL.
  # enroll.py's _advertised_hs_url() now refuses that state loudly; this keeps
  # it from arising at all.
  if [ -f "${TOKENS_FILE}" ]; then
    grep -E "^(MASTER_PUBLIC_URL|ENROLL_PUBLIC_URL)='" "${TOKENS_FILE}" || true
  fi
} > "${TOKENS_FILE}.new"
mv -f "${TOKENS_FILE}.new" "${TOKENS_FILE}"
chmod 600 "${TOKENS_FILE}"
log "wrote ${TOKENS_FILE} (mode 600)"
log "done. teammates: ${TEAMMATES[*]}"
