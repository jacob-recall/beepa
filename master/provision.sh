#!/usr/bin/env bash
# master/provision.sh — idempotent provisioning of the matrix-master homeserver.
#
# Creates @manager:master, @alice:master, @bob:master (server-side, via the
# registration shared secret); logs each in for an access token; creates one
# Matrix SPACE per teammate owned by that teammate with @manager invited at a
# read-only power level (events_default 50, teammate PL 100, manager PL 0).
#
# Re-running is safe: existing accounts are skipped, tokens are refreshed, and
# spaces already recorded (and still joined) are not recreated.
#
# Outputs:
#   master/tokens.local   access tokens as shell-sourceable vars (mode 600)
#   master/.provision-state.local  recorded space room ids (mode 600)
#
# Prereqs: the stack is up and Synapse healthy. See docker-compose.master.yml.
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
declare -a TEAMMATES=("alice" "bob")

umask 077
touch "${STATE_FILE}"; chmod 600 "${STATE_FILE}"

log() { printf '[provision] %s\n' "$*" >&2; }
fail() { printf '[provision] ERROR: %s\n' "$*" >&2; exit 1; }

# --- passwords: read from env if present, else generate + persist to state ---
# We persist generated account passwords in the (600) state file so re-runs can
# log in again without resetting them.
# shellcheck disable=SC1090
[ -s "${STATE_FILE}" ] && source "${STATE_FILE}" || true

gen_pw() { python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(32)))'; }

persist_state() {
  # rewrite the state file from current shell vars
  {
    echo "# matrix-master provisioning state (mode 600, gitignored). Do NOT commit."
    echo "MANAGER_PW='${MANAGER_PW}'"
    echo "ALICE_PW='${ALICE_PW}'"
    echo "BOB_PW='${BOB_PW}'"
    [ -n "${SPACE_ALICE:-}" ] && echo "SPACE_ALICE='${SPACE_ALICE}'"
    [ -n "${SPACE_BOB:-}"   ] && echo "SPACE_BOB='${SPACE_BOB}'"
  } > "${STATE_FILE}"
  chmod 600 "${STATE_FILE}"
}

# The manager is the human-facing master login; default it to the simple
# 'password' (overridable via env) so the operator can sign in easily. The
# alice/bob teammate slots stay randomly generated.
MANAGER_PW="${MANAGER_PW:-password}"
ALICE_PW="${ALICE_PW:-$(gen_pw)}"
BOB_PW="${BOB_PW:-$(gen_pw)}"

pw_for() { case "$1" in manager) echo "${MANAGER_PW}";; alice) echo "${ALICE_PW}";; bob) echo "${BOB_PW}";; esac; }

# --- wait for Synapse health ---
log "waiting for Synapse CS API at ${CS_BASE} ..."
for i in $(seq 1 60); do
  if curl -fsS "${CS_BASE}/health" >/dev/null 2>&1; then log "Synapse healthy"; break; fi
  [ "$i" -eq 60 ] && fail "Synapse did not become healthy at ${CS_BASE}"
  sleep 2
done

# --- create an account (idempotent) via register_new_matrix_user ---
register() {
  local user="$1" pass="$2"
  log "registering @${user}:${SERVER} (skip if exists)"
  local out
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

# --- does a room still exist and have the teammate joined? (idempotency check) ---
space_valid() {
  local token="$1" room="$2" code
  [ -z "${room}" ] && return 1
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${token}" \
    "${CS_BASE}/_matrix/client/v3/rooms/${room}/state/m.room.create/") || return 1
  [ "${code}" = "200" ]
}

# --- create a teammate space owned by the teammate, manager invited read-only ---
create_space() {
  local user="$1" token="$2" resp room
  resp=$(curl -fsS -XPOST "${CS_BASE}/_matrix/client/v3/createRoom" \
    -H "Authorization: Bearer ${token}" \
    -H 'Content-Type: application/json' \
    -d "$(cat <<JSON
{
  "name": "space:${user}",
  "topic": "Read-only master space for @${user}:${SERVER}",
  "preset": "private_chat",
  "creation_content": { "type": "m.space" },
  "invite": ["@${MANAGER_LP}:${SERVER}"],
  "power_level_content_override": {
    "events_default": 50,
    "state_default": 50,
    "invite": 50,
    "kick": 50,
    "ban": 50,
    "redact": 50,
    "users_default": 0,
    "users": {
      "@${user}:${SERVER}": 100,
      "@${MANAGER_LP}:${SERVER}": 0
    }
  }
}
JSON
)") || fail "createRoom (space:${user}) failed"
  room=$(printf '%s' "${resp}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["room_id"])') \
    || fail "no room_id for space:${user}"
  printf '%s' "${room}"
}

# =========================== run ===========================
# bash 3.2 (macOS default) has no associative arrays — use plain vars.
register "${MANAGER_LP}" "${MANAGER_PW}"
MANAGER_TOKEN=$(login_token "${MANAGER_LP}" "${MANAGER_PW}")

register "alice" "${ALICE_PW}"
ALICE_TOKEN=$(login_token "alice" "${ALICE_PW}")
register "bob" "${BOB_PW}"
BOB_TOKEN=$(login_token "bob" "${BOB_PW}")
persist_state   # save passwords now that accounts exist

# spaces (idempotent: reuse a recorded room the teammate is still in)
if space_valid "${ALICE_TOKEN}" "${SPACE_ALICE:-}"; then
  log "space:alice already exists (${SPACE_ALICE}) — skipping"
else
  SPACE_ALICE=$(create_space "alice" "${ALICE_TOKEN}"); log "created space:alice = ${SPACE_ALICE}"
fi
if space_valid "${BOB_TOKEN}" "${SPACE_BOB:-}"; then
  log "space:bob already exists (${SPACE_BOB}) — skipping"
else
  SPACE_BOB=$(create_space "bob" "${BOB_TOKEN}"); log "created space:bob = ${SPACE_BOB}"
fi
persist_state   # save space ids

# --- write sourceable tokens file (600) ---
{
  echo "# matrix-master access tokens — mode 600, gitignored. Do NOT commit."
  echo "# Regenerate any time with: master/provision.sh"
  echo "MASTER_CS_BASE='${CS_BASE}'"
  echo "MASTER_MANAGER_USER='@${MANAGER_LP}:${SERVER}'"
  echo "MASTER_MANAGER_TOKEN='${MANAGER_TOKEN}'"
  echo "MASTER_ALICE_USER='@alice:${SERVER}'"
  echo "MASTER_ALICE_TOKEN='${ALICE_TOKEN}'"
  echo "MASTER_BOB_USER='@bob:${SERVER}'"
  echo "MASTER_BOB_TOKEN='${BOB_TOKEN}'"
  echo "MASTER_SPACE_ALICE='${SPACE_ALICE:-}'"
  echo "MASTER_SPACE_BOB='${SPACE_BOB:-}'"
} > "${TOKENS_FILE}"
chmod 600 "${TOKENS_FILE}"
log "wrote ${TOKENS_FILE} (mode 600)"

log "done."
