# agents/uplink/ — the outbound-only mirror-up daemon

A headless background service that runs on the teammate's own machine
(sibling to the iMessage daemon), mirrors *shared* local conversations up
to the master homeserver, and pulls proposal suggestions back down into a
dedicated local room. Python 3.9+ stdlib only (`urllib` + `sqlite3`) — no
pip dependencies. PLAN-MASTER-SYNC.md §5.4/§7/§8; PLAN-MASTER-SYNC-IMPL.md
Phase 2/3/4.

**This daemon can send** — in exactly one case. A conversation the teammate
has explicitly set to the `direct` level auto-sends manager proposals into
that conversation, with no review click
(`docs/superpowers/plans/2026-09-02-direct-share-level.md`, D2). Every other
level, and any failed gate, still lands in the teammate's proposal inbox as
an ordinary draft they send themselves. The honest posture that follows: once
any conversation is `direct`, the manager identity on the master is a **remote
send capability on the teammate's real messaging accounts for those
conversations**, executed here. The master still holds no teammate credential;
what bounds the capability is the gate list under "Security invariants".

## What lives here

- `uplink.py` — the daemon (`Uplink` class + `main()`). Two transports:
  `self.local(...)` (reads the teammate's LOCAL homeserver as the
  teammate's own account) and `self.master(...)` (writes the MASTER
  homeserver as the teammate's *scoped* master account; any transport
  failure against master raises `MasterUnreachable` so the caller buffers).
  Durable archive support now lives in `durable_sync.py`. The scheduler runs
  independent retry stages: revocation, retired-destination cleanup, reconciliation,
  local ingestion, live delivery, proposal pull, one history page, one media retry,
  and contacts on their own cadence. A contact-store failure cannot stop messages.
  `tail_once()` commits source event IDs/gap jobs before advancing `sync_since`;
  remote delivery has a separate confirmed ledger. Thus ingestion continues when
  master sleeps. Never restore the old global-watermark-frozen assumption.

  SQLite state remains mode 600. Existing `event_map` is a compatibility archive
  consulted ONLY for explicitly adopted old mirror rooms. `delivery_map` is keyed
  by `(master_room_id, local_event_id)`; each re-share creates a new generation.
  `mirror_lifecycle` retains allocation/link/revocation stages; `pending_events`,
  `history_jobs` and `history_refs` contain source references/cursors, not bodies.
  Failed pagination has an explicit `incomplete` outcome. Empty pages with new
  tokens continue; repeated tokens do not silently become completed history.
  `delivery_attempts` records credential fingerprints before archive dispatch;
  a lost response followed by token rotation becomes visible ambiguity instead
  of assuming Matrix transaction deduplication spans tokens. `media_retry` tracks
  transient blob failures and edits an existing placeholder when retrieval succeeds.
  `retired_mirrors` preserves cleanup credentials for an old organization separately
  from the newly active pairing. Nothing reuses those credentials to send content.

  `proposal_map`, `direct_send_log` and `direct_send_audit` are the separate
  **Direct safety ledger** and survive authority/epoch changes. An authority change
  still drops proposal discovery/cursors, triggers a cold start and suspends Direct
  until the existing acknowledgment gate passes. Archive data epoch changes do not
  change the current authority or reset sending evidence/rate counters.
  Optional `master_authority_id`, `master_data_epoch`, `master_enroll_url` metadata
  supports same-scoped-account recovery. It requires retained per-install recovery
  material and HTTPS (or loopback tests). Empty/disabled master_link is an explicit,
  persisted disconnect; only a truly absent legacy record can adopt env credentials.
  Never fall back to environment credentials after a persisted disconnect.

- `consent.py` — pure Python port of `shared/model/consent.js`. **Must stay
  byte-parity with it** — same explicit three-level conversation model
  (`share`/`direct` mirror, absent-or-unrecognized is private, nothing
  inherited), same reason strings, same normalization; the layered,
  most-specific-wins precedence survives only in the separate
  contact-sharing dimension. This is what the daemon actually enforces at
  runtime, so a drift here is a shipped authorization bug, not just a UI
  inconsistency. Tested against `tests/unit/consent.test.js`'s cases via
  `tests/unit/consent_py.test.py`.
- `reconcile.py` — pure logic, no I/O: `reconcile_decisions()` (create /
  delete / keep sets from desired-shared vs. existing mirrors, over per-room
  LEVELS via `level_is_shared()` — never bare truthiness, under which the
  string `'private'` would read as shared), `plan_level_restamp()` (which
  kept mirrors need their `com.jkali.share_level` re-PUT, a diff in both
  directions), `select_new_events()` (the idempotency filter over `event_map`),
  `next_watermark()` (advance only when `confirmed=True` — i.e. only after
  the master has 200'd), and `plan_contact_mirror()` /
  `select_contacts_to_tombstone()` (the contact mirror's per-pass diff —
  the consent gate for address-book PII, see the invariant below).
  Unit-tested in `tests/unit/uplink_reconcile.test.py`.
- `enroll_client.py` — teammate-side half of the v1.5 enrollment flow
  (`master/enroll.py` is the master side). Exchanges a one-time code for
  scoped master credentials over the loopback/TLS-fronted exchange
  endpoint and writes them to a mode-600 shell-sourceable env file
  (`MASTER_HS_URL`/`MASTER_USER`/`MASTER_TOKEN`/`MANAGER_MXID`/
  `MASTER_SPACE`) that `uplink.py` consumes directly.

## Security invariants (do not weaken)

- **Outbound-only, one-way (up).** Two `urllib` clients, no server socket
  anywhere in `uplink.py`. Events flow LOCAL → MASTER only for
  conversations; the only DOWN direction is the v2 proposal pull, which is
  itself teammate-initiated outbound (the uplink `/sync`s master as its
  *own* account — nothing is pushed to it).
- **Consent boundary lives in `consent.py`, and `consent.py` must match
  `shared/model/consent.js` byte-for-byte.** `desired_shared()` is the only
  place that decides whether a room mirrors; it always goes through
  `consent.effective_shared()`, never a hand-rolled check (the `reason`
  string is UI-only — never parse it for authorization). If you change the
  precedence, default, or any input gate anywhere, change it in **both**
  files and rerun `tests/unit/consent.test.js`, `tests/unit/consent_py.test.py`
  **and `tests/conformance/consent_conformance.py`** — the conformance
  harness proves parity on ~84k exhaustive+fuzz vectors and is the
  authority; any differing output or crash on either side is a red build.
- **Revocation durably retires access, not bytes.** `delete_mirror()`
  removes the master space-child link, kicks the manager, and leaves the
  room — a CS-API client (not a Synapse admin) cannot server-side-purge a
  room. Historical copies may remain readable under Matrix retention/history
  semantics. Cleanup failures retain their stage and retry; a revoking room cannot
  forward new content. Do not describe this as erasure or add a purge authority.
- **Read-only enforcement on every mirror room, stamped at creation.**
  `create_mirror()`'s `power_level_content_override` sets
  `{users: {master_user: 100, manager_mxid: 0}, events_default: 50}` — the
  manager can read, never send. Any new mirror-room-creation path must set
  the same override. The per-teammate proposals room uses a *different*,
  narrower override (`events_default: 100`, `events: {com.jkali.proposal:
  50}`) so the manager can send *only* that one event type there, never
  `m.room.message`.
- **Delivery idempotency is destination-scoped.** `delivery_map` commits only
  after a successful destination write. Source ingestion may checkpoint after
  durable queue/gap references exist. Histories are discovered pagewise on disk,
  then delivered chronologically; Direct history proposals always take cold-start
  handling. Fresh room consent is checked before each outgoing message slice and
  event. A request already in flight cannot be unsent.
- **Proposal pull has hard limits, checked before every write:** the write
  target must equal the recorded `local_proposals_room` (checked in
  `forward_proposals()`, and it also refuses if that id collides with any
  `mirror_rooms.master_room_id` — a proposals target can never be a mirror
  room); the event type written there is always the hardcoded literal
  `com.jkali.proposal`, never `m.room.message`; `target_room` inside a
  pulled proposal is validated by *shape* only (`ROOMID_RE`) and is
  otherwise inert data carried down for the teammate's own guarded local
  send path (`shared/ui/chat.js`'s `sendConvoMessage`) to re-validate
  against the live joined set. **Exception (D2):** for a `direct`-level
  conversation the daemon itself sends into `target_room` — see the next
  invariant, which is the complete list of what makes that safe.
- **The `direct` auto-send is the one send path here, and all eleven gates
  are load-bearing.** `_direct_send_gate()` runs them in order and returns
  the sanitized body only if every one passes; ANY failure falls back to the
  ordinary actionable inbox draft (never a silent drop), and the gate name is
  logged hash-only:
  1. **Sender.** `ev["sender"]` — the SERVER-stamped one — must equal the
     freshly-resolved `cfg.manager_mxid`. `cfg.master_user` (the teammate's
     own PL-100 scoped master account) is refused by name. `created_by` is
     cosmetic, pinned to the server-stamped sender in
     `sanitize_proposal_content()`, and feeds no decision anywhere.
  2. **Send-grade sanitization** (`sanitize_send_body()`): the same
     character class as `shared/ui/el.js sanitize()` (control/bidi/
     zero-width), an 8000 clamp, and a refusal of any body whose first
     non-whitespace character is `!` (bridge-command injection defense).
  3. **Freshness.** Incremental sync only, `origin_server_ts` within 10
     minutes. A cold start (no `proposal_sync_since`, or an empty
     `proposal_map` — a lost or restored state.db) routes the WHOLE batch to
     the inbox, so state loss can never replay history as real sends.
  4. **Positive target check.** Room-targeted AND a row in
     `mirror_rooms.local_room_id` — membership in the mirrored set, not a
     management-room denylist. Person-targeted proposals are NEVER auto-sent.
  5. **Fresh consent point-read.** The target's `com.jkali.share_override`
     is re-read from account-data at send time through
     `consent.effective_level`; `direct` required, ANY error means not
     direct. The reconcile pass's cached view is never the authority here.
  6. **Persisted rate cap.** `UPLINK_DIRECT_SEND_ROOM_HOURLY` (default 20),
     rolling 3600s per room, counted in `direct_send_log` — so it survives
     KeepAlive restarts and a crash cannot reset the budget. `0` disables
     auto-send entirely.
  7. **Intent before dispatch.** `proposal_map.outcome = 'attempted'` and the
     rate-cap tick are COMMITTED before the PUT, whose txn id is the
     deterministic `autosend_<master_event_id>`. Never reorder these.
  8. **Exactly one inbox artifact per proposal**, on every path: the plain
     draft, the `com.jkali.auto_sent` + `sent_event_id` record, or the
     `com.jkali.send_ambiguous` record. The field names are a wire contract
     with `apps/user/proposals.js` — do not rename one side alone.
  9. **Failure split.** A 4xx from the local hs is a known refusal (nothing
     sent → ordinary draft); a transport failure or 5xx is post-dispatch
     ambiguous → the labelled "may already have been sent" record, never a
     plain pending draft. An interrupted send found as `attempted` on a later
     pass is recovered the same way and is **never re-sent**.
  10. **Durable audit.** `direct_send_audit` rows plus the log line, both
      hash-only: no body, no room id, no handle — ever, on this path.
  11. **Master-identity binding.** `master_proposals_room`,
      `proposal_sync_since` are bound to `(master_hs,
      master_user, manager_mxid)` in `meta`. Any change drops discovery/cursors
      (so the next pull is a cold start), RETAINS `proposal_map` safety outcomes,
      AND suspends auto-send, writing
      `com.jkali.direct_send_suspended`; it resumes only when the teammate's
      `com.jkali.direct_send_ack` matches that exact four-field tuple.
  The `com.jkali.auto_sent_from_proposal` field on the sent message is
  **cosmetic** (F14) — forgeable by anything holding the teammate token, and
  it must never feed the `from_me` gate or any other trust decision.
- **Address-book contacts leave the machine only through
  `reconcile.plan_contact_mirror`, and only as a per-pass diff.** The
  planner is the consent gate for contact PII: a row is a push candidate
  only if it is live, its source is in the daemon's known-source allowlist
  (`SOURCE_ID_TO_LABEL` — adding a store source means adding it there), and
  `consent.resolve_contact_share` says shared; a not-shared row is in
  neither leg and never reaches a network call.
  **Per-contact overrides (per-contact-share plan, C2)** add a more specific
  level to that resolution — `com.jkali.contact_overrides`, keyed
  `'<source>|<network_id>'`, values `share`/`private` — with these bounds:
  `overrides` is a **required positional** parameter of
  `plan_contact_mirror` (F4: an unconverted call site must be a `TypeError`,
  never a silent widening back to source-only); it is consulted strictly
  **after** the known-source and `deleted` checks, so it can never resurrect
  an unknown source or a soft-deleted row; and it is a **boolean gate only** —
  every field of pushed content comes from the store row, never from the
  override key. `read_contact_overrides()` returns `{}` for 404 and **None**
  for anything else, and a `None` **skips the push leg entirely** while
  tombstones still run off the last successfully-read map cached in
  `meta['contact_overrides_cache']` (P3) — a transient read failure must never
  let a `'private'`-overridden contact fall back to a share-all source and push
  its PII. `read_contact_policy()` has the same 404-vs-other split and a
  non-404 error **aborts the whole pass** (collapsing it to global-private used
  to storm a full tombstone sweep on a blip). The push leg re-reads the map
  every `OVERRIDE_RECHECK` (50) pushes and drops rows flipped to `'private'`
  mid-pass (F10); the residual sub-chunk window is accepted.
  **F9: an override key IS a phone number / email — never log one.** Contact
  logging stays counts-only, as it already was for handles and display names.
  New contacts rooms are created with `history_visibility: joined` (F2) as a
  partial mitigation for the fact that a tombstone does not retract what was
  already mirrored; existing rooms are deliberately unchanged, and the UI copy
  states the residual plainly. The tombstone leg diffs the
  **complete, unfiltered** `contact_mirror` table against live-shared, so
  a mirrored handle whose source was renamed/removed is tombstoned, never
  stranded. Tombstones are applied before pushes (revocation never waits
  behind a backfill). `contact_mirror` is written (push) or deleted
  (tombstone) only after the master's 2xx; push compares versions with
  `!=`, not `<` (a rebuilt `contacts.db` restarts at 1). If the profiles
  read fails for any reason other than 404, relink and pushes are skipped
  that pass and tombstones still run. Backfill volume relies on the
  master's loosened `rc_message` (`master/setup.sh`). The
  `sha1(source|network_id)` state key is pseudonymous (E.164 is a small
  enumerable domain), not confidential; tombstones legitimately transmit
  keys of handles that were already on the master.
- **Media re-upload remains size bounded.** Permanent invalid/encrypted/oversize
  inputs use the existing placeholder. Transient errors additionally create a
  durable media retry; a successful retry edits the placeholder and preserves
  server-derived attribution. Consent is checked again before retry dispatch.
- **Secrets and state are mode 600.** `state.db` is chmod'd 600 on open;
  the enrollment env file `enroll_client.py` writes is 600. Never widen
  either.
- **Config is env-only, no secrets on argv, no config file.** See the
  `Config` class / the module docstring in `uplink.py` for the full env
  var list (`LOCAL_HS_URL`/`LOCAL_TOKEN`/`MASTER_HS_URL`/`MASTER_TOKEN`/
  `MANAGER_MXID`/`MASTER_SPACE`, etc.).

## How to run / test

```bash
# unit tests (pure logic, no daemon, no network):
python3 tests/unit/consent_py.test.py
python3 tests/unit/uplink_reconcile.test.py
python3 tests/unit/uplink_direct_send.test.py    # D2 auto-send gates + schema
python3 tests/unit/uplink_share_level.test.py    # D2b share-level stamping
python3 tests/unit/uplink_contact_overrides.test.py  # per-contact override gates

# run the daemon against a real local + master pair (see master/CLAUDE.md
# to bring the master stack up first):
LOCAL_HS_URL=http://127.0.0.1:8008 LOCAL_USER=@jkali:localhost \
LOCAL_TOKEN=... \
MASTER_HS_URL=http://127.0.0.1:8018 MASTER_USER=@alice:master \
MASTER_TOKEN=... MANAGER_MXID=@manager:master MASTER_SPACE=!... \
python3 agents/uplink/uplink.py

# enrollment flow (v1.5), teammate side — see master/CLAUDE.md for minting
# the code on the master side:
python3 agents/uplink/enroll_client.py --enroll-url https://master.example \
  --code <CODE> --out ./uplink.env.local
```

The **integration harness** (`tests/integration/harness.py`) runs the real
`uplink.py` as a subprocess against two live homeservers and is the
authoritative end-to-end test for this directory — every scenario in
`tests/integration/harness.py`'s `SCENARIOS` list exercises this daemon.
See `tests/CLAUDE.md`.

## How to change this safely

1. Any change to `consent.py`'s resolution logic must be mirrored in
   `shared/model/consent.js` in the same commit, and both unit test files
   re-run. Do not let these drift even for a "temporary" fix.
2. Any new master-write call must decide, explicitly, which power-level
   override applies (mirror-room read-only vs. proposals-room
   proposal-only) and must not accidentally grant the manager
   `events_default` high enough to send `m.room.message`.
3. Never advance a watermark (`_set_watermark`, `meta_set("sync_since", …)`,
   `meta_set("proposal_sync_since", …)`) before the corresponding forward
   call has returned successfully — that ordering is what makes offline/
   online catch-up gap-free and dup-free (scenario `3_offline_online_catchup`
   in the integration harness is the check).
4. If you add a new mirror-room state stamp (following
   `com.jkali.source`/`com.jkali.profile`/`com.jkali.share_level`/
   `com.jkali.mirror_of`), remember `apps/master/main.js`'s
   `parseSnapshot()` is what reads it — update both sides together.
5. **Never widen the auto-send path.** Adding a caller of `_auto_send()`,
   reordering `_direct_send_gate()`, moving the `'attempted'` write after
   the PUT, making a gate advisory, or letting a gate failure drop a
   proposal instead of filing the ordinary draft each turns a bounded
   capability into an unbounded one. `tests/unit/uplink_direct_send.test.py`
   is the regression check; a change here that does not also change that
   file is almost certainly wrong. The record field names
   (`com.jkali.auto_sent` / `sent_event_id` / `com.jkali.send_ambiguous` /
   `com.jkali.direct_send_suspended` / `com.jkali.direct_send_ack`) are a
   wire contract with `apps/user/` — rename them on both sides or not at all.
6. Any change to an EXISTING state.db table's shape needs a
   `Uplink._migrate_db()` block and a `SCHEMA_VERSION` bump, tested against a
   db created by the previous schema — the init block cannot evolve one.
7. Run the full integration suite after any change here (see
   `tests/CLAUDE.md`) — this daemon has no meaningful "does it look right"
   check short of the real two-homeserver scenarios.
