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
  # -f: discard any local edits so the build is deterministic (the stripper below
  # reproduces the one edit the build needs).
  git checkout -f --quiet "${PIN}" 2>/dev/null || { log "pinned commit ${PIN} not available — skipping"; exit 1; }
  # Xcode #Preview macros (PreviewsMacros) aren't available to a headless
  # `swift build` and fail compilation; strip those editor-only blocks first.
  python3 "${HERE}/imessage/strip-previews.py" "${SRC}/src"
  log "building imessage-cli with Swift (first build downloads deps; can take a few minutes)…"
  # Clear C/C++/ObjC include-path env vars for the build. A polluted CPATH (e.g.
  # a toolchain that prepends its own ncurses `include/` — seen with
  # ~/.cache/nebula-toolchain) makes clang resolve the macOS SDK's
  # `#include <unctrl.h>` to the wrong header, which redeclares `unctrl`
  # incompatibly and kills the Swift `Darwin` module ("could not build
  # Objective-C module 'Darwin'"). The first package compiled (often Rainbow)
  # takes the blame, but ANY Swift package fails identically. Unsetting these for
  # just this build survives re-runs and needs no change to the user's shell.
  env -u CPATH -u LIBRARY_PATH -u C_INCLUDE_PATH -u CPLUS_INCLUDE_PATH -u OBJC_INCLUDE_PATH \
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
