#!/usr/bin/env bash
# imessage/build-cli.sh — obtain the iMessage CLI by cloning Beeper's PUBLIC
# platform-imessage repo (MIT) at a PINNED commit and building it with Swift.
# Nothing is vendored into this repo; the source is fetched on demand.
#
# macOS only. Idempotent: skips if imessage/bin/imessage-cli already exists.
# Non-fatal if Swift is missing or SKIP_IMESSAGE=1 — every other network works
# without iMessage, so this must never abort the rest of setup.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="https://github.com/beeper/platform-imessage"
PIN="cda1545b87db4aeb2ec266bd8f9f335eec67c323"   # ~v0.24.4 (2026-07-14); bump deliberately
SRC="${HERE}/imessage/platform-imessage"
OUT="${HERE}/imessage/bin/imessage-cli"
log() { printf '[imessage-build] %s\n' "$*" >&2; }

[ "${SKIP_IMESSAGE:-0}" = "1" ] && { log "SKIP_IMESSAGE=1 — skipping iMessage CLI build"; exit 0; }
[ "$(uname -s 2>/dev/null)" = "Darwin" ] || { log "not macOS — iMessage is Mac-only, skipping"; exit 0; }
if [ -x "${OUT}" ]; then log "imessage/bin/imessage-cli already present — skipping build"; exit 0; fi

if ! command -v swift >/dev/null 2>&1; then
  log "Swift toolchain not found — install Xcode Command Line Tools:  xcode-select --install"
  log "then re-run setup.sh. (iMessage is optional; every other network works without it.)"
  exit 0
fi

# clone (or reuse) the source, then pin the exact commit
if [ ! -d "${SRC}/.git" ]; then
  log "cloning ${REPO} ..."
  git clone "${REPO}" "${SRC}" || { log "clone failed (offline?) — skipping iMessage"; exit 0; }
fi
(
  cd "${SRC}"
  git fetch --quiet origin 2>/dev/null || true
  git checkout --quiet "${PIN}" 2>/dev/null || { log "pinned commit ${PIN} not available — skipping"; exit 1; }
  log "building imessage-cli with Swift (first build downloads deps; can take a few minutes)…"
  swift build -c release --product imessage-cli
) || { log "swift build failed — see output above; iMessage skipped, other networks unaffected"; exit 0; }

BIN="${SRC}/.build/release/imessage-cli"
[ -x "${BIN}" ] || { log "build did not produce ${BIN} — skipping"; exit 0; }
mkdir -p "${HERE}/imessage/bin"
cp "${BIN}" "${OUT}"; chmod 755 "${OUT}"
log "installed ${OUT} (built from ${REPO} @ ${PIN})"
log "STILL MANUAL, per teammate:"
log "  1. grant '${OUT}' Full Disk Access in System Settings > Privacy & Security"
log "  2. set self_handle (your iMessage phone/email) in imessage/daemon.json"
log "  then re-run setup.sh to load the daemon."
