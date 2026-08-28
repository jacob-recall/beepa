# Simplification & Modularity Plan

Proposals, ordered by leverage; items marked DONE have since been executed.
Ordered by leverage
(payoff ÷ risk). Finding references (F1…F20) point into
`docs/AUDIT-FINDINGS.md`. The UI is under active development elsewhere; the
proposals below avoid prescribing visual changes.

---

## P1. Retire the legacy hub (`hub/site/`) — the single biggest win — DONE 2026-08-28
**Target:** `hub/site/app.js` (2,344 lines), `hub/site/index.html`,
`hub/site/style.css`, the `hub` service in `docker-compose.yml`,
`hub/nginx.conf` (config to be *reused*, see P2; kept in place). Finding F1.

**What:** Once `apps/user` is accepted as the daily driver, remove the `hub`
compose service and delete `hub/site/`. Keep `hub/nginx.conf` as the template
for the `views` server.

**Payoff:** ~2,700 lines deleted (≈ a quarter of all first-party code); the
system goes from *two* independent send-path implementations to one, making
the documented claim "one guarded send path" actually true; every future fix
lands once instead of twice.

**Risk:** Medium — the monolith is still the declared daily driver, and
`apps/user` must first be verified feature-equivalent (bridge command console,
QR login, session-paste flows all exist in `shared/ui/`, but daily-use parity
should be confirmed by the person who lives in it). Mitigation: flip the
default bookmark to :8011 for a week, then delete.

## P2. Give the new apps the hardened server the old one has — DONE 2026-08-28
**Target:** `docker-compose.yml` `views` service; `hub/nginx.conf` as donor.
Finding F2 (security regression).

**What:** Mount an nginx config for `views` that sends the CSP as an HTTP
header (per app path), plus `X-Frame-Options: DENY`, `Referrer-Policy:
no-referrer`, GET/HEAD-only, no-store — i.e., what :8010 already does. Keep
the `<meta>` CSP as defense-in-depth if desired, but the header must exist for
`frame-ancestors` to mean anything.

**Payoff:** Restores clickjacking protection on the consent controls and the
manager console; closes the audit's only live security-posture regression.
**Risk:** Low (config-only; test both apps still load — header CSP must match
the metas' allowances, including fonts and the :8019 connect-src).

## P3. Break the `shared/ui` import cycle so the master app can stop hand-copying read logic
**Target:** `shared/ui/render.js` (`convoResolveContent`), `shared/ui/rows.js`
(`buildPlatBadge`), the localpart/time helpers, vs. their duplicates in
`apps/master/main.js:87-165, 315-343`. Finding F14.

**What:** Extract the *pure, send-free* read-side into leaf modules with no
imports beyond `el.js`/`state.js` — e.g. `shared/ui/content.js`
(`convoResolveContent` + media-label table), `shared/ui/badges.js`
(`buildPlatBadge`, `PLATFORM_ICON/LABEL`, source-id table). `render.js`,
`rows.js`, and `apps/master/main.js` then import them. The master app keeps
its own tail loop and mirror-specific logic, but drops the copied whitelist
and badge code (~120 lines) — and, more importantly, drops the *obligation to
manually re-sync them* that its file header currently imposes.

**Payoff:** One content whitelist instead of two security-critical copies that
must be diffed by hand on every change; the "absent send code" property
becomes enforceable by a trivial rule ("master imports only leaf modules")
instead of a paragraph of prose.
**Risk:** Medium — this is security-sensitive surface (the render whitelist).
The integration harness's static scan of `apps/master/main.js`
(`harness.py:868-876`) must be extended to the new leaves, and the CLAUDE.md
guidance rewritten. Do it as its own reviewed change, nothing else in the same
commit.

## P4. Uplink hygiene: delete the dead machinery, fix the backfill gap
**Target:** `agents/uplink/uplink.py`, `agents/uplink/reconcile.py`.
Findings F3, F5, F6, F7, F8, F19.

**What (one focused pass):**
1. Delete `sync_room()` and its call loop, `mapped_ids_for_room()`, and the
   `last_synced_pos` column writes (`_set_watermark`, the `backfill()` tail
   lines) — or, if per-room resume is wanted, actually *read* the column;
   pick one, don't keep write-only state. Update the harness assertion at
   `harness.py:316` accordingly.
2. Fix F3: record a `backfill_done` flag per mirror (or insert the
   `mirror_rooms` row only after `backfill()` succeeds) so an interrupted
   backfill re-runs on the next loop.
3. Either call `reconcile.next_watermark()` where watermarks advance, or
   delete it and its tests — a tested-but-unused function misleads reviewers
   about where the delivery guarantee lives.
4. Cache `event_map` ids in memory (a set, appended on insert) instead of
   re-loading the full table on every `forward_events()` call.
5. Wrap `run()`'s loop body against local-transport errors (`URLError`,
   `TimeoutError`) with the same backoff used for `MasterUnreachable`,
   instead of crash-looping through launchd every 15s (F9).

**Payoff:** ~60 lines deleted, one real durability bug fixed, one crash mode
removed, and the daemon's actual guarantees match its comments.
**Risk:** Low-medium; the 11-scenario harness exists precisely to catch
regressions here (add a 12th: kill master mid-backfill).

## P5. Fix the enrollment manager check
**Target:** `master/enroll.py:322` (`_require_manager`). Finding F4.

**What:** `who != manager or who != "@manager:master"` → compare against the
configured manager only (`who != manager`), and drop the hardcoded literal (or
assert it once at startup with a clear error).
**Payoff:** Removes a guaranteed future lockout on any renamed/redeployed
master. **Risk:** Trivial; `tests/integration/test_enroll.py` covers the flow.

## P6. One source-of-truth for the bridge/source table
**Target:** `shared/ui/sources.js` (`SOURCES`), `agents/uplink/uplink.py:64-76`,
`apps/master/main.js:328-335`, (`hub/site/app.js:28` goes away with P1).
Finding F17.

**What:** Cheapest robust option (no build step exists): keep the four tables
but add a parity unit test — like the consent pair already has — that asserts
the Python table and the JS tables agree (a tiny JSON fixture both tests
read, or a generated-constants check). Adding bridge #7 then fails loudly
instead of silently missing a surface.
**Payoff:** Prevents the most likely future drift bug. **Risk:** None.

## P7. Make `tests/run.sh` run the whole unit suite
**Target:** `tests/run.sh`. Finding F20.

**What:** Add the three `python3 tests/unit/*.py` invocations after the docker
node run.
**Payoff:** The obvious entry point stops silently skipping ¾ of the unit
tests. **Risk:** None.

## P8. Truth-sync the small stale strings
**Targets & fixes (mechanical):**
- `tests/integration/run.sh:2` and `harness.py:4`: "eight" → "eleven"
  scenarios (F11).
- `tests/integration/harness.py:56-60`: default `SYNCTEST_STATE_DIR` →
  a repo-relative or `tempfile.mkdtemp()` path, not a hardcoded
  session-specific `/private/tmp/claude-501/...` (F12).
- `PLAN-MASTER-SYNC-IMPL.md:6`: branch note → merged to `main` (F13).
- `shared/ui/chat.js:129` ("No second send path exists") → scope the claim to
  conversation rooms; `sendCmd`/`sendSecretToMgmt` are the management-room
  path (F18).
- Root `CLAUDE.md`'s "only external send path in the whole system" → same
  rescoping (until P1 lands, also mention the legacy hub).

**Payoff:** Docs a new reader can trust. **Risk:** None.

## P9. Reduce the uplink's steady-state load
**Target:** `Uplink.run()` / `reconcile()` (`uplink.py:392, 957`).

**What:** `reconcile()` does a full `/sync` + policy read + profiles read
every ~30s loop even when nothing changed (log shows `create=0 delete=0
keep=6` every 30s). Cheaper: run the full reconcile on a slower timer (e.g.
every 5 min) *and* immediately when `tail_once()`'s sync shows a relevant
account-data change (`com.jkali.share_policy` / `share_override` /
`contact_profiles` events are already visible in that stream).
**Payoff:** ~10× fewer full syncs against the local hs; faster reaction to
consent changes (today worst-case ~30s, unchanged). **Risk:** Low; scenario 6
(revoke) guards the semantics.

## P10. Housekeeping
- Tear down the `matrix-synctest` stack when not testing (31h+ up; F15) — and
  note in `tests/CLAUDE.md` that `run.sh` could do `up`/`down` itself.
- Prune the three stale `.claude/worktrees/` checkouts (old copies of the
  monolith era; they inflate every repo-wide grep).
- Consider moving the 13 historical `PLAN*.md` files to `docs/history/` so
  the repo root presents current docs first (pure `git mv`, no content
  change).
- Align the mirrored consent suites (83 JS vs 80 py cases, F16) by porting
  the three missing cases, keeping the "mirrored tests" claim exact.

---

### What NOT to simplify (checked and deliberate)
- **The master app's separate module graph** is justified *today* (it is the
  "absent send code" guarantee); P3 shrinks the duplication without
  collapsing the boundary. Do not simply import `shared/ui/render.js` into
  the master app — that drags the send path in transitively.
- **The revocation semantics** (orphan + kick, not purge) look like a
  shortcut but are a CS-API limitation, documented in
  `agents/uplink/CLAUDE.md`; leave as is.
- **Two consent implementations (JS + Python)** are inherent to the
  architecture (browser UX + daemon enforcement); the parity tests are the
  right tool. Don't try to unify the languages.
- **The three independent sync loops** in the user app (command / feed /
  open-conversation) are an intentional isolation design (per-loop filters),
  not accidental duplication.
