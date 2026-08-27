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
# TEAMMATES env var (space-separated usernames), default "jkali". The
# integration harness, which needs two isolated teammates for the cross-user
# isolation scenario, runs this with TEAMMATES="alice bob".
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
TOKENS_FILE="${HERE}/tokens.local"
STATE_FILE="${HERE}/.provision-state.local"
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

# Roster resolution: explicit env override wins; else the persisted roster the
# last provision / console-add wrote (sourced just above as a scalar TEAMMATES);
# else the single real user "jkali".
read -r -a TEAMMATES <<< "${ENV_TEAMMATES:-${TEAMMATES:-jkali}}"

gen_pw() { python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(32)))'; }

# The manager is the human-facing master login; default it to the simple
# 'password' (overridable via env). Teammate slots (the uplink authenticates as
# them with a token, no human login) get random passwords, persisted so re-runs
# don't reset them.
MANAGER_PW="${MANAGER_PW:-password}"

# --- wait for Synapse health ---
log "waiting for Synapse CS API at ${CS_BASE} ..."
for i in $(seq 1 60); do
  if curl -fsS "${CS_BASE}/health" >/dev/null 2>&1; then log "Synapse healthy"; break; fi
  [ "$i" -eq 60 ] && fail "Synapse did not become healthy at ${CS_BASE}"
  sleep 2
done

# --- create an account (idempotent) via register_new_matrix_user ---
register() {
  local user="$1" pass="$2" out
  log "registering @${user}:${SERVER} (skip if exists)"
  if out=$("${COMPOSE[@]}" exec -T synapse \
        register_new_matrix_user -c "${HS_YAML}" \
        -u "${user}" -p "${pass}" --no-admin http://localhost:8008 2>&1); then
    log "  created @${user}:${SERVER}"
  else
    if printf '%s' "${out}" | grep -qiE 'already taken|already exists'; then
      log "  @${user}:${SERVER} already exists — ok"
    else
      fail "register @${user} failed: ${out}"
    fi
  fi
}

# --- password login for an access token via the CS API ---
login_token() {
  local user="$1" pass="$2" resp token
  resp=$(curl -fsS -XPOST "${CS_BASE}/_matrix/client/v3/login" \
    -H 'Content-Type: application/json' \
    -d "{\"type\":\"m.login.password\",\"identifier\":{\"type\":\"m.id.user\",\"user\":\"${user}\"},\"password\":\"${pass}\",\"initial_device_display_name\":\"master-provision\"}") \
    || fail "login @${user} failed"
  token=$(printf '%s' "${resp}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])') \
    || fail "no access_token for @${user}"
  printf '%s' "${token}"
}

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
register "${MANAGER_LP}" "${MANAGER_PW}"
MANAGER_TOKEN=$(login_token "${MANAGER_LP}" "${MANAGER_PW}")

# Per-teammate: password (persisted), account, token, space. bash 3.2 has no
# associative arrays, so dynamic var names go through eval (values are our own
# generated tokens/ids, never external input).
for t in "${TEAMMATES[@]}"; do
  U=$(upper "$t")
  eval "pw=\${PW_${U}:-}"
  [ -z "${pw}" ] && { pw=$(gen_pw); eval "PW_${U}=\${pw}"; }
  register "$t" "${pw}"
  tok=$(login_token "$t" "${pw}")
  eval "TOKEN_${U}=\${tok}"
  eval "sp=\${SPACE_${U}:-}"
  if space_valid "${tok}" "${sp}"; then
    log "space:${t} already exists (${sp}) — skipping"
  else
    sp=$(create_space "$t" "${tok}"); log "created space:${t} = ${sp}"
    eval "SPACE_${U}=\${sp}"
  fi
done

# --- persist state (600): manager pw + each teammate pw + space id ---
{
  echo "# matrix-master provisioning state (mode 600, gitignored). Do NOT commit."
  echo "MANAGER_PW='${MANAGER_PW}'"
  echo "TEAMMATES='${TEAMMATES[*]}'"
  for t in "${TEAMMATES[@]}"; do
    U=$(upper "$t"); eval "pw=\${PW_${U}}"; eval "sp=\${SPACE_${U}:-}"
    echo "PW_${U}='${pw}'"
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
  FT=$(upper "${TEAMMATES[0]}"); eval "ftok=\${TOKEN_${FT}}"; eval "fsp=\${SPACE_${FT}:-}"
  echo "MASTER_TEAMMATE_USER='@${TEAMMATES[0]}:${SERVER}'"
  echo "MASTER_TEAMMATE_TOKEN='${ftok}'"
  echo "MASTER_SPACE_TEAMMATE='${fsp}'"
} > "${TOKENS_FILE}"
chmod 600 "${TOKENS_FILE}"
log "wrote ${TOKENS_FILE} (mode 600)"
log "done. teammates: ${TEAMMATES[*]}"
