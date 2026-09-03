#!/usr/bin/env bash
# hub/provision-user.sh — create this hub's local Matrix account and mint the
# uplink's LOCAL_TOKEN. Runs AFTER the stack is up (needs Synapse reachable and
# registration_shared_secret present, which hub/render-hub.sh added). Idempotent:
# reuses the stored password + existing account; safe to re-run.
#
# The localpart stays a fixed local label (default 'jkali') because the bridge
# configs grant permissions to exactly that mxid; it is never seen off-machine
# (the teammate's visible identity is their separate @<name>:master). Override
# with LOCAL_LOCALPART only alongside the template change that reparameterizes
# the bridge permissions (see pm_mng-9qg follow-up).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
log()  { printf '[provision-user] %s\n' "$*" >&2; }
fail() { printf '[provision-user] ERROR: %s\n' "$*" >&2; exit 1; }

HS="${LOCAL_HS_URL:-http://127.0.0.1:8008}"
LP="${LOCAL_LOCALPART:-jkali}"
CREDS="${HUB_USER_CREDS:-${HERE}/hub/.local-user.local}"       # stored password (600)
OUT="${HUB_UPLINK_LOCAL_ENV:-${HERE}/agents/uplink/local.env.local}"  # LOCAL_* for link.sh (600)

# reuse a previously-stored password, else mint and persist one
[ -f "${CREDS}" ] && { set -a; . "${CREDS}"; set +a; }
if [ -z "${LOCAL_PASSWORD:-}" ]; then
  LOCAL_PASSWORD="$(openssl rand -base64 18 2>/dev/null || head -c 18 /dev/urandom | base64 | tr -d '\n')"
  ( umask 077; { printf "LOCAL_LOCALPART='%s'\n" "${LP}"; printf "LOCAL_PASSWORD='%s'\n" "${LOCAL_PASSWORD}"; } > "${CREDS}" )
  chmod 600 "${CREDS}"
fi

# wait for Synapse to answer (bounded)
up=0
for _ in $(seq 1 30); do curl -fsS "${HS}/health" >/dev/null 2>&1 && { up=1; break; }; sleep 2; done
[ "${up}" = 1 ] || fail "Synapse not reachable at ${HS} after ~60s"

# register (idempotent — 'User ID already taken' is fine)
created=0
if command -v docker >/dev/null 2>&1; then
  reg="$( ( cd "${HERE}" && docker compose exec -T synapse \
      register_new_matrix_user -c /data/homeserver.yaml --no-admin \
      -u "${LP}" -p "${LOCAL_PASSWORD}" http://localhost:8008 ) 2>&1 || true )"
  case "${reg}" in
    *"already taken"*) log "account @${LP}:localhost already exists — reusing" ;;
    *Success*)         log "registered @${LP}:localhost"; created=1 ;;
    *"registration_shared_secret"*)
      fail "registration_shared_secret not active in the running Synapse. On a fresh install hub/render-hub.sh adds it and 'docker compose up' loads it; on an existing hub, re-render + restart Synapse first." ;;
    "")                log "registered @${LP}:localhost"; created=1 ;;
    *)                 log "register: ${reg}" ;;
  esac
else
  fail "docker not available — cannot register the local account"
fi

# login -> access token (JSON built + parsed in python; no shell/JSON injection).
# Exit codes: 0 = token on stdout, or empty for 401/403 (genuinely wrong
# password — the only case that may mean "someone else's account"); 7 = the
# homeserver is unhealthy (5xx / unreachable). A 500 during first-time setup
# used to be misreported as "account exists with a foreign password", sending
# the operator to exactly the wrong place.
login_rc=0
TOKEN="$(python3 - "${HS}" "${LP}" "${LOCAL_PASSWORD}" <<'PY'
import sys, json, urllib.request, urllib.error
hs, lp, pw = sys.argv[1], sys.argv[2], sys.argv[3]
body = json.dumps({"type":"m.login.password",
                   "identifier":{"type":"m.id.user","user":lp},
                   "password":pw}).encode()
req = urllib.request.Request(hs+"/_matrix/client/v3/login", data=body,
                             headers={"Content-Type":"application/json"})
try:
    print(json.load(urllib.request.urlopen(req)).get("access_token",""))
except urllib.error.HTTPError as e:
    sys.stderr.write("login error: %s\n" % e)
    if e.code not in (401, 403):
        sys.exit(7)          # server broken, not a credentials problem
except Exception as e:
    sys.stderr.write("login error: %s\n" % e)
    sys.exit(7)              # unreachable/timeout: same
PY
)" || login_rc=$?
if [ "${login_rc}" = 7 ]; then
  fail "homeserver unhealthy during login (see the error above) — fix the hub before provisioning; this is NOT a password problem."
fi
if [ -z "${TOKEN}" ]; then
  if [ "${created}" = 1 ]; then
    fail "created @${LP}:localhost but login failed — that is a real bug, not a re-run."
  fi
  # Pre-existing account whose password this installer didn't set: this is a
  # normal re-run on an already-configured hub. Skip WITHOUT failing setup —
  # the teammate's existing login + uplink token are untouched.
  log "@${LP}:localhost already exists with a password not set by this installer — skipping local provisioning."
  log "  (existing install: your login + uplink token are unaffected. For a clean re-provision, remove ${CREDS#"${HERE}/"} and reset the password.)"
  exit 0
fi

# set the Matrix display name (settable/changeable; from LOCAL_DISPLAYNAME, which
# setup.sh resolves from the Mac or a prompt). Best-effort — a failure warns but
# never fails provisioning. Unlike the localpart, this is freely changeable later.
if [ -n "${LOCAL_DISPLAYNAME:-}" ]; then
  if python3 - "${HS}" "${LP}" "${TOKEN}" "${LOCAL_DISPLAYNAME}" <<'PY'
import sys, json, urllib.request
hs, lp, tok, name = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
body = json.dumps({"displayname": name}).encode()
req = urllib.request.Request(
    hs + "/_matrix/client/v3/profile/@" + lp + ":localhost/displayname",
    data=body, method="PUT",
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok})
urllib.request.urlopen(req).read()
PY
  then log "display name set to '${LOCAL_DISPLAYNAME}'"; else log "display name set failed (non-fatal)"; fi
fi

# write LOCAL_* for the uplink linker (link.sh sources these instead of prompting)
( umask 077; {
    printf 'LOCAL_HS_URL=%s\n' "${HS}"
    printf 'LOCAL_USER=@%s:localhost\n' "${LP}"
    printf 'LOCAL_TOKEN=%s\n' "${TOKEN}"
  } > "${OUT}" )
chmod 600 "${OUT}"

log "done — @${LP}:localhost ready; LOCAL_* written to ${OUT#"${HERE}/"} (600)"

# Passwordless auto-login for apps/user: mint a SEPARATE app-session token (its
# own device, so signing out never touches the uplink's token) and write it
# where the app fetches it (apps/user/session.local.json, gitignored, 600).
# Best-effort — the app falls back to the login form if this is absent/invalid.
APP_TOKEN="$(python3 - "${HS}" "${LP}" "${LOCAL_PASSWORD}" <<'PY'
import sys, json, urllib.request
hs, lp, pw = sys.argv[1], sys.argv[2], sys.argv[3]
body = json.dumps({"type":"m.login.password","identifier":{"type":"m.id.user","user":lp},
                   "password":pw,"initial_device_display_name":"beepa-user-app"}).encode()
try:
    print(json.load(urllib.request.urlopen(urllib.request.Request(
        hs+"/_matrix/client/v3/login", data=body, headers={"Content-Type":"application/json"}))).get("access_token",""))
except Exception:
    pass
PY
)"
if [ -n "${APP_TOKEN}" ]; then
  APP_SESSION="${HERE}/apps/user/session.local.json"
  ( umask 077; printf '{"user_id":"@%s:localhost","access_token":"%s"}\n' "${LP}" "${APP_TOKEN}" > "${APP_SESSION}" )
  chmod 600 "${APP_SESSION}"
  log "passwordless login enabled for apps/user (no password screen)"
else
  log "APP LOGIN (fallback) -> username: ${LP}   password: ${LOCAL_PASSWORD}"
fi
