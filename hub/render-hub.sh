#!/usr/bin/env bash
# hub/render-hub.sh — INSTALL-TIME. Renders the tracked templates in
# hub/templates/ into a complete, working hub config (synapse/ + each bridge
# dir), minting any missing secrets. Idempotent: existing secrets in
# synapse/.hub-secrets.local are reused (so re-running never rotates tokens out
# from under a live stack), and only missing ones are minted. Called by setup.sh
# before `docker compose up`. No secret is ever printed.
#
# Faithfulness: rendering with the current secrets reproduces the current config
# byte-for-byte — that is what `hub/render-hub.sh --verify` checks against the
# working tree.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TPL="${HERE}/hub/templates"
STATE_ROOT="${BEEPA_INSTALL_ROOT:-${HERE}}"
SECRETS="${STATE_ROOT}/synapse/.hub-secrets.local"
ENV_FILE="${STATE_ROOT}/.env"
SYN_IMAGE='ghcr.io/element-hq/synapse:v1.159.0@sha256:edf259d2b575b669a3e81024918ab8d5cfb7d2fba5a53c9e09695f1abc5645cb'
OUT_ROOT="${OUT_ROOT:-${STATE_ROOT}}"    # overridable for --verify into a scratch dir
VERIFY=0; [ "${1:-}" = "--verify" ] && VERIFY=1
log() { printf '[render-hub] %s\n' "$*" >&2; }

mint() { if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32; \
         else head -c 48 /dev/urandom | LC_ALL=C tr -dc 'a-f0-9' | head -c 64; fi; }

# --- placeholders the templates ACTUALLY reference (derived, not hardcoded) --
# Scanning the templates keeps this correct as bridges/sentinels change (e.g.
# whatsapp's shared_secret stays the literal 'disable', so no PROV_WHATSAPP).
# Only our own known placeholder names are matched, so mautrix's ${...} config
# refs are never picked up. DB_PASSWORD comes from .env and is excluded here.
KNOWN='DB_PASSWORD|MACAROON_SECRET|FORM_SECRET|AS_TOKEN_[A-Z]+|HS_TOKEN_[A-Z]+|PROV_[A-Z]+|PICKLE_[A-Z]+'
VARS="$(grep -rhoE '\$\{('"${KNOWN}"')\}' "${TPL}" \
        | sed -E 's/^\$\{(.*)\}$/\1/' | sort -u | grep -v '^DB_PASSWORD$' | tr '\n' ' ')"

# --- DB password comes from .env (single source of truth) -----------------
[ -f "${ENV_FILE}" ] || { log "FATAL: ${ENV_FILE} missing (setup.sh mints it first)"; exit 1; }
# shellcheck disable=SC1090
DB_PASSWORD="$(grep -E '^POSTGRES_PASSWORD=' "${ENV_FILE}" | head -1 | cut -d= -f2- || true)"
[ -n "${DB_PASSWORD}" ] || { log "FATAL: POSTGRES_PASSWORD not set in .env"; exit 1; }
export DB_PASSWORD

# Resolve the same installed identity used by provisioning and login helpers.
LOCAL_MXID="$(python3 "${HERE}/install_config.py" --root "${STATE_ROOT}" identity)"
export LOCAL_MXID

# --- load existing secrets, mint any missing (never in --verify) ----------
# On a fresh clone synapse/ does not exist yet (the render loop makes it later),
# so ensure the secrets dir exists before we read or write .hub-secrets.local.
mkdir -p "$(dirname "${SECRETS}")"
[ -f "${SECRETS}" ] && { set -a; . "${SECRETS}"; set +a; }
missing=""
for v in ${VARS}; do
  if [ -z "${!v:-}" ]; then
    if [ "${VERIFY}" = 1 ]; then
      log "FATAL(--verify): ${v} not in ${SECRETS}; run hub/make-templates.sh first"; exit 1
    fi
    printf -v "$v" '%s' "$(mint)"; export "${v?}"; missing="${missing} ${v}"
  else
    export "${v?}"
  fi
done
if [ -n "${missing}" ] && [ "${VERIFY}" != 1 ]; then
  ( umask 077
    [ -f "${SECRETS}" ] || printf '# Rendered-hub secrets — 600, gitignored, DO NOT COMMIT.\n' > "${SECRETS}"
    for v in ${missing}; do printf "%s='%s'\n" "$v" "${!v}" >> "${SECRETS}"; done )
  chmod 600 "${SECRETS}"
  log "minted$(printf ' %s' ${missing}) into synapse/.hub-secrets.local"
fi

# --- render every template with an explicit var allowlist -----------------
# A tiny python substituter (portable; macOS has no envsubst) replaces only our
# named placeholders, leaving mautrix's own ${...} config refs untouched.
RENDER_ROOT="${OUT_ROOT}"
if [ "${VERIFY}" != 1 ]; then
  RENDER_ROOT="$(mktemp -d)"
  trap 'rm -rf "${RENDER_ROOT}"' EXIT
fi
count=0
while IFS= read -r tmpl; do
  rel="${tmpl#"${TPL}/"}"; dest="${RENDER_ROOT}/${rel%.tmpl}"
  mkdir -p "$(dirname "${dest}")"
  # shellcheck disable=SC2086
  python3 "${HERE}/hub/_render_subst.py" "${tmpl}" "${dest}" DB_PASSWORD LOCAL_MXID ${VARS}
  chmod 600 "${dest}"
  count=$((count+1))
done < <(find "${TPL}" -name '*.tmpl' | sort)
log "rendered ${count} config files for validation"

[ "${VERIFY}" = 1 ] && { log "verify render complete (diff is the caller's job)"; exit 0; }

# Keep registration credentials in the same managed render transaction.
if [ -z "${REG_SHARED_SECRET:-}" ]; then
  REG_SHARED_SECRET="$(mint)"
  ( umask 077; printf "REG_SHARED_SECRET='%s'\n" "${REG_SHARED_SECRET}" >> "${SECRETS}" )
  chmod 600 "${SECRETS}"
fi
printf '\nregistration_shared_secret: "%s"\n' "${REG_SHARED_SECRET}" >> "${RENDER_ROOT}/synapse/homeserver.yaml"
python3 "${HERE}/hub/managed_config.py" "${OUT_ROOT}" "${RENDER_ROOT}"

# --- runtime files Synapse needs beyond the YAMLs -------------------------
mkdir -p "${OUT_ROOT}/synapse/media_store"
LOGCFG="${OUT_ROOT}/synapse/localhost.log.config"
if [ ! -f "${LOGCFG}" ]; then
  cat > "${LOGCFG}" <<'LOGEOF'
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
root:
  level: INFO
  handlers: [console]
disable_existing_loggers: false
LOGEOF
  log "wrote synapse/localhost.log.config"
fi

# Signing key: generate once; never regenerate a good one.
#   (a) `[ ! -s ]` (empty OR missing) self-heals a 0-byte key left by an
#       interrupted run — `[ ! -f ]` treated an empty file as present and
#       skipped forever, booting Synapse with an invalid key.
#   (b) Generate into a temp and `mv` on success — `> "${SIGN}"` creates the
#       file at redirect time, so a Ctrl-C mid-pull left a 0-byte key at the
#       real path.
#   (c) Pull the image as its OWN visible step, and DON'T silence the generate.
#       Folded into `docker run … 2>/dev/null`, the ~500MB first-run pull is a
#       silent multi-minute download that reads as a hang.
# Plus a pure-python ed25519 fallback so a missing/emulation-broken Docker (an
# amd64 image on arm64 without Rosetta can exit 0 yet write nothing) never blocks.
SIGN="${OUT_ROOT}/synapse/localhost.signing.key"
if [ ! -s "${SIGN}" ]; then
  rm -f "${SIGN}"
  tmp="$(mktemp)"
  if command -v docker >/dev/null 2>&1; then
    log "ensuring the Synapse image is present (first run downloads ~500MB — this is not a hang)…"
    docker pull "${SYN_IMAGE}" || log "WARN: image pull failed; the generate below may retry it"
    docker run --rm --entrypoint generate_signing_key "${SYN_IMAGE}" -o /dev/stdout > "${tmp}" || true
  fi
  if [ ! -s "${tmp}" ]; then
    python3 "${HERE}/hub/_gen_signing_key.py" > "${tmp}" 2>/dev/null || true   # docker missing/broken
  fi
  if [ -s "${tmp}" ]; then
    mv "${tmp}" "${SIGN}"; chmod 600 "${SIGN}"
    log "generated synapse/localhost.signing.key"
  else
    rm -f "${tmp}"
    log "WARN: could not generate a signing key — Synapse will not boot until one exists."
    log "  Fix: docker run --rm --entrypoint generate_signing_key ${SYN_IMAGE} -o /dev/stdout > synapse/localhost.signing.key"
  fi
fi

# iMessage daemon config: render its as_token/hs_token from the SAME secrets as
# synapse/imessage-registration.yaml so a fresh install's daemon and appservice
# agree (they diverged before this). Only when absent — never clobber a hand-
# filled one. self_handle + the vendored bin/imessage-cli stay manual (macOS #3).
IMSG_TMPL="${HERE}/hub/imessage-daemon.json.tmpl"
IMSG_DEST="${OUT_ROOT}/imessage/daemon.json"
if [ -f "${IMSG_TMPL}" ] && [ ! -f "${IMSG_DEST}" ]; then
  mkdir -p "$(dirname "${IMSG_DEST}")"
  CLI_PATH="${OUT_ROOT}/imessage/bin/imessage-cli"; export CLI_PATH
  python3 "${HERE}/hub/_render_subst.py" "${IMSG_TMPL}" "${IMSG_DEST}" \
    AS_TOKEN_IMESSAGE HS_TOKEN_IMESSAGE CLI_PATH LOCAL_MXID
  chmod 600 "${IMSG_DEST}"
  log "wrote imessage/daemon.json (tokens match imessage-registration.yaml; still fill self_handle + provide bin/imessage-cli)"
fi

log "done — hub config rendered under ${OUT_ROOT}"
