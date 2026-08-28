# Audit Findings

Fresh-eyes audit, 2026-08-28. Read-only: every finding below is evidence-cited;
nothing was fixed. **CONFIRMED** = reproduced by reading the exact code path or
by a live command in this audit. **SUSPECTED** = the code path implies it, but
it was not reproduced (usually because doing so would mutate live state).

Severity order, most severe first.

---

## F1 — The entire user-facing app exists twice; the legacy copy is live and has its own send paths — CONFIRMED

**Claim:** `hub/site/app.js` (2,344 lines, served at `http://127.0.0.1:8010`)
is a full parallel implementation of nearly everything in `shared/` +
`apps/user/`, and it is not dead: `docker-compose.yml` calls it *"the current
daily driver"* and the container is up.

**Evidence:**
- `docker-compose.yml` (views service comment): "hub/site (the current daily driver) is unaffected"; `docker ps` shows `matrix-wa-hub-1` up on 127.0.0.1:8010, and `curl -sI http://127.0.0.1:8010/` returns 200 with full CSP headers.
- `shared/ui/render.js:1`, `shared/ui/chat.js:1`, `shared/matrix/client.js:1` all begin "Relocated **verbatim** from hub/site/app.js" — but the monolith kept its own copies: send paths at `hub/site/app.js:438` (sendCmd), `:470` (sendSecretToMgmt), `:1383` (sendConvoMessage); three sync loops; its own renderer, feed, connections UI (subagent sweep found all ~80 functions live, none dead *within* the file).
- Consequence: the load-bearing doc claim — root `CLAUDE.md`: "`sendConvoMessage` (in `shared/ui/chat.js`) is the *only* external send path in the whole system" — is **false** while :8010 is served. There are at least four external-send call sites in two independent codebases (plus `shared/ui/sources.js:277,309`, see F18).
- The copies have already drifted: the shared/app versions gained consent decorations, proposals, contacts; the monolith has none of them, and bug fixes land (or don't) twice.

**Resolved 2026-08-28** — see docs/SIMPLIFICATION-PLAN.md P1 (legacy hub retired)

## F2 — The new apps (:8011) are served with no security headers; `frame-ancestors` lives only in a `<meta>` tag, where browsers ignore it — CONFIRMED

**Claim:** The successor apps lost the HTTP-header hardening the legacy hub has.

**Evidence:**
- `curl -sI http://127.0.0.1:8011/apps/user/index.html` → no `Content-Security-Policy`, no `X-Frame-Options`, no `Referrer-Policy` (verified live in this audit). Compare `curl -sI http://127.0.0.1:8010/` → full CSP + `X-Frame-Options: DENY` + `Referrer-Policy: no-referrer` (from `hub/nginx.conf`).
- The apps carry CSP only in `<meta http-equiv>` (`apps/user/index.html:5`, `apps/master/index.html:5`). Per the CSP spec, `frame-ancestors` (and `report-uri`, `sandbox`) are **ignored when delivered via meta** — so the declared `frame-ancestors 'none'` does nothing, and both the teammate app and the manager console can be framed by any site (clickjacking on the consent toggles / proposal composer). The rest of the meta CSP does apply, but only after the parser reaches the tag.
- The `views` nginx service mounts no config (`docker-compose.yml` views service: only the two content mounts), so it also lacks the legacy hub's GET/HEAD-only method filter and no-store cache headers.

**Resolved 2026-08-28** — see docs/SIMPLIFICATION-PLAN.md P2 (views/nginx.conf hardening)

## F3 — Interrupted backfill leaves a permanent, silent gap in a mirror room — SUSPECTED (code-confirmed path; not reproduced)

**Claim:** If the master becomes unreachable partway through the initial
backfill of a newly shared conversation, the not-yet-forwarded history is never
mirrored — with no error and no retry.

**Evidence (code path):** `agents/uplink/uplink.py`:
- `create_mirror()` inserts the `mirror_rooms` row (`:466-470`) **before** calling `self.backfill(...)` (`:472`).
- `backfill()` → `forward_events()` → `self.master(...)` raises `MasterUnreachable` mid-batch; already-posted events are in `event_map`, the rest are not.
- On the next loop, the room is in the reconcile plan's `keep` set, and the "Backfill/tail every kept + freshly-created room" step (`:421-422`) calls `sync_room()` — which is a **no-op** (`:526-528`, see F5). `tail_once()` (`:890`) only forwards *new* `/sync` timeline events. Nothing ever re-runs the backfill.
- Reproducing would require killing the live master mid-backfill; not done in a read-only audit. The integration harness's `3_offline_catchup` covers tail-time outage, not backfill-time outage (`tests/integration/harness.py:580`).

## F4 — `enroll.py`'s manager check contains a latent lockout bug (`or` where `and` was meant) — CONFIRMED (logic; benign only under default naming)

**Evidence:** `master/enroll.py:322` (in `_require_manager`):
```python
if who != manager or who != "@manager:master":
    raise HttpError(403, ...)
```
This rejects **everyone** unless `who` equals *both* the configured manager
mxid *and* the hardcoded literal. It works today only because
`tokens.local`'s manager *is* `@manager:master`. Rename the manager or change
the server name and `/admin/add-teammate` locks out permanently (a fail-closed
bug, so not exploitable — but a booby trap for any redeployment, and clearly
not the intended expression).

## F5 — `sync_room()` is a no-op, and the comment above its call site claims it backfills/tails — CONFIRMED

**Evidence:** `agents/uplink/uplink.py:526-528` (`"""Placeholder per-room
catch-up hook; tail is driven by the global loop."""` → `return`), called from
`:421-422` under the comment "Backfill/tail every kept + freshly-created
room." The loop over `create|keep` does nothing at all. Dead code plus a
misleading comment that hides F3.

## F6 — `reconcile.next_watermark()` is unit-tested but never called by the daemon — CONFIRMED

**Evidence:** `grep -rn next_watermark agents tests` → definition
(`agents/uplink/reconcile.py:65`) and six test references
(`tests/unit/uplink_reconcile.test.py:95-104`); **zero** call sites in
`uplink.py`. The "watermark advances only on confirmed delivery" property is
actually enforced by exception ordering in `tail_once()`/`pull_proposals()`
(`uplink.py:905-910, 882-885`). The tests assert a function the runtime never
executes — false confidence about where the guarantee lives.

## F7 — `mirror_rooms.last_synced_pos` is write-only state — CONFIRMED

**Evidence:** written at `uplink.py:530-533` (`_set_watermark`) and `:468`;
the only read is `mirror_for()`'s SELECT (`:227-230`), whose callers use
`row[0]`/`row[1]` only. The one real reader is the test harness's assertion
(`tests/integration/harness.py:316`). The per-room watermark drives nothing;
actual resume position is the single global `meta.sync_since` token.

## F8 — `mapped_ids_for_room()` is dead, and `forward_events()` re-loads the entire `event_map` table on every call — CONFIRMED

**Evidence:** `uplink.py:242-246` defined, never called (grep). Meanwhile
`forward_events()` does the same full-table `SELECT local_event_id FROM
event_map` into a Python set (`:546-547`) on **every** tail iteration and every
backfill — O(all events ever mirrored) per ~30s loop, growing forever.
Correctness is fine; it's dead weight plus an unbounded slow creep.

## F9 — A local-homeserver outage crashes the uplink; it survives only via launchd restart — SUSPECTED

**Evidence:** `Uplink.run()` (`uplink.py:944-975`) catches `MasterUnreachable`,
`urllib.error.HTTPError`, and `KeyboardInterrupt`. A transport-level failure
against the **local** hs (`URLError`, `TimeoutError`, `ConnectionError` from
`self.local(...)` — only master-side calls are wrapped, `:185-191`) propagates
out of `run()` and kills the process. `com.jkali.uplink.plist` sets
`KeepAlive` + `ThrottleInterval 15`, so the daemon crash-loops at 15s intervals
instead of backing off. Current logs show no traceback (`grep -c Traceback
agents/uplink/logs/uplink.err` → 0) because the local hub hasn't dropped while
the daemon ran — hence SUSPECTED, not reproduced.

## F10 — README.md describes a system that no longer exists, and contradicts itself — CONFIRMED

**Evidence:** `README.md` is titled "Local WhatsApp↔Matrix bridge"; its
services table (lines 8-16) omits mautrix-meta, mautrix-twitter,
mautrix-gmessages, the views server (:8011), the master stack (:8018), the
enroll service (:8019), the uplink, and both new apps. It says backfill "is
**enabled** (recent history: ~50 msgs/chat...)" (line ~23) and, 25 lines later,
"History backfill is **off** (privacy default)" (line ~48). Nothing about the
manager/consent layer that is now the bulk of the codebase.
*(Superseded by the rewritten README + `docs/ARCHITECTURE.md` from this audit.)*

## F11 — "Eight scenarios" comments vs. eleven actual scenarios — CONFIRMED

**Evidence:** `tests/integration/run.sh:2` ("the 8 Phase-2 scenarios") and
`tests/integration/harness.py:4` ("Drives the eight edge-case scenarios") vs.
`SCENARIOS` containing 11 entries (`harness.py:479-1311`,
`scenario_1_share_one` … `scenario_11_profile_span_platforms`). Docs
(`tests/CLAUDE.md`, `PLAN-MASTER-SYNC-IMPL.md`) say 11; the code comments say 8.

## F12 — The integration harness's default state dir is a hardcoded, session-specific temp path — CONFIRMED

**Evidence:** `tests/integration/harness.py:56-60`: `SYNCTEST_STATE_DIR`
defaults to `/private/tmp/claude-501/-Users-jkali-work-pm-mng/736e7f1b-.../scratchpad/uplink-state`
— a machine- and session-specific scratch path baked into a tracked file. On
any other machine (or after temp cleanup) the default silently recreates a
directory under `/private/tmp/claude-501/...`, which is misleading at best.

## F13 — Stale status claims in the plan docs — CONFIRMED

**Evidence:** `PLAN-MASTER-SYNC-IMPL.md:6` says the work lives on branch
`feat/master-sync`, "no push yet". Reality: `git log` shows it merged to
`main` (HEAD → main, `2c9316c`), remote `origin` = `github.com/jacob-recall/beepa`.
Minor, but it's the first line an onboarding reader sees.

## F14 — `shared/ui/` is one fully cyclic import cluster; importing any file drags in the send path, forcing the master app to duplicate code — CONFIRMED

**Evidence:** import edges (grep of `shared/ui/*.js`): `account-data ↔ render`,
`nav ↔ chat`, `rows ↔ chat`, `search ↔ rows`, `nav ↔ account-data`,
`sources ↔ connections`, etc. — all eight UI modules form one strongly
connected component containing `chat.js:sendConvoMessage` and
`sources.js:sendCmd`. `apps/master/main.js:1-19` documents that this is *why*
it re-implements `resolveMirrorContent` (≈ `render.js:convoResolveContent`),
`startTail` (≈ `chat.js:startConvoWatch`), `buildPlatBadge`, `localpart`, and
relative-time/platform-label helpers locally. The duplication is deliberate and
documented — but it exists only because the module graph is a knot. Two copies
of the content whitelist (`render.js:61-78` vs `apps/master/main.js:87-113`)
must now be kept in sync by hand; `apps/master/main.js`'s copy has already
grown master-only behavior (v1.5 media), making future drift likely.

## F15 — The "throwaway" test homeserver has been running for 31+ hours next to production stacks — CONFIRMED

**Evidence:** `docker ps`: `matrix-synctest-synapse-1` / `-postgres-1` "Up 31
hours" on 127.0.0.1:8028. Harmless but contradicts "throwaway", consumes
resources, and leaves a third homeserver accepting logins on localhost.

## F16 — The "mirrored" consent test suites are not actually identical — CONFIRMED (minor)

**Evidence:** `docker run … node tests/unit/consent.test.js` → **83 passed**;
`python3 tests/unit/consent_py.test.py` → **80 passed**. Both green (run in
this audit), and my line-by-line comparison of the two resolvers found no
behavioral divergence — but the suites the parity claim rests on differ by
three cases, so "byte-parity, asserted by mirrored tests" is looser than
documented.

## F17 — The uplink re-implements the bridge-source table by hand — CONFIRMED (minor)

**Evidence:** `agents/uplink/uplink.py:64-76` (`SOURCE_LABEL_TO_ID` /
`SOURCE_ID_TO_LABEL`, with a comment "mirroring shared/ui/sources.js") vs.
`shared/ui/sources.js` `SOURCES`. Unlike consent (which has cross-language
parity tests), nothing ties these two tables together;
`tests/unit/uplink_sources.test.py` tests only the Python side. Adding a
bridge means remembering both — plus a third copy in `hub/site/app.js` and a
fourth in `apps/master/main.js:328-335` (`PLATFORM_ICON`/`PLATFORM_LABEL`).

## F18 — "The only external send path" is imprecise even inside the new app — CONFIRMED (doc precision)

**Evidence:** `shared/ui/sources.js:277` (`sendCmd`) and `:309`
(`sendSecretToMgmt`) also PUT `m.room.message` — into verified bridge
*management* rooms (C-1 guard re-verifies before every send, confirmed by
subagent read). These are command/credential channels to the user's own bridge
bots, and the bots do act externally (login/logout). The accurate claim is:
"`sendConvoMessage` is the only path that posts into a *conversation*;
`sendCmd`/`sendSecretToMgmt` post only into verified management rooms."
Docs stating "no second send path exists" (`shared/ui/chat.js:129`) overreach.

## F19 — `backfill()`'s watermark logic is misleading — CONFIRMED (minor)

**Evidence:** `uplink.py:520-523`: `end = res.get("start")` (a *backward*
pagination token) is used only as a truthiness gate, and the value actually
written is the unrelated global `meta.sync_since`. Combined with F7 (nothing
reads it), these three lines are ceremony around a value with no consumer.

## F20 — `tests/run.sh` runs 1 of the 4 unit test files — CONFIRMED (minor)

**Evidence:** `tests/run.sh` wraps only `tests/unit/consent.test.js` (in
docker). `consent_py.test.py`, `uplink_reconcile.test.py`,
`uplink_sources.test.py` must be invoked by hand (documented in
`shared/CLAUDE.md`, but the obvious entry point silently under-tests).

---

## Not verified (would require mutating state)

- **The 11 integration scenarios actually passing** — running
  `tests/integration/harness.py` writes rooms/messages to the *live* master
  homeserver (as `@alice:master`) and the synctest hub. Claim
  (`PLAN-MASTER-SYNC-IMPL.md`: "all 11 verified 2026-08-27") left as stated.
- **iMessage daemon runtime invariants** (M-1 allowlist, rate caps, echo
  ledger): code inspected (subagent sweep found endpoints/guards present, no
  dead code), but not exercised — sending test iMessages is an external send.
- **Enrollment end-to-end** (`test_enroll.py`) — mints/burns real codes against
  the live master; not run.
- **Media re-upload caps** (`UPLINK_MEDIA_MAX`) — code-read only.

## What was verified clean (worth stating)

- **JS↔Python consent parity**: both resolvers compared line-by-line — same
  precedence, same reason strings, same normalization; all three pure-logic
  test suites pass (83 JS + 80 py + 23 reconcile + 5 sources checks, run in
  this audit).
- **`apps/master` really has no message-send code**: the only Matrix writes in
  the file are one `PUT …/send/com.jkali.proposal/…` into an allowlisted
  proposals room (`main.js:772`), a marker-scoped `/join` (`:285`), and one
  POST to the enroll admin service (`:825`). The harness even asserts this
  statically (`harness.py:868-876`).
- **Power-level pinning at creation**: mirror rooms (`uplink.py:452-457`),
  proposals rooms (`:753-759`), spaces (`provision.sh:125-127`,
  `enroll.py:_create_space`) all pin the manager at PL 0/50 below
  `events_default` in the `createRoom` call itself.
- **Secrets hygiene**: `.env`, `master/.env`, `tokens.local`,
  `.provision-state.local`, `enrollments.local`, `state.db`,
  `uplink.env.local` all gitignored (verified `git check-ignore`) and mode 600
  on disk (verified `ls -l`).
- **All 25 first-party JS files and 10 Python files pass syntax checks**
  (`node --check` in the pinned node:20-alpine, `py_compile`).
- **`apps/user`**: subagent sweep found no dead exports, no unwired DOM ids,
  no half-built features; proposals funnel through the guarded send path.
