# Share Logic — how a conversation actually reaches the manager

The end-to-end "share pipeline" design doc: every hop a conversation takes from
a teammate's private hub to the manager's screen, where each hop is enforced,
and the membership defect found (and fixed) on 2026-08-28 that left the
console's Teammates view dead. Companion to `docs/SYSTEM-DESIGN.md` (the
plain-language schema) and `docs/ARCHITECTURE.md`.

## The pipeline — six hops

A conversation is visible to the manager only when **all six** hops complete.
Each hop has a distinct owner and enforcement point:

| # | Hop | Owner | Mechanism | Can stall when |
|---|-----|-------|-----------|----------------|
| 1 | **Consent** — the conversation resolves to *shared* | Teammate | 4-layer resolver (`shared/model/consent.js` ↔ `agents/uplink/consent.py`), default private | Teammate never opted in (correct behavior, not a stall) |
| 2 | **Mirror creation** — a copy room appears on the master | Uplink daemon | `create_mirror()`: createRoom with manager invited at PL 0, `events_default` 50; stamps `com.jkali.source`, `com.jkali.mirror_of`, optional `com.jkali.profile` | Master unreachable (buffered + retried) |
| 3 | **Space linking** — the mirror is filed under `space:<teammate>` | Uplink daemon | `m.space.child` on the teammate's master space | — |
| 4 | **Event flow** — messages copy up, exactly once | Uplink daemon | `event_map` idempotency + watermark-after-confirm | Master outage (buffers; see AUDIT F3 for the backfill edge) |
| 5 | **Manager membership** — the manager's account *joins* the space, the mirrors, and the proposals room | **Manager console** | Auto-accepting the uplink's invites — **this hop was broken; see below** | Invites pile up unaccepted → console renders nothing |
| 6 | **Rendering** — rail + feed + per-teammate views | Manager console | `parseSnapshot`/`buildByUser`: only *joined* rooms that are children of a joined `space:*` space are listed | Hop 5 incomplete |

The reverse (proposal) path piggybacks on the same membership: the manager can
only write a suggestion into a teammate's Proposals room after joining it
(hop 5), and the teammate's uplink pulls it down regardless of console state.

## The defect (found 2026-08-28, live-verified)

**Symptom:** the Teammates rail and per-teammate views in the master console
were empty/static; new shares never appeared.

**Live evidence:** a `/sync` as `@manager:master` showed **0 joined rooms and
8 pending invites** — `space:jkali`, six mirror conversations, and the
Proposals room. The uplink had done its job perfectly (hops 1–4 all green,
6 mirrors flowing); everything was stuck at hop 5.

**Root cause (two layers):**

1. The console's only auto-join, `autoJoinProposalInvites()`
   (`apps/master/main.js`), required a `com.jkali.proposals` event inside the
   invite's `invite_state`. But Matrix **stripped invite state** carries only a
   fixed set of standard event types (`m.room.create`, `m.room.name`,
   `join_rules`, `topic`, the member events) — **custom state types are never
   included**. The gate could never fire, for any invite, on any Synapse. It
   was dead code from day one.
2. Every other membership was documented as "accepted out of band". The
   integration harness performs those joins *programmatically* with the
   manager's token — so all 11 scenarios pass while the real console, which
   has no out-of-band hand, shows nothing. The tests validated the pipeline
   while silently standing in for its missing last hop.

Additionally, the top-bar "Teammates" tab (`apps/master/index.html:47`) was
rebuilt in the Beepa artboard pass as a literal static
`<span class="tab tab-static">` — wired to nothing (the dynamic rail is the
sidebar `#nav-teammates`, which was empty because of the above).

## The fix — a strict, testable auto-join gate

What stripped invite state *does* reliably carry (verified against the live
invites) is enough to recognize every uplink-originated invite:

| Invite kind | Recognizable by (in stripped state) |
|---|---|
| Teammate space | `m.room.create` content `type: "m.space"` **and** `m.room.name` starting `space:` |
| Mirror room | `m.room.create` content carries `com.jkali.mirror_of` (a room-id-shaped string) — the uplink puts it in `creation_content`, and create content survives stripping |
| Proposals room | *Not* directly (its create content is bare) — but the uplink links it as an `m.space.child` of the teammate's space, so after the space is joined it is recognizable as a **known space child** |

So the console gains one pure predicate module (`apps/master/invites.js` — no
DOM, no network, unit-testable in node) that **both** gates share, keyed on a
single identity source: the room's `m.room.create` **sender**, which is
server-stamped and present even in stripped invite state (verified live:
room version 11, sender `@jkali:master` on every uplink invite). An invite is
auto-joined **iff**:

- (a) it is a space whose name is exactly `space:` + the *create sender's*
  localpart (`spaceLabelFor` — strict mxid validation, raw comparison, no
  fallback value of any kind), or
- (c) its room id is an `m.space.child` of an already-joined,
  identity-verified space **and** its own create sender matches that space's
  label (this joins the mirrors and the Proposals room on the second pass).

There deliberately is **no rule that joins on `com.jkali.mirror_of` shape
alone** — an earlier draft had one, and security review showed it was both
redundant (the uplink space-links every legitimate room immediately, so rule
(c) covers it within one refresh) and the design's only unauthenticated join
path, i.e. its DoS surface. Joins are capped per refresh, refused invites are
never retried eternally (session memo of hard failures), and the console
still has no leave/reject call — the write surface stays exactly
`{POST /join, PUT /send/com.jkali.proposal}`, now asserted by a widened
build-time scan of *every* JS file in `apps/master/`.

The same predicate is enforced again at render: `buildByUser` accepts only
identity-verified spaces (an unverified space is *skipped*, never given a
fallback label) and only children whose own creator matches the space label —
so a teammate can neither present another teammate's label nor capture
another teammate's rooms into their own space. Same-label duplicates merge
deterministically instead of overwriting. Skipped items surface as a visible
count rather than silent data loss. And the proposal composer now pins
`{mirror room, label, proposals room, target}` at open time and re-asserts
all four at submit, so a mid-session revocation or label change refuses the
write instead of redirecting it. (The static top-bar Teammates tab noted in
the root cause was independently replaced by the in-progress sidebar
redesign while this fix was being built; `main.js`'s tab handler is
existence-guarded so it works with either markup.)

**Why auto-join is safe here (trust analysis):**
- Joining is *membership*, never a send — the console's "absent send code"
  doctrine is untouched (the only write endpoints remain `/join` and the one
  `com.jkali.proposal` PUT), and the harness's build-time scan is widened
  from `main.js` alone to **every** `*.js` under `apps/master/`.
- On this master, registration is disabled and federation is off, so an
  invite can only originate from a provisioned teammate account.
- **The cross-teammate spoof is closed by the identity bind** (caught in
  plan review before implementation): without it, `@bob:master` could create
  a space named `space:jkali`, get it auto-joined, and — because the rail
  keys on the space *name* — inject fabricated conversations under jkali's
  label **and hijack `proposalsByUser`**, redirecting the manager's
  suggestions into a bob-controlled room. With the bind at both join and
  render time, a mislabeled space is refused twice.
- A crafted invite that fakes rule (b) only makes the console a *member* of
  a junk room: the rail renders exclusively children of identity-verified
  spaces, so it never appears in the UI. A teammate fabricating content
  under their *own* label is inherent to the sharing model (they control
  what they mirror) and is not treated as a vulnerability.

**Security-review outcome (15 findings, all dispositioned before
implementation):** the review that produced the final gate above found, and
the slice fixes: a fail-open `'unknown'` localpart sentinel; the
unauthenticated join rule; an inviter-vs-creator predicate mismatch; lossy
sanitize-then-compare; proposals-marker hijack on mirror rooms
(`isProposals` now also requires no `mirror_of`); duplicate-label overwrite;
label-resolution at submit time; missing join backpressure; silent
verification skips; and the too-narrow build-time scan. Accepted as inherent
or pre-existing: presence visibility to co-members (a `presence.enabled:
false` hardening in `master/setup.sh` is noted as follow-up), and a
teammate's ability to fabricate content *under their own label*. Deferred:
power-level shape verification of proposals rooms, and the server-side
alternative of adding the custom markers to Synapse's
`room_prejoin_state.additional_event_types`.

**Guardrails added with the fix:**
- `tests/unit/master_invites.test.js` — fixtures copied from the real
  stripped-state payloads; asserts each rule fires, arbitrary invites are
  refused, junk ids rejected. Added to `tests/run.sh`.
- `apps/master/CLAUDE.md` gains the invariant: *never* auto-join outside
  `invitesToJoin`'s gate; `invites.js` stays a pure leaf.
- Follow-up for the uplink (not in this change): stamp
  `creation_content: {"com.jkali.proposals": true}` on new proposals rooms so
  rule (b)-style direct recognition covers them too; and add a harness
  scenario that exercises hop 5 *through the console's own gate* instead of
  joining out of band — closing the masking gap that hid this bug.

## Relationship to cleanup P3

This is the functional half of P3 ("the master console's connection logic").
The structural half — extracting the render whitelist into a shared leaf
(`shared/ui/content.js`) so the console stops hand-copying security-critical
code — remains queued in `docs/SIMPLIFICATION-PLAN.md` P3 and should follow as
its own reviewed change. `invites.js` establishes the pattern both follow:
**the master app imports only pure leaf modules, and every trust decision
lives in a function a unit test can hold still.**

**Live outcome (2026-08-28):** the fix was implemented, independently
verified (unit tests, predicate probes, write-surface scan, live read-only
gate replay), and then exercised for real by driving the shipped
`invites.js` gate against the live master: pass 1 admitted exactly the
teammate space, pass 2 exactly the 7 space children, ending at **8 joined /
0 pending** — six mirrors (four WhatsApp, one Google Messages, one iMessage)
with readable history, the Proposals room, and the space. One operational
quirk observed: Synapse caches identical initial-sync responses for ~2
minutes, so right after a join the console's next snapshot can briefly show
the pre-join state — the multi-pass loop then simply finishes the remaining
joins on a later 20-second refresh. Harmless, but expect first-connect to
take a cycle or two rather than being instant.

## How to check the pipeline end to end (operator runbook)

1. Teammate hub: `tail -f agents/uplink/logs/uplink.log` — expect
   `reconcile: create/delete/keep` lines; `keep=N` is your shared count.
2. Master state (read-only, as the manager):
   `/_matrix/client/v3/sync` — pending `invite` entries mean hop 5 hasn't run
   (open the console once); `join` entries with `space:*` + mirrors mean the
   pipeline is fully live.
3. Console: Teammates rail shows each teammate with a conversation count;
   opening a room shows history sorted by `com.jkali.origin_ts`, live-tailed.
4. Un-share test: flip one conversation to Private in the teammate app —
   within ~30s the mirror disappears from the console (revocation = unlink +
   kick + leave).
