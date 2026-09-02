# Plan: explicit per-conversation share levels with a new "Direct" level

**ID:** direct-share-level
**Date:** 2026-09-02
**Status:** r5 — three verifier rounds complete (all blockers dispositioned
FIX and folded in; round-3's three were mechanical completions applied without
a further round per review-cap policy); security-review findings integrated
with dispositions (§Security review). PRESENTED FOR USER APPROVAL.

## Outcome

A teammate sets each conversation to exactly one of three explicit levels:

| Level | Mirrored to master | Manager proposals |
|---|---|---|
| `private` (default, unset) | no | n/a (master never sees it) |
| `share` | yes | land in teammate's proposal inbox; teammate reviews and clicks send (today's behavior) |
| `direct` (NEW) | yes | teammate's **uplink auto-sends** the message into the conversation on arrival — no review click |

The per-conversation "Auto (inherit)" state is **removed**: conversations no
longer inherit from contact-profile / per-source / global policy. An unset
conversation is simply `private`. (User decision 2026-09-02.)

**Honest security posture (changed, deliberately):** the master still holds no
teammate credentials, but once any conversation is `direct`, the master-side
manager identity becomes a **remote send capability on the teammate's real
messaging accounts for those conversations**, executed by teammate-side code
behind the gates in D2. A compromise of the manager session or master
homeserver therefore means "can send as the teammate into `direct` rooms" —
a new risk that `share` never had. This is stated in the consent UI copy the
teammate sees before enabling `direct` (D3), and every gate below exists to
bound it. The recipient of an auto-sent message cannot distinguish it from
the teammate typing — accepted residual risk, also stated in the UI copy.

## Non-goals

- Contact-sharing consent (`resolve_contact_share`, per-source contact policy)
  is untouched.
- Person-targeted proposals (start-new-chat) are NEVER auto-sent.
- No master-side sending capability of any kind beyond the above framing.
- No changes to bridges, the iMessage daemon, mirror-room power levels, or the
  `from_me` render gate (any provenance field stays cosmetic — F14).

## Design

### D0. Migration: materialize inherited shares (runs with S1)

One-time, guarded by a `migrated_explicit_levels` flag in the uplink's
state.db:

- For every room the OLD resolver (inherit semantics) resolves shared AND that
  is **currently mirrored**, with no explicit per-room override: write an
  explicit `'share'` override into the teammate's account-data overrides map,
  with a `migrated: true` field in the event content.
- The teammate UI (S2) surfaces a one-time "review migrated shares" list of
  exactly these rooms, so converting a revocable standing policy into explicit
  overrides is **surfaced, never silent** (F11 disposition).
- **Ordering (verifier r3):** the migration pass runs to completion (and sets
  `migrated_explicit_levels`) BEFORE the first reconcile evaluates deletions
  under the new resolver; no `delete_mirror` may execute during the migration
  pass. S1 acceptance: starting from a pre-S1 state.db with a
  standing-policy-shared, currently-mirrored room, S1 end-to-end yields an
  explicit `migrated: true` `'share'` override AND the same `master_room_id`
  in `mirror_rooms` (no delete/re-create; zero `delete_mirror` calls
  observed).
- S1 also: forces an uplink restart (F7) and writes a model-version marker
  (`com.jkali.consent_model = 2`) into local account-data; the new three-level
  UI renders only when the marker is present, showing an "updating…" banner
  otherwise, so the UI can never claim Private while an old daemon still
  inherits (F7).
- **S1 also guards the OLD UI in the same slice** (verifier r2): when the
  marker is present, the shipped consent surface hides/disables the dead
  controls — the "Auto (inherit)" cycle position, the per-source conversation
  cycle, and the global Share-All toggle — and shows an "updating to explicit
  levels…" banner, so the old UI can never display a room as shared on the
  strength of a standing policy the daemon has stopped honoring. (The full
  three-level surface still ships in S2.)

### D1. Consent model (`shared/model/consent.js` + byte-parity `agents/uplink/consent.py`)

- Conversation override values: `'share' | 'direct' | 'private'`; absent or
  **any unrecognized value** = `'private'` under the NEW model — a stated
  invariant with its own conformance vector class (F8).
- `resolve()` for conversations: `override in ('share','direct')` → shared,
  else private. Profile/source/global dropped from the conversation path
  (kept for contacts). Profile `share` copy in apps/user updated so nothing
  implies it still affects conversation mirroring (F13).
- New `effective_level(override)` → `'private' | 'share' | 'direct'`.
- **Compat correction (F8):** OLD code treats `'direct'` as *inherit* (junk
  override → key deleted → falls through to profile/source/global), NOT as
  private. See Rollback for the consequence; a curated test pins this exact
  behavior so partial rollbacks are caught.

### D2. Uplink auto-send (`agents/uplink/uplink.py`)

The single deliberate exception to the "only `sendConvoMessage` sends into a
conversation" invariant. Gate order per F10; ALL must pass, any failure at
1–6 → actionable inbox forward:

1. **Sender verification (F1):** server-stamped `ev["sender"] ==
   cfg.manager_mxid`, freshly resolved. Events from `cfg.master_user` (the
   teammate's own PL-100 scoped account) are explicitly refused for
   auto-send. `created_by` is never used for any trust decision and is pinned
   cosmetically to the server-stamped sender (F16).
2. **Shape + send-grade sanitization (F2):** existing field whitelist, plus:
   body clamped to 8000, control/bidi/zero-width stripped (same class as
   `shared/ui/el.js sanitize()`), and any body whose first non-whitespace
   character is `!` is refused (bridge-command injection defense — required
   regardless of how the mautrix command-scope hypothesis resolves; S5 also
   settles it empirically on the throwaway stack, never `matrix-wa`).
3. **Freshness / replay bound (F3):** auto-send only for events that arrived
   on an *incremental* sync (`since` present) with `origin_server_ts` within
   10 minutes. Cold start (no `proposal_sync_since`, or empty
   `proposal_map`): the entire first batch routes to the inbox.
4. **Positive target check (F9):** proposal is room-targeted and the target
   is a row in `mirror_rooms.local_room_id` (membership in the mirrored set —
   a positive check, not a management-room denylist).
5. **Fresh consent point-read (F10):** `GET` the single
   `com.jkali.share_override` account-data event for the target at send time;
   `effective_level == 'direct'` required; any error → not direct.
6. **Persisted rate cap:** `DIRECT_SEND_ROOM_HOURLY = 20` (env
   `UPLINK_DIRECT_SEND_ROOM_HOURLY`), rolling 3600s per target room, counter
   in state.db `direct_send_log(ts, room_hash)` — survives KeepAlive restarts
   (F9).
7. **Record intent, then send idempotently (F4):** write
   `proposal_map` state `attempted` BEFORE the PUT; send with deterministic
   txn id `autosend_<master_event_id>` so an ambiguous retry is HS-idempotent;
   then record `sent` (or `fallback`).
8. **Outcome record:** after a successful send the uplink forwards ONE
   non-actionable record into the local proposals room — the proposal content
   plus `com.jkali.auto_sent: true` and the local sent event id — which
   apps/user classifies as history **from event content, not localStorage**
   (F5). Exactly one inbox artifact per proposal.
9. **Failure split (F4):** pre-dispatch failures → normal actionable draft.
   Post-dispatch ambiguous failures (PUT dispatched, outcome unknown) → inbox
   row explicitly labelled "may already have been sent — check the
   conversation", never a plain pending draft.
10. **Durable audit (F15):** state.db table `direct_send_audit(ts,
    master_event_id, room_hash, outcome)` — hash-only, no body — in addition
    to the log line.
11. **Master-identity binding (F12):** `master_proposals_room`,
    `proposal_sync_since` and `proposal_map` are stored keyed to
    `(master_hs, master_user, manager_mxid)`. Any change invalidates them
    (cold-start rule applies) AND suspends auto-send until the teammate
    re-confirms via a local account-data ack surfaced in apps/user.

The auto-sent `m.room.message` carries a cosmetic
`com.jkali.auto_sent_from_proposal: <master_event_id>` field so apps/user can
badge the bubble and the mirror carries it up; it is forgeable by anything
holding the teammate token and must never feed the `from_me` gate (F14).

**Docs and room copy in the same slice:** update `agents/uplink/CLAUDE.md`,
`apps/user/CLAUDE.md`, and the root `CLAUDE.md` master-sync line — no doc may
still assert the old absolute ("uplink never sends", "proposals are never
instructions to send"). Additionally (verifier r2): the two room topics the
uplink writes assert the same absolutes — the local proposals room ("Review
each one and send it yourself — nothing here is sent automatically.") and the
master proposals room ("Suggestions only — nothing here is ever sent
externally."). Both strings are rewritten to state the `direct` exception,
and because topics are set only at room creation, S3 performs a **one-time
topic re-PUT** on already-created proposal rooms (guarded by a state.db
flag). Acceptance: grep of `agents/uplink/uplink.py` finds neither absolute;
an existing-install run updates both topics exactly once.

**state.db schema migration in the same slice (verifier r2):** the daemon's
schema init is CREATE-IF-NOT-EXISTS only, so S3 adds a versioned migration
(sqlite `user_version`): rebuild/ALTER `proposal_map` to carry the outcome
state (`attempted`/`sent`/`fallback`), and create `direct_send_log` +
`direct_send_audit`. Runs idempotently against a pre-existing teammate
state.db; a test opens a DB created by the pre-S3 schema, runs the S3 code,
and drives one inbox-forward and one auto-send with no sqlite error.

### D2b. Level stamping on mirrors (reconcile)

`desired_shared` returns per-room **level**; reconcile derives
create/delete/keep from `level != private` and re-stamps kept mirrors whose
level changed (promotion and demotion): state event `com.jkali.share_level`,
state_key `""`, `{ "level": ... }`, written at create and re-PUT on change;
last-stamped level tracked in state.db.

### D3. apps/user

- Cycle becomes `share → private` only. **`direct` is NOT a cycle position
  (F6):** it is a separate explicit control (e.g. a "Direct" toggle revealed
  behind the share state) requiring a typed/modal confirm whose copy states:
  manager messages will be sent as you without review; a master/manager
  compromise can send as you into this conversation; recipients cannot tell
  the difference. No pass-through write of `direct` can occur.
- Remove per-source conversation-policy cycle + global conversation toggle
  (dead under explicit model). Contact-share controls stay; profile copy
  updated (F13).
- Bulk action per source ("set all conversations to…"): offers `share` /
  `private` only — **never `direct`** (F11); never overwrites an existing
  explicit `private` without listing the affected conversations in the
  confirm (F11).
- Proposal inbox: classifies `com.jkali.auto_sent: true` records as
  non-actionable history from event content (F5); renders the
  "may already have been sent" ambiguous state distinctly (F4); shows the
  one-time migrated-shares review list (D0).
- **Master-identity re-confirm surface (owns D2.11's ack; verifier r3):**
  when the uplink has suspended auto-send after a master-identity rebinding,
  apps/user renders a re-confirm affordance stating the new identity; the
  teammate's ack writes the local account-data event the uplink requires to
  resume auto-send. Ships in S2 so the surface exists before auto-send
  activates in S3.
- Conversation view badges auto-sent bubbles from the cosmetic field (F14).

### D4. apps/master

- No change to the write path.
- UI reads `com.jkali.share_level` (D2b); `direct` rooms label the draft
  affordance "Send" instead of "Propose". Stale/absent stamp → "Propose"
  (under-promise only).

### D5. Tests

- Consent: three-level explicit model in both languages; unknown-override ⇒
  private vector class (F8); curated old-model test pinning `'direct'` ⇒
  inherit under old inputs (F8); junk-value key-deletion behavior pinned.
- Conformance vectors regenerated (`tests/conformance/consent_conformance.py`
  remains parity authority).
- `tests/unit/uplink_proposals.test.py` additions: sender mismatch;
  `master_user` as sender refused; `created_by` spoof ignored; `!`-prefixed
  body refused; oversized/control-char body sanitized or refused; cold-start
  batch (no `since` / empty map) → inbox only; stale `origin_server_ts` →
  inbox; non-mirrored target → inbox; over-cap → inbox; cap survives restart;
  ambiguous-send crash between PUT and commit → labelled ambiguous row, no
  duplicate send (deterministic txn asserted); `direct → private` flip
  between pull and send → inbox; identity rebinding suspends auto-send until
  ack; exactly one inbox artifact per proposal.
- apps/user UI (S2 acceptance): `auto_sent`/ambiguous classification from
  event content (localStorage-independent); bulk-action level set + explicit-
  private overwrite confirm; manual checks for direct-confirm reachability
  and risk copy.
- Schema migration: pre-S3 state.db opened by S3 code drives inbox-forward
  and auto-send without sqlite error; topic re-PUT happens exactly once.
- Reconcile: level flip re-stamps `com.jkali.share_level`; demotion covered.
- Migration: standing-policy-shared room ends with explicit `migrated: true`
  `'share'` override and intact mirror; review list shows exactly those.
- Integration: update standing-policy scenarios; new scenario — direct
  round-trip (master proposal → uplink auto-send → message in conversation
  with cosmetic field, inbox holds one non-actionable record); F2 empirical
  probe: send `!wa help` from the teammate account into a WhatsApp portal on
  the synctest stack and record the observed bridge behavior.

## Slices (in order — UI lands BEFORE auto-send activates, per F5)

1. **S1 consent core + migration:** D0 + D1 both languages, uplink restart +
   model marker, unit/conformance/migration tests. (security-executor)
2. **S2 teammate UI:** D3 — three-level rendering gated on model marker,
   direct confirm control, inbox `auto_sent` classification, migrated-shares
   review, bulk action. Ships while auto-send does not exist yet, so every
   proposal is still an ordinary draft.
   **S2 acceptance (verifier r2):** node unit tests alongside the existing
   proposal tests covering: `com.jkali.auto_sent` and ambiguous records
   classified as non-actionable/labelled **from event content** (a fresh
   browser profile / empty localStorage classifies identically); bulk-action
   writes only `share`/`private` and refuses to overwrite an explicit
   `private` without the listed confirm; the re-confirm affordance renders
   when the suspension flag is set and its ack (written through the UI, no
   hand-edited account-data) clears suspension; plus a stated manual check
   that the share cycle cannot reach `direct` (only the separate confirm
   control can) and that the confirm copy contains the
   impersonation/compromise risk statement. S2 fails acceptance if any of
   these does not hold.
3. **S3 uplink gate + stamping + docs:** D2 + D2b + CLAUDE.md updates +
   uplink/reconcile tests. Auto-send activates only here, after the UI can
   already render its records. (security-executor)
4. **S4 master UI:** D4. **Acceptance (verifier r3):** a node unit test
   alongside the existing master tests asserts the draft-affordance label
   selection: level `direct` → "Send"; level `share`, absent stamp,
   unrecognized/junk stamp, or stale read error → "Propose". S4 fails if any
   missing/junk case yields "Send".
5. **S5 integration:** harness updates, direct round-trip scenario, F2 probe;
   full `tests/run.sh` + integration pass.

## Rollback

Per-slice commits. Reverting S3 disables auto-send entirely (inbox path is
the preserved fallback; the topic re-PUT and schema migration are harmless
residue). **S1 and S2 revert as a single unit together with clearing the
model marker** (verifier r2): reverting S1 alone would leave
`com.jkali.consent_model = 2` and/or the three-level UI reading a restored
inherit-semantics daemon — the exact misrepresentation F7 guards against —
and would leave the teammate without the per-source/global controls the
reverted daemon has resumed honoring. The unit revert therefore: reverts S2's
surface (restoring the standing-policy controls), reverts S1's resolver, and
downgrades/clears the marker in the same step. F8-corrected caveats stand:
(a) materialized explicit `'share'` overrides remain and are honored
identically by old code; (b) a stored `'direct'` override under old code is
treated as *inherit* — if old standing policies still exist in account-data,
such a room resolves **shared** (it was mirrored under `direct` anyway, so
exposure does not widen beyond what the teammate opted into; review-free
semantics are gone because auto-send code left with S3). No rollback path can
auto-send, and no configuration may pair a three-level UI with an
inherit-semantics daemon.

## Security review — findings and dispositions

Reviewer: pilotfish:security-reviewer, 2026-09-02 (read-only, pre-approval).

| # | Sev | Finding (abridged) | Disposition |
|---|-----|--------------------|-------------|
| F1 | P0 | No sender/provenance verification on proposal path; PL-100 master_user and impersonators become send oracles under `direct` | Mitigated-by-D2.1 (server-stamped sender == manager_mxid, master_user refused, created_by cosmetic-only); threat-model reframing added to Outcome |
| F2 | P0 | Sanitization inadequate for sent text; bridge-command (`!wa`) injection hypothesis | Mitigated-by-D2.2 (clamp, control/bidi strip, leading-`!` refusal) regardless of hypothesis; empirical probe in S5 |
| F3 | P1 | state.db loss replays up to 100 historical proposals as real sends | Mitigated-by-D2.3 (incremental-sync + 10-min freshness; cold start → inbox) |
| F4 | P1 | Ambiguous post-dispatch failure becomes human double-send | Mitigated-by-D2.7/D2.9 (intent-before-PUT, deterministic txn, labelled ambiguous row) |
| F5 | P1 | S2-before-S3 ordering created a double-send window; localStorage-only classification | Mitigated-by-reordering (UI before auto-send) and D2.8 (marker in event content) |
| F6 | P1 | `direct` reachable by pass-through slider tap | Mitigated-by-D3 (non-cycle explicit control + confirm) |
| F7 | P1 | UI/daemon model skew during migration misrepresents Private | Mitigated-by-D0 (forced restart + model-version marker gating UI) |
| F8 | P2 | Plan's downgrade claim wrong: old code treats `'direct'` as inherit, not private | Accepted-and-corrected (D1 compat note, Rollback rewrite, curated pin test) |
| F9 | P2 | Mgmt-room denylist has no primitive; cited cap constant didn't exist | Mitigated-by-D2.4 (positive mirror-set check) and D2.6 (new persisted constant) |
| F10 | P2 | Gate ordering; stale consent at send time | Adopted wholesale as D2's order; fresh point-read in D2.5 |
| F11 | P2 | Bulk action / migration can widen or silently make standing policy permanent | Mitigated-by-D0 (migrated flag + review list) and D3 (bulk: no direct, no silent private overwrite) |
| F12 | P2 | Master-identity rebinding doesn't invalidate proposal state | Mitigated-by-D2.11 (identity-keyed state, suspend until teammate re-ack) |
| F13 | P2 | Profile `share` copy misleads after inheritance removal | Mitigated-by-D1/D3 copy updates |
| F14 | P3 | Recipient can't distinguish auto-sent (inherent); provenance field forgeable | Accepted-residual-risk, stated in UI copy; cosmetic local badge added, never touches `from_me` gate |
| F15 | P3 | Audit only in rotating log | Mitigated-by-D2.10 (state.db audit table) |
| F16 | P3 | `created_by` spoofable | Mitigated-by-D2.1 (pinned to server-stamped sender) |

## Risks / open points

- Losing standing policies is deliberate; mitigated by D0 materialization
  (surfaced) + D3 bulk action.
- The F2 mautrix command-scope hypothesis stays open until the S5 probe; the
  defense ships regardless.
- Impersonation residual risk accepted and disclosed in the teammate's
  consent copy (F14).
