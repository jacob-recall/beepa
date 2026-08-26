# Plan: iMessage connector, Phase 1 MVP (imsg-p1-v1)

User-approved direction (2026-08-25): bridge iMessage into the existing local
Matrix stack using Beeper's `platform-imessage` as the engine, custom
connector, backend before UI. Phases 2 (richness) and 3 (Hub v3 left-bar +
Directory UI) are separate future plans.

## Facts (verified)
- `platform-imessage` is a Swift CLI/library, NOT a bridge: `send`,
  `send-file`, `reply`, `edit`, `react`, `typing`, `create-chat`,
  `chats`/`messages`/`search` with `--json`, and a REPL that subscribes to
  real-time incoming-message events. Permissions: Accessibility, Contacts,
  Messages Data, Automation. Must run on the host Mac, signed into
  Messages.app. macOS 26.6 (Tahoe) is its primary target.
- Host toolchain: Swift 6.3.3 (arm64, CLT only, no Xcode/Homebrew), Python
  3.9 (daemon language — stdlib only, no package manager available).
- Existing stack: compose project matrix-wa; Synapse v1.159.0 in Docker on
  127.0.0.1:8008, appservices reach outward from the container; existing
  whatsapp appservice pattern in `synapse/registration.yaml`.

## Architecture
```
Messages.app ⇄ imessage-cli (Swift, host)      Synapse (Docker)
                    ⇅ JSON stdio                     ⇅ appservice HTTP
              imessage-daemon (Python 3, host, launchd)
              - event stream → portal rooms (ghosts @imessage_*)
              - Matrix transactions → send/send-file
              - state: SQLite at ~/work/pm_mng/imessage/state.db
```
- Daemon listens ONLY on `127.0.0.1:29350` — deliberately OUTSIDE the Beeper
  dev-stack range 29317-29341 (M-3). The port is hardcoded in both the
  registration and the launchd plist; on bind failure the daemon FAILS
  CLOSED (exits; never auto-shifts ports — the registration URL is static
  and a shifted port would deliver hs_token-bearing transactions to a
  stranger). Synapse reaches it as `http://host.docker.internal:29350`
  (reachability empirically tested in I2 before anything depends on it; if
  Docker Desktop cannot reach a loopback-bound host port, STOP and redesign
  the transport — do not bind wider).
- New appservice registration `imessage-registration.yaml`: id `imessage`,
  fresh random as/hs tokens, bot `@imessagebot:localhost`, exclusive
  namespace `@imessage_.*:localhost`, rate_limited false.
- Membership (M-22, definitive): portal rooms and the iMessage space are
  created by the bot, which INVITES @jkali only. The daemon never holds or
  uses @jkali's user token and never force-joins. Accepting invites is the
  user's (or, during testing, the operator's — see I3) action; Phase 3 UI
  will surface pending invites.
- All portals added to a new `iMessage` space (bot-created), mirroring the
  WhatsApp space pattern → own section in Element's left bar.
- MVP scope: inbound + outbound text and file attachments, 1:1 and group
  chats, contact display names, self-loop testability. Explicitly NOT in
  MVP: reactions/tapbacks, edits, replies-as-threads, typing, read receipts,
  history backfill (Phase 2).

## Security constraints (pre-review draft; final = review dispositions)
- Daemon config (`imessage/daemon.yaml`: as/hs tokens, homeserver URL) mode
  600; state.db 600; all under the existing 700 project root; added to the
  standing secret-file invariant.
- Daemon binds 127.0.0.1:29350 only; verified by listener enumeration (no
  `0.0.0.0`/`[::]` listener on 29350, `127.0.0.1:29350` the sole binding);
  port fixed, verified free this session, fail-closed on bind error (M-3);
  unrelated Beeper dev stack occupies 29317-29341 and is untouched.
- hs_token checked on every inbound transaction; requests without it 403.
- The macOS TCC grants (Accessibility/Contacts/Messages/Automation) are
  user-approved by clicking the prompts — the assistant never automates
  permission dialogs. Messages Data = the daemon can read all iMessages;
  that is the product, stated plainly.
- iMessage content lands in the same unencrypted local Postgres/media_store
  archive as WhatsApp (F10 posture, FileVault + loopback-only).
- Egress stated honestly (review correction): imessage-cli makes no network
  calls of its own, but it drives Messages.app, which sends to arbitrary
  handles over Apple's network — the daemon IS a new localhost-HTTP-triggered
  egress channel. The controls that bound it: M-1 sender allowlist, M-2
  mapped-chats-only destinations, M-8 rate caps. The daemon's own network
  client talks only to 127.0.0.1:8008.
- Apple ToS note for the user: this automates Messages.app locally (not a
  protocol reimplementation — lower account risk than e.g. pypush-style
  approaches), but it is still unofficial automation.

## Slices
- **I1 — Engine**: clone github.com/beeper/platform-imessage into
  `imessage/platform-imessage` at a pinned commit SHA (recorded here +
  README, M-14), `swift build -c release`, COPY (not symlink) `imessage-cli`
  to `imessage/bin/` (M-14). USER STEP (M-5 procedure): ADD
  `/Users/jkali/work/pm_mng/imessage/bin/imessage-cli` BY PATH in System
  Settings → Privacy & Security for each grant that supports add-by-path
  (Accessibility, Automation targets, Full Disk Access if required for
  Messages data); then run `imessage/bin/imessage-cli current-user` and
  approve any remaining prompts, verifying each prompt names imessage-cli.
  NEVER grant Terminal.app, python3, or any shell; if a grant can only be
  obtained by broadening a general-purpose binary, the M-5 STOP fires HERE
  in I1 (not I6). Confirm Messages.app is signed in.
  *Acceptance*: `current-user` returns the Apple ID; `chats --json` returns
  a non-error chat list; the granted TCC entries (System Settings, or
  `tccutil`-adjacent inspection where readable) show
  `imessage/bin/imessage-cli` as the holder, with Terminal.app/python3
  entries unchanged from their pre-slice state — asserted BEFORE I3.
- **I2 — Appservice plumbing**: registration-file readability is already
  proven by the existing pattern: `synapse/registration.yaml` is mode 600
  owned by uid 501, and the Synapse container runs as UID=501/GID=20
  (compose env) — so `synapse/imessage-registration.yaml` uses the identical
  owner/mode (600, uid 501); if that combination were ever unreadable, STOP
  (no in-slice widening of a token-bearing file). Generate the registration
  with fresh tokens (M-16 namespaces); back up `homeserver.yaml`, append to
  `app_service_config_files`, restart Synapse, re-check
  base-stack health (S1(a) + WhatsApp bot profile 200 — the existing bridge
  must be unaffected); daemon skeleton: PUT /_matrix/app/v1/transactions/*
  (hs_token-gated, 403 otherwise), GET /health; register @imessagebot +
  displayname "iMessage bridge bot".
  *Acceptance*: (a) container→host reachability proven (`docker compose exec
  synapse curl http://host.docker.internal:29350/health` 200); (b)
  @imessagebot profile 200; (c) transaction with wrong hs_token → 403, with
  correct token → 200; (d) WhatsApp bridge still answers `list-logins`;
  (e) listener check: `127.0.0.1:29350` is the sole binding for that port,
  no `0.0.0.0`/`[::]` on 29350; (f) registration file shows owner uid 501
  mode 600 and Synapse loaded it (appservice in logs / bot registrable).
- **I3 — Inbound**: daemon drives `imessage-cli` (REPL event subscription;
  fallback: poll `messages` deltas — decided empirically in-slice); chat→
  room mapping in SQLite; per incoming message: ensure ghost (register +
  displayname from chat participants/Contacts), ensure portal room (created
  by bot, jkali invited, added to iMessage space, `uk.half-shot.bridge`
  state), post text as ghost; attachments: file path from engine → upload to
  Synapse media → m.image/m.file.
  *Test-membership prerequisite*: after the self-chat portal + space are
  created and invites issued, the OPERATOR (main session) accepts them as
  @jkali via a short-lived login used only for testing — that token is never
  written to daemon config, never passed to the daemon process, and is
  revoked at slice end (M-22 kept: the daemon itself never sees a user
  token).
  *Acceptance (self-loop)*: @jkali membership is `join` in the self-chat
  portal (precondition), then: `imessage-cli send <own-handle> "<nonce>"`
  (the user's own Apple ID = Messages "send to self", M-15 constraints
  enforced) produces, within 60s, a Matrix room in the iMessage space
  containing the nonce, sent by a ghost or bot-attributed sender; an
  attachment self-loop lands as an m.file/m.image with matching bytes.
- **I4 — Outbound**: transactions: m.room.message from @jkali in a mapped
  portal → `send`/`send-file` to that chat; echo suppression via
  sent-message ledger (from_me events matching a recent daemon-sent text/
  attachment are dropped, TTL 60s).
  *Acceptance (self-loop)*: posting a nonce text via jkali's API into the
  self-chat portal appears in `messages <chat> --json` as from_me within
  60s, and does NOT double-post back into the Matrix room. File outbound
  similarly. M-8/M-13 checks: replaying the same txn_id returns 200 with
  zero engine invocations; a burst exceeding the per-chat rate cap is
  dropped-and-logged; the echo ledger rows contain only sha256 hashes (no
  plaintext bodies in state.db).
- **I5 — Names & polish**: group chat room names from chat title; ghost
  displaynames from Contacts data in `chats --json`; iMessage space named
  "iMessage" with distinct avatar; daemon survives imessage-cli crash
  (respawn with backoff) and Synapse restart (transaction retries are
  Synapse-side; daemon is stateless per-request).
  *Acceptance*: self-chat + at least the space have human names; kill -9 the
  engine child → daemon respawns it and the self-loop still passes.
- **I6 — Service-ification**: launchd user agent
  (`~/Library/LaunchAgents/com.jkali.imessage-daemon.plist`, KeepAlive,
  logs to `imessage/logs/`), start-on-login; TCC grants verified to hold for
  the launchd-spawned process (if macOS re-prompts, USER STEP to re-grant);
  README + memory updated; Connections card for iMessage in the hub is
  Phase 2 (not here).
  *Acceptance*: `launchctl kickstart` → self-loop passes with the daemon
  running under launchd, not a shell.
- **I7 — Verification**: fresh `pilotfish:verifier` on the exact claim
  (self-loop both directions incl. attachment, hs_token 403, listener set
  incl. no new exposure, WhatsApp bridge unaffected, secret modes, launchd
  liveness).

## Rollback
- Any slice: stop daemon (`launchctl bootout`), remove registration from
  `homeserver.yaml` (restore I2 backup), restart Synapse, delete
  `imessage/` tree. Portal rooms/ghosts left behind are inert (bot appservice
  gone); full cleanup = Synapse admin purge, documented as optional.
- User-side: macOS Settings → Privacy & Security → revoke the four grants.

## Stops
Any slice failing after 2 distinct fix attempts → stop and report. Never
click/automate macOS permission dialogs. Never run `delete-chat` against
real chats; engine testing uses only the self-chat. If I2(a) reachability
fails, STOP (transport redesign needs its own review).

## Security review dispositions (pilotfish:security-reviewer, 2026-08-26) — GOVERNING
Verdict: sound in shape; 22 findings folded in. Framing correction adopted:
this is the first component where remote unauthenticated parties (anyone who
can iMessage the user) feed a hand-written host-side parser whose child holds
Accessibility/Automation/Messages TCC grants and can send to arbitrary
handles. Where this section conflicts with earlier draft text, THIS section
governs.

Mitigate-now (folded into implementation as hard requirements):
- **M-1** Sender allowlist: drop every transaction event whose sender is not
  exactly `@jkali:localhost` before ANY engine call (kills ghost-echo →
  outbound loops). I4 acceptance: ghost-authored event ⇒ zero engine calls.
- **M-2** Destinations only via the SQLite room→chat map; unmapped room ⇒
  drop; never derive a handle from event/room content; no `create-chat` in
  the MVP outbound path.
- **M-3** Fixed port 29350 (outside Beeper's 29317-29341), fail-closed bind.
- **M-4** Attachments: outbound temp files via `tempfile.mkstemp` in
  `imessage/tmp/` (700), our names, sanitized extension only; mxc accepted
  only as `mxc://localhost/[A-Za-z0-9_-]{1,255}`; inbound engine paths
  `realpath`-checked against an allowlist (`~/Library/Messages/Attachments`,
  engine temp dir) else rejected; size caps both ways; temps unlinked in
  `finally`.
- **M-5** TCC grants must be attributed to `imessage/bin/imessage-cli`
  (added by path in System Settings); NEVER grant Terminal.app, python3,
  launchd, or any shell as a workaround — if grants require broadening a
  general-purpose binary, STOP (own review). I6 asserts which binary holds
  each grant.
- **M-6** Listener hardening: ThreadingHTTPServer + daemon threads, 30s
  socket timeout, 8 MiB body cap (413), reject Transfer-Encoding, no CORS/
  OPTIONS ever, Host-header allowlist (127.0.0.1:29350, localhost:29350,
  host.docker.internal:29350), log_message override strips query strings,
  `/health` = static 200 with no identifying data.
- **M-7** hs_token from `Authorization: Bearer` (query param fallback only),
  compared with `hmac.compare_digest`; verify empirically which form Synapse
  v1.159.0 sends.
- **M-8** Transaction idempotency (persist last txn_id; repeats ⇒ 200
  without re-execution) + outbound rate cap ≤1/s and ≤30/min per chat
  (drop + log) as blast-radius limit.
- **M-9** Untrusted-input handling: line-length-capped JSON reads, bodies
  clamped ~64 KiB, plain `m.text` only (no formatted_body from remote text),
  displaynames stripped of C0/C1/bidi + clamped, ghost localparts via a
  deterministic INJECTIVE mautrix-style escaping of the handle (no lossy
  strip).
- **M-10** Invariant additions: `imessage-registration.yaml` 600 (in
  synapse/ for the container mount), `daemon.yaml` 600, `state.db{,-wal,-shm}`
  600, `logs/` 700 + 600 files, `tmp/` 700, `bin/` 700; `imessage/` added to
  `.gitignore` at I2.
- **M-11** launchd: plist has paths only (no secrets), `Umask` 0o077,
  pre-created 600 logs, WorkingDirectory=project root, ThrottleInterval set,
  absolute CLT python3 path.
- **M-12** Logging: INFO carries no message bodies, filenames, handles, or
  tokens — ids/hashes/byte-counts/outcomes only; DEBUG off by default and
  never in the plist.
- **M-13** Echo ledger stores sha256 hashes, never plaintext bodies.
- **M-14** Supply chain: pin the engine to a commit SHA recorded here and in
  README; record `Package.resolved`; no `swift package update` mid-slice;
  COPY (not symlink) the binary to `imessage/bin/` — cdhash change on
  rebuild forces a visible TCC re-prompt (feature, keep it). The "no network
  calls" claim is upstream's; corroborate by scanning Package.swift deps.
- **M-15** Self-loop hygiene: enforced destination==current-user check
  before every test send; ≤10 sends/run, ≥5s apart; nonces are
  content-free `pmmng-test-*` tokens; dummy attachment files only; report
  tells the user test messages sync to all their Apple devices and self-chat
  cleanup is theirs (never `delete-chat`).
- **M-16** Registration mirrors the WhatsApp pattern exactly
  (`^@imessage_.*:localhost$`, `^@imessagebot:localhost$`, both exclusive;
  sender_localpart inside the namespace); no ephemeral-events flags in MVP.

Accepted (with reasons recorded):
- **M-17** Container→host-daemon is a new trust-boundary crossing and the
  WhatsApp container is inside the reach set; source-IP filtering cannot
  help (Docker Desktop proxies everything to 127.0.0.1). hs_token +
  M-1/M-2/M-6 are the controls.
- **M-18** Token-reading local process ⇒ contact impersonation (new) but
  exfiltration is not new (F5 framing); bounded by M-1/M-2/M-8. Tokenless ⇒
  fingerprint + DoS only; hostile websites can't pass the gate.
- **M-19** Archive posture unchanged under F10, but scope note for README:
  iMessage includes SMS/RCS fallback (2FA codes, bank alerts) — the
  archive's sensitivity class goes up.
- **M-20** argv disclosure accepted (dominated); parsing mitigated: list
  argv, shell=False everywhere, `--` before user-controlled operands; prefer
  the REPL/stdin path for `send` if workable (removes argv leak).
- **M-21** No ghost-namespace collisions (verified against the live
  registration); Beeper dev stack shares only the port range (M-3).
- **M-22** as_token cannot read WhatsApp content (namespace-scoped) and the
  daemon NEVER receives @jkali's user token — invite-not-force-join is the
  security-correct choice, kept.

I7 verifier claim additions (from the review): wrong/absent hs_token → 403
(constant-time); oversize body → 413; ghost-authored event → zero engine
invocations; unmapped room → dropped; `mxc://evil.com/x` and traversal
bodies rejected without filesystem touch; engine path outside allowlist
rejected; port 29350 + fail-closed bind; INFO log sample body-free; TCC
grants attributed to imessage-cli (not Terminal/python3); mode sweep incl.
state.db-wal; pinned commit SHA recorded; replayed txn_id → 200 with zero
engine invocations (M-8); over-cap outbound burst dropped-and-logged (M-8);
echo-ledger rows hash-only, no plaintext bodies (M-13).

### I1 build deviation (2026-08-26, recorded)
CLT-only Swift cannot expand SwiftUI `#Preview` macros (Xcode-only plugin).
The pinned checkout (cda1545b) was patched locally: two `#Preview` blocks and
their orphaned `@available` attributes removed from SettingsView.swift and
EclipsingDebuggerView.swift (18 lines deleted, dev-preview scaffolding only —
zero runtime behavior change). `git diff` in imessage/platform-imessage is
the exact record. Binary: 13.4 MB arm64, copied (not symlinked) to
imessage/bin/imessage-cli per M-14.

### Product requirement captured 2026-08-26 (user): guided permission setup
For the live version the TCC permission flow must be part of the hub's
Connections card (like WhatsApp's Connect): a click-through wizard. Design
(Phase 2 scope, to be planned/gated then): @imessagebot gains `setup` and
`status` commands; the daemon live-probes each grant (Accessibility, Full
Disk/Messages Data, Automation, Contacts) by attempting the corresponding
read, and can deep-link System Settings to the exact pane via
`open "x-apple.systempreferences:com.apple.preference.security?Privacy_..."`;
the hub's iMessage card renders the checklist with an "Open settings" button
per missing grant and auto-advances as probes turn green. Manual grants used
for Phase 1 testing (user-performed).

### Scope adjustment (user, 2026-08-26): text-first
User: "not incredibly concerned with sending files... this primarily needs to
work on text." Attachments demoted to best-effort in Phase 1: inbound
attachment relay implemented (asset:// hex path decoding + M-4 allowlist);
outbound file path implemented but not acceptance-gated. I3/I4 acceptance and
the I7 claim are TEXT loops; attachment checks dropped from acceptance.

### I7 outcome (2026-08-26)
First verifier pass: REFUTED — P1: single-slot txn idempotency defeated by
interleaved echo transactions (replay re-sent an iMessage); P2: state.db 644.
Fixes applied: set-based `txns` table (1h retention) replaces the slot;
`os.chmod(state.db, 0o600)` at startup + immediate chmod; stale pid file
removed (launchd owns lifecycle); deviation line-count corrected (18).
Re-verification of the exact defeat scenario (replay with intervening
transaction): nonce sent EXACTLY once. Rate-cap min-gap arm is structurally
unreachable via the synchronous HTTP path (engine latency ≥1s spaces sends);
both limiter arms proven by in-process unit test (<1s repeat blocked; 31st
send in 60s blocked) with zero engine sends. All other 7 claim points were
CONFIRMED by the verifier on first pass. Advisory left open: none blocking.
