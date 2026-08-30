> ARCHIVED (2026-08-30): historical planning doc, superseded — kept for reference only.

# PLAN-MASTER-SYNC-IMPL — Implementation Plan

Executes the design in **PLAN-MASTER-SYNC.md**. Status: **COMPLETE —
Phases 1–5 (V1 foundation, V1 master+uplink, V1.5, V2 proposal channel,
unified contacts) all built and verified** (2026-08-27). Branch:
`feat/master-sync`. No push (orchestrator commits after verification).

Verified end state: `shared/` core (consent + contacts models, render
whitelist, sync primitives) shared by `apps/user/` (share controls, consent
panel, contacts UI, proposal inbox) and `apps/master/` (read-only,
per-user/per-profile grouping, the one narrow proposal-write path);
`agents/uplink/` mirrors consent-approved conversations up and proposals
down with watermark/event-map exactly-once delivery, `consent.py` kept in
byte-parity with `shared/model/consent.js`; `master/` provides the always-on
homeserver, provisioning, and v1.5 enrollment-code flow; `tests/` covers
consent-resolver + reconcile-logic unit tests and all 11 integration
scenarios in `tests/integration/harness.py` (see `tests/CLAUDE.md`) plus the
enrollment-flow test. Per-directory `CLAUDE.md` files documenting each area
are in place per the "Cross-cutting: documentation & tests" section below.

## Ground rules (apply to every task)

- **Non-destructive to the daily driver.** `main` holds the working hub; all
  work is on `feat/master-sync`. `git checkout main` must always restore a
  working single-user hub.
- **Preserve security invariants** from PLAN-HUB.md: strict CSP + Trusted Types,
  `frame-ancestors 'none'`, textContent-only rendering (no `innerHTML`), mxc/room
  validation, mgmt-room verification before sends, secrets 600 / dirs 700. The
  refactor must not weaken any of these; a diff of the CSP and the rendering
  whitelist is part of acceptance.
- **Master is a separate local stack** (compose project `matrix-master`, its own
  homeserver, own ports) reachable over the network — treated as the remote
  always-on server. The uplink connects to it by a configurable base URL.
- **Model routing (UltraCode / pilotfish):** recon → Haiku(low); implementation
  & tests → Sonnet(medium); **security-sensitive** (consent/authorization, token
  handling, cross-user isolation, CSP preservation, revocation) → Opus(high);
  adversarial verification → Opus(high), always a *fresh* agent that did not
  write the code.
- **Every phase ends verified.** A phase is "done" only when its acceptance tests
  pass on the running stacks; otherwise it is reported partial with the exact
  failure.

## Repo target layout (end state)

```
messaging-hub root (= this repo)
  shared/     ui/(el,sanitize,renderer,convo-row,chat-view,search)  matrix/(client,sync)  model/(consent,contacts)
  apps/
    user/     index.html main.js  (+ share controls, consent panel, composer, per-source pages)
    master/   index.html main.js  (read-only; per-user grouping; NO composer)
  agents/
    imessage/ (existing)
    uplink/   NEW mirror-up daemon (+ its own sqlite state, tests)
  bridges/    docker-compose.yml (user stack, existing)
  master/     docker-compose.master.yml + master homeserver config + provisioning scripts
  tests/      integration harness (spins both stacks, drives scenarios)
  CLAUDE.md files: root + one per major dir
```
`hub/site/` stays serving until `apps/user/` reaches verified parity, then nginx
is repointed and `hub/site/` is retired (git-preserved).

---

## Phase 1 — Foundation (shared/ + consent) — **V1 part A**

**P1.1 Recon (Haiku).** Map `hub/site/app.js` seams with line ranges: `el`,
`sanitize`/`sanitizeLine`, `renderMessageEvent`, convo-row/`buildConvoRow`,
`openConvo`/chat view, the three sync loops, `SOURCES`, `buildConnections`,
search, account-data helpers. Output: a seam map.

**P1.2 shared/ extraction (Opus — security-sensitive: preserves CSP/renderer).**
Carve the seams into `shared/ui/*`, `shared/matrix/*` as native ES modules
(no bundler). Create `apps/user/{index.html,main.js}` that imports them and
reproduces the current hub exactly. Do NOT touch `hub/site/` yet. CSP + Trusted
Types + the render whitelist move verbatim.

**P1.3 Consent model (Opus — security-sensitive: authorization boundary).**
`shared/model/consent.js`: the tri-state resolver (per-conversation > per-source
> global > private) exactly per spec §4. Pure function `effectiveShared(convo,
policy, override)`. Storage helpers reading/writing `com.jkali.share_policy`
(account-data) and `com.jkali.share_override` (room account-data).

**P1.4 User share UI (Sonnet).** In `apps/user/`: global "Share All" switch,
per-source "Share all <source>" switch, per-row tri-state toggle showing
effective state + reason, and the consent summary panel (spec §4.2). Reuse
existing search. textContent-only, no CSP change.

**P1.5 Unit tests (Sonnet).** `tests/unit/consent.test` — every precedence
combination, including standing-policy auto-share of a new conversation and
per-conversation exclusion overriding a share-all.

**P1.6 Verify (Opus, fresh).** `node --check` all modules; consent unit tests
pass; `apps/user/` renders identically to `hub/site/` (structural diff of DOM
output for a sample room set); CSP + render-whitelist byte-unchanged.

**Acceptance:** apps/user works standalone == current hub; consent resolver
correct on all cases; zero security-invariant regression.

---

## Phase 2 — Master + uplink (the core) — **V1 part B**

**P2.1 Master stack (Opus — security-sensitive: isolation/hardening).**
`master/docker-compose.master.yml`: a second Synapse (`server_name: master`,
own DB, own port) + Postgres. Registration disabled, TLS-ready (self-signed for
local, treated as remote), only CS API exposed. Provisioning script:
create `@manager:master` + per-user accounts + issue scoped tokens; create a
per-user space; auto-invite `@manager` read-only.

**P2.2 Uplink agent (Opus — security-sensitive: tokens, one-way boundary,
idempotency).** `agents/uplink/`: headless daemon (stdlib, mirrors the iMessage
daemon's style). Responsibilities per spec §5.4/§8.2:
- read consent policy + overrides from the local homeserver;
- resolve effective-shared set; reconcile (create/backfill/tail mirror rooms;
  delete on revoke);
- forward messages/edits/redactions to the master, preserving
  `com.jkali.from_me`, `com.jkali.origin_ts`, sender name, source tag;
- SQLite `mirror_rooms` + `event_map` for watermark + idempotency;
- outbound-only client to the master base URL; buffer + backoff when master
  unreachable; advance watermark only on confirmed delivery.

**P2.3 Master app (Sonnet — reuses shared/).** `apps/master/`: read-only client
against the master homeserver; cross-user recent feed (reuse Home feed),
per-user spaces (reuse per-source view grouped by user), search across all;
**no composer**. Renders mirror events via the shared renderer, ordering by
`com.jkali.origin_ts`.

**P2.4 Integration harness (Sonnet).** `tests/integration/`: brings up user
stack + master stack; scripts scenarios; asserts on the master homeserver state.

**P2.5 Adversarial verification (Opus, fresh) — the edge cases the user named.**
Run and confirm:
1. Share a conversation → mirror room on master with correct history, order,
   alignment, source badge.
2. New local message → appears on master within a sync window.
3. **User offline → messages sent → user back online → catch-up** with no gaps,
   no duplicates (kill the uplink + local homeserver, inject messages via the
   bridge/local events, restart, verify watermark resume). This is the headline
   edge case.
4. **Master offline → user sends → master back → delivery** (buffer + resume).
5. Share-all iMessage → all current + a newly-arrived iMessage room mirror up;
   a per-conversation `private` exception stays out.
6. Revoke at each level → master copy deleted.
7. Manager cannot send: power-level rejection AND no composer present.
8. Cross-user isolation: user A's token cannot write user B's rooms.

**Acceptance:** all eight scenarios pass on the running two-stack setup.
**This is the definition of V1 done.**

---

## Phase 3 — V1.5

**P3.1 Media re-upload (Opus — handles content, mxc rewrite).** Uplink downloads
media from the local homeserver and re-uploads to the master, rewriting `mxc`;
master app renders real media. Fallback to placeholder on failure.
**P3.2 Enrollment flow (Opus — token exchange).** Replace manual token handoff
with a one-time enrollment code the uplink exchanges for a scoped token via a
small master provisioning endpoint.
**P3.3 Verify (Opus, fresh):** media round-trips and renders; enrollment issues
a correctly-scoped token; no regression in Phase-2 scenarios.

---

## Phase 4 — V2 proposal channel

**P4.1 Proposal model (Opus — reverse channel, still user-owns-send).** Manager
composes a proposed message / reusable "class of message" into a proposal event
in the mirror room (or a per-user proposal room). Uplink `/sync`s the master for
proposals (user-initiated outbound) and surfaces them in `apps/user/`.
**P4.2 Approve/edit/send UI (Sonnet) in apps/user:** the teammate reviews,
edits, and **sends from their own account** via the existing local send path.
Master never sends externally.
**P4.3 Master compose-proposal UI (Sonnet) in apps/master:** the one place the
manager can "write" — a proposal, not a send; clearly labeled.
**P4.4 Verify (Opus, fresh):** proposal syncs down; teammate edits + sends;
message goes out via the teammate's account only; master has no external send
path; power-level/composer invariants intact.

---

## Phase 5 — Unified contacts (final feature)

**P5.1 Contact model (Opus — becomes the sharing/routing unit).**
`shared/model/contacts.js`: a `ContactProfile` object linking multiple
conversations (across sources/rooms) to one person; stored in account-data
(`com.jkali.contact_profiles`). Linking is user-curated (manual link/unlink),
with optional suggestions (same display name / handle) — suggestions only,
never auto-merge.
**P5.2 Contact management UI (Sonnet) in apps/user:** create a profile, search
and attach conversations to it (reuse search), view all of a person's threads
in one place, unlink.
**P5.3 Profile-level sharing (Opus — extends consent).** Sharing a profile shares
all its linked conversations (a new consent scope layered above per-conversation
but below explicit per-conversation `private` — precedence documented).
**P5.4 Master rendering (Sonnet):** the master groups a shared profile's threads
under one person across platforms.
**P5.5 Verify (Opus, fresh):** create profile across ≥2 platforms; share the
profile → all linked threads mirror up under one master profile; per-conversation
`private` still excludes; unlink/unshare behave correctly.

---

## Cross-cutting: documentation & tests

- **CLAUDE.md per directory** (Sonnet, after each phase touches a dir): root +
  `shared/`, `apps/user/`, `apps/master/`, `agents/uplink/`, `master/`,
  `tests/` — each stating what lives there, the security invariants, how to run
  it, and how a model should make changes safely.
- **Test suite** runnable via a single `tests/run.sh`: unit (consent, contacts,
  uplink idempotency) + integration (the eight Phase-2 scenarios + phase 3–5
  additions). Every edge case the user named is an explicit, named test.
- **Checkpoint commit** on `feat/master-sync` after each phase's verification
  passes, message stating what's verified. No push.

## Definition of done for the run

V1 (phases 1–2) fully working and all eight edge-case scenarios green on the
two-stack setup, documented; then V1.5, V2, unified contacts completed in order
as far as correctness allows, each gated on its verify step. Final report states
exactly which phases are verified-complete, which are partial (with the failing
test), and which were not reached.
