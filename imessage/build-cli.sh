#!/usr/bin/env bash
# imessage/build-cli.sh — obtain the iMessage CLI by DOWNLOADING Beeper's
# PREBUILT, Developer-ID-signed + notarized universal binary from their public
# GitHub releases (MIT), pinned to an exact version and verified against a
# pinned SHA-256. Nothing is compiled or vendored; the binary is fetched on
# demand.
#
# Why prebuilt instead of `swift build`: the signed release carries a STABLE
# code identity (Developer ID "Automattic, Inc." PZYM8XX95Q) and the
# com.apple.security.automation.apple-events entitlement. macOS keys TCC grants
# (Full Disk Access, Automation, Accessibility) on that identity, so grants
# STICK — and even survive upgrades to a new version — instead of being revoked
# every rebuild the way an ad-hoc `swift build` binary's were. No Xcode/Swift
# toolchain needed either.
#
# macOS only. Idempotent: skips if imessage/bin/imessage-cli already exists (so
# a present, already-granted binary is never disturbed). Non-fatal if the
# download fails or SKIP_IMESSAGE=1 — every other network works without
# iMessage, so this must never abort the rest of setup.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="0.24.4"                # bump deliberately; must match SHA256 below
# SHA-256 of imessage-cli-${VERSION}-macos-universal.tar.gz, pinned as the
# source of truth (the release also ships a .sha256, but that is served from the
# same origin and is not an independent check). Update both together on a bump.
SHA256="7629c828593faef7e324cd86a94df2e8fdbe7ae48c7b6f8d22167589627a77e6"
ASSET="imessage-cli-${VERSION}-macos-universal.tar.gz"
URL="https://github.com/beeper/platform-imessage/releases/download/v${VERSION}/${ASSET}"
TEAM_ID="PZYM8XX95Q"           # expected Developer ID team (Automattic, Inc.)
OUT="${HERE}/imessage/bin/imessage-cli"
log() { printf '[imessage-build] %s\n' "$*" >&2; }

[ "${SKIP_IMESSAGE:-0}" = "1" ] && { log "SKIP_IMESSAGE=1 — skipping iMessage CLI download"; exit 0; }
[ "$(uname -s 2>/dev/null)" = "Darwin" ] || { log "not macOS — iMessage is Mac-only, skipping"; exit 0; }

if [ -x "${OUT}" ]; then log "imessage/bin/imessage-cli already present — skipping download"; exit 0; fi

# --- download the pinned, signed prebuilt binary ---------------------------
command -v curl >/dev/null 2>&1 || { log "curl not found — cannot download iMessage CLI, skipping"; exit 0; }
TMP="$(mktemp -d)"; trap 'rm -rf "${TMP}"' EXIT
log "downloading ${ASSET} (Developer-ID signed, notarized) …"
curl --retry 5 --retry-delay 3 --retry-all-errors -fsSL "${URL}" -o "${TMP}/${ASSET}" \
  || { log "download failed (offline?) — skipping iMessage; other networks unaffected"; exit 0; }

got="$(shasum -a 256 "${TMP}/${ASSET}" | awk '{print $1}')"
if [ "${got}" != "${SHA256}" ]; then
  log "SHA-256 MISMATCH — refusing to install. expected ${SHA256} got ${got}"; exit 1
fi
log "checksum OK (${got})"

tar -xzf "${TMP}/${ASSET}" -C "${TMP}" || { log "extract failed — skipping"; exit 0; }
BIN="$(find "${TMP}" -type f -name imessage-cli -perm -u+x | head -1)"
[ -n "${BIN}" ] || BIN="$(find "${TMP}" -type f -name imessage-cli | head -1)"
[ -n "${BIN}" ] || { log "tarball did not contain imessage-cli — skipping"; exit 0; }

# Verify the Developer ID signature is intact and from the expected team BEFORE
# installing (guards a tampered mirror even if the checksum were also swapped).
if ! codesign --verify --strict "${BIN}" 2>/dev/null; then
  log "signature verification FAILED — refusing to install"; exit 1
fi
# No pipe here: `codesign | grep -q` under set -o pipefail is a latent race —
# grep -q exits on first match and closes the pipe, codesign takes SIGPIPE,
# and a SUCCESSFUL match reports as a failed check on some machines.
sig="$(codesign -dvv "${BIN}" 2>&1 || true)"
case "${sig}" in
  *"TeamIdentifier=${TEAM_ID}"*) ;;
  *) log "unexpected code-signing team (want ${TEAM_ID}) — refusing to install"; exit 1 ;;
esac

mkdir -p "${HERE}/imessage/bin"
cp "${BIN}" "${OUT}"; chmod 755 "${OUT}"
# Do NOT re-sign: that would strip the Developer ID signature (and its stable,
# grant-preserving identity) and replace it with a throwaway ad-hoc one.
log "installed ${OUT} (prebuilt v${VERSION}, Developer ID ${TEAM_ID})"
log "STILL MANUAL, per teammate (System Settings > Privacy & Security) — each is"
log "a ONE-TIME grant; identities are stable, so they persist across restarts"
log "and future version bumps:"
log "  1. RECEIVING: grant '${OUT}' Full Disk Access (add it via '+')."
log "  2. SENDING: grant '${OUT}' Accessibility too (the engine types into the"
log "     Messages window via the accessibility APIs), and Allow the 'control"
log "     Messages' Automation prompt on first send. The daemon opens the right"
log "     Settings pane automatically the first time a send fails for lack of a"
log "     grant. IMPORTANT: if you ever REPLACE this binary, remove ('-') and"
log "     re-add ('+') its Accessibility and Full Disk Access rows — toggling a"
log "     stale row does not re-key it to the new binary."
log "  3. set self_handle (your iMessage phone/email) in imessage/daemon.json,"
log "     then re-run setup.sh to load the daemon."
