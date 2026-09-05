#!/usr/bin/env bash
# master/setup.sh — regenerate the (gitignored) master Synapse config from
# TRACKED, non-secret settings so a fresh master stack is reproducible.
#
# The secret-bearing homeserver.yaml is deliberately NOT tracked (it embeds the
# signing key path + registration_shared_secret + DB password). This script is
# the tracked source of every NON-secret setting — crucially the uplink-burst
# rate limits (rc_message / rc_invites / rc_room_creation) — and it MINTS the
# secrets locally, writing them only to gitignored 0600 files:
#
#   master/synapse/homeserver.yaml     rendered here (0600, gitignored)
#   master/synapse/master.signing.key  minted here if absent (0600, gitignored)
#   master/synapse/master.log.config   written here if absent (gitignored)
#   master/synapse/.secrets.local      the 3 shared secrets (0600, gitignored)
#   master/synapse/media_store/        created here, host-owned by UID 501 so the
#                                      Synapse container (UID 501) can write media
#
# The DB password is read from master/.env (MASTER_POSTGRES_PASSWORD) so Synapse
# and Postgres agree. Re-running is safe: existing secrets + signing key are
# reused, so an existing DB keeps working; only homeserver.yaml is re-rendered.
#
# Usage:
#   master/setup.sh                       # regenerate config
#   docker compose -p matrix-master --env-file master/.env \
#     -f master/docker-compose.master.yml up -d
#   master/provision.sh                   # create accounts + spaces
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"

STATE_DIR="${BEEPA_MASTER_STATE_DIR:-${BEEPA_INSTALL_ROOT:-$(cd "${HERE}/.." && pwd)}/master}"
SYN_DIR="${STATE_DIR}/synapse"
HS_YAML="${SYN_DIR}/homeserver.yaml"
SIGNING_KEY="${SYN_DIR}/master.signing.key"
LOG_CONFIG="${SYN_DIR}/master.log.config"
SECRETS="${SYN_DIR}/.secrets.local"
MEDIA_DIR="${SYN_DIR}/media_store"
ENV_FILE="${STATE_DIR}/.env"
IMAGE='ghcr.io/element-hq/synapse:v1.159.0@sha256:edf259d2b575b669a3e81024918ab8d5cfb7d2fba5a53c9e09695f1abc5645cb'

log() { printf '[setup] %s\n' "$*" >&2; }
fail() { printf '[setup] ERROR: %s\n' "$*" >&2; exit 1; }

umask 077
mkdir -p "${SYN_DIR}" "${MEDIA_DIR}"

# --- DB password from .env (must match the Postgres container) ---
[ -f "${ENV_FILE}" ] || fail "missing ${ENV_FILE} (set MASTER_POSTGRES_PASSWORD)"
# shellcheck disable=SC1090
DB_PASSWORD="$(grep -E '^MASTER_POSTGRES_PASSWORD=' "${ENV_FILE}" | head -1 | cut -d= -f2- || true)"
[ -n "${DB_PASSWORD}" ] || fail "MASTER_POSTGRES_PASSWORD not set in ${ENV_FILE}"

gen_secret() { python3 -c 'import secrets;print(secrets.token_urlsafe(48))'; }

# Updates must never turn missing authority state into a new master identity.
if [ "${BEEPA_UPDATE:-0}" = 1 ]; then
  [ -s "${SECRETS}" ] && [ -s "${SIGNING_KEY}" ] || fail "master authority files missing; use reviewed recovery, not update-time key generation"
  unset TEAMMATE_PASSWORD_KEY TEAMMATE_PASSWORD_KEY_PREV
fi

# --- shared secrets: reuse if present, else mint + persist (0600) ---
# TEAMMATE_PASSWORD_KEY is the ONLY root of every master-side account password
# (teammates + manager): master/enroll.py derives each password from it with
# HMAC-SHA256, so no password is ever stored (see master/CLAUDE.md). This
# script is the single writer of this file; enroll.py/provision.sh only read
# it and fail loudly if the key is absent.
#
# Precedence for the two password keys (unlike the three Synapse secrets,
# which are file-wins): an explicitly-set env value overrides the file value;
# an explicitly-set EMPTY TEAMMATE_PASSWORD_KEY_PREV means "omit the _PREV
# line" (end of a rotation). `source` below clobbers the env variables, so
# both value and set-ness are captured first.
ENV_TEAMMATE_PASSWORD_KEY="${TEAMMATE_PASSWORD_KEY-}"
ENV_TEAMMATE_PASSWORD_KEY_PREV="${TEAMMATE_PASSWORD_KEY_PREV-}"
ENV_PREV_SET="${TEAMMATE_PASSWORD_KEY_PREV+set}"
TEAMMATE_PASSWORD_KEY=""
TEAMMATE_PASSWORD_KEY_PREV=""
if [ -s "${SECRETS}" ]; then
  # shellcheck disable=SC1090
  source "${SECRETS}"
  log "reusing existing shared secrets"
fi
MACAROON_SECRET="${MACAROON_SECRET:-$(gen_secret)}"
FORM_SECRET="${FORM_SECRET:-$(gen_secret)}"
REGISTRATION_SHARED_SECRET="${REGISTRATION_SHARED_SECRET:-$(gen_secret)}"
# current key: non-empty env -> env; else file; else mint. Never emitted empty.
if [ -n "${ENV_TEAMMATE_PASSWORD_KEY}" ]; then
  TEAMMATE_PASSWORD_KEY="${ENV_TEAMMATE_PASSWORD_KEY}"
fi
TEAMMATE_PASSWORD_KEY="${TEAMMATE_PASSWORD_KEY:-$(gen_secret)}"
# previous key: set+non-empty env -> env; set+empty -> omit; unset -> file (if any)
if [ -n "${ENV_PREV_SET}" ]; then
  TEAMMATE_PASSWORD_KEY_PREV="${ENV_TEAMMATE_PASSWORD_KEY_PREV}"
fi
{
  echo "# matrix-master Synapse shared secrets (mode 600, gitignored). Do NOT commit."
  echo "MACAROON_SECRET='${MACAROON_SECRET}'"
  echo "FORM_SECRET='${FORM_SECRET}'"
  echo "REGISTRATION_SHARED_SECRET='${REGISTRATION_SHARED_SECRET}'"
  echo "TEAMMATE_PASSWORD_KEY='${TEAMMATE_PASSWORD_KEY}'"
  if [ -n "${TEAMMATE_PASSWORD_KEY_PREV}" ]; then
    echo "TEAMMATE_PASSWORD_KEY_PREV='${TEAMMATE_PASSWORD_KEY_PREV}'"
  fi
} > "${SECRETS}"
chmod 600 "${SECRETS}"

# --- signing key: mint once via the Synapse image if absent ---
if [ ! -s "${SIGNING_KEY}" ]; then
  log "minting signing key ..."
  docker run --rm --entrypoint generate_signing_key "${IMAGE}" -o /dev/stdout \
    > "${SIGNING_KEY}" 2>/dev/null || fail "generate_signing_key failed"
  chmod 600 "${SIGNING_KEY}"
  log "wrote ${SIGNING_KEY}"
fi

# --- log config: static, write if absent ---
if [ ! -s "${LOG_CONFIG}" ]; then
  cat > "${LOG_CONFIG}" <<'LOGCFG'
version: 1
formatters:
  precise:
    format: '%(asctime)s - %(name)s - %(lineno)d - %(levelname)s - %(request)s - %(message)s'
handlers:
  console:
    class: logging.StreamHandler
    formatter: precise
loggers:
    _placeholder:
        level: "INFO"
    synapse.storage.SQL:
        level: INFO
root:
    level: INFO
    handlers: [console]
disable_existing_loggers: false
LOGCFG
  log "wrote ${LOG_CONFIG}"
fi

# Render incoming defaults separately; preserve operator configuration through
# the same recoverable merge used by teammate installations.
CONFIG_STAGE="$(mktemp -d)"
trap 'rm -rf "${CONFIG_STAGE}"' EXIT
mkdir -p "${CONFIG_STAGE}/synapse"
HS_YAML="${CONFIG_STAGE}/synapse/homeserver.yaml"
cat > "${HS_YAML}" <<YAML
# GENERATED by master/setup.sh — do NOT edit by hand, do NOT commit.
# All NON-secret settings below are the tracked source of truth in setup.sh.
# Secrets (signing key, macaroon/form/registration) are minted locally.
#
# MASTER Synapse (server_name: master). CS API only, no federation, no public
# registration. Isolated from the live matrix-wa stack.
server_name: "master"
pid_file: /data/homeserver.pid
listeners:
  - port: 8008
    tls: false
    type: http
    x_forwarded: true
    resources:
      - names: [client]
        compress: false
database:
  name: psycopg2
  args:
    user: matrix
    password: "${DB_PASSWORD}"
    dbname: synapse
    host: postgres
    port: 5432
    cp_min: 5
    cp_max: 10
log_config: "/data/master.log.config"
media_store_path: /data/media_store
report_stats: false
macaroon_secret_key: "${MACAROON_SECRET}"
form_secret: "${FORM_SECRET}"
signing_key_path: "/data/master.signing.key"
trusted_key_servers: []

# No public registration. Provisioning uses the shared secret below with
# register_new_matrix_user against the local CS API (see master/provision.sh).
enable_registration: false
registration_shared_secret: "${REGISTRATION_SHARED_SECRET}"

# Federation stays off.
federation_domain_whitelist: []

rc_login:
  address:
    per_second: 0.15
    burst_count: 10
  account:
    per_second: 0.15
    burst_count: 10
  failed_attempts:
    per_second: 0.15
    burst_count: 10

rc_joins:
  local:
    per_second: 20
    burst_count: 300

# A teammate's first share creates many mirror rooms at once, and each
# createRoom emits several events. The default rc_message burst of 10 would 429
# the uplink partway through a burst of room creations / backfills. Loosen the
# message + invite limiters so the master can accept a real uplink's reconcile
# burst. (Master stack only — unrelated to the live matrix-wa hub.)
rc_message:
  per_second: 100
  burst_count: 1000
# createRoom is gated by its OWN limiter (rc_room_creation), NOT rc_message — the
# default (burst ~10, slow refill) 429s the uplink when a reconcile creates
# several mirror rooms plus the per-teammate proposals room in one window.
rc_room_creation:
  per_second: 10
  burst_count: 200
rc_invites:
  per_room:
    per_second: 100
    burst_count: 1000
  per_user:
    per_second: 100
    burst_count: 1000

# The master console (apps/master) refreshes with a full initial /sync every
# 20s BY DESIGN; Synapse's initial-sync response cache (default 2m) would feed
# it minutes-stale snapshots (stale room joins, missed proposals rooms/mirrors
# — found live 2026-08-29). Single-user server: fresh computation is cheap.
caches:
  sync_response_cache_duration: 0

# vim:ft=yaml
YAML
chmod 600 "${HS_YAML}"
python3 "${HERE}/../hub/managed_config.py" "${STATE_DIR}" "${CONFIG_STAGE}"
log "preserved/updated ${SYN_DIR}/homeserver.yaml (mode 600) with upstream defaults"
log "done. Next: bring the stack up, then run master/provision.sh"
