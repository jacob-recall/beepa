# agents/uplink/ — the outbound-only mirror-up daemon

A headless background service that runs on the teammate's own machine
(sibling to the iMessage daemon), mirrors *shared* local conversations up
to the master homeserver, and pulls proposal suggestions back down into a
dedicated local room. Python 3.9+ stdlib only (`urllib` + `sqlite3`) — no
pip dependencies. PLAN-MASTER-SYNC.md §5.4/§7/§8; PLAN-MASTER-SYNC-IMPL.md
Phase 2/3/4.

## What lives here

- `uplink.py` — the daemon (`Uplink` class + `main()`). Two transports:
  `self.local(...)` (reads the teammate's LOCAL homeserver as the
  teammate's own account) and `self.master(...)` (writes the MASTER
  homeserver as the teammate's *scoped* master account; any transport
  failure against master raises `MasterUnreachable` so the caller buffers).
  Responsibilities, in `run()`'s loop order:
  1. `reconcile()` — resolve every joined local room's effective
     shared-state (via `consent.py`), diff against existing mirrors
     (`reconcile.py`'s `reconcile_decisions`), create new mirrors
     (`create_mirror`, which also backfills), delete revoked ones
     (`delete_mirror`), then backfill/tail every kept-or-created room.
  2. `tail_once()` — one `/sync` of the LOCAL homeserver; forwards new
     timeline events in already-mirrored rooms via `forward_events()`.
  3. `ensure_proposal_rooms()` / `pull_proposals()` — v2: idempotently
     create the per-teammate master + local proposals rooms, then pull new
     manager-authored `com.jkali.proposal` events down into the local one.
  SQLite state (`state.db`, chmod 600) holds `mirror_rooms`
  (`local_room_id → master_room_id, source, last_synced_pos`), `event_map`
  (`local_event_id → master_event_id`, the up-direction idempotency guard),
  `proposal_map` (`master_event_id → local_event_id`, the down-direction
  one), and `meta` (sync tokens, discovered proposal room ids).
- `consent.py` — pure Python port of `shared/model/consent.js`. **Must stay
  byte-parity with it** — same 4-level precedence, same reason strings, same
  normalization. This is what the daemon actually enforces at runtime, so a
  drift here is a shipped authorization bug, not just a UI inconsistency.
  Tested against `tests/unit/consent.test.js`'s cases via
  `tests/unit/consent_py.test.py`.
- `reconcile.py` — pure logic, no I/O: `reconcile_decisions()` (create /
  delete / keep sets from desired-shared vs. existing mirrors),
  `select_new_events()` (the idempotency filter over `event_map`),
  `next_watermark()` (advance only when `confirmed=True` — i.e. only after
  the master has 200'd). Unit-tested in
  `tests/unit/uplink_reconcile.test.py`.
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
  `consent.effective_shared()`, never a hand-rolled check. If you change
  the precedence or default anywhere, change it in **both** files and rerun
  both `tests/unit/consent.test.js` and `tests/unit/consent_py.test.py`.
- **Revocation deletes, it does not just stop updating.** `delete_mirror()`
  removes the master space-child link, kicks the manager, and leaves the
  room — a CS-API client (not a Synapse admin) cannot server-side-purge a
  room, so the durable copy becomes unreachable-by-the-manager rather than
  bytes-deleted; that satisfies §9's revocation requirement, and it is
  intentional, not a shortcut — do not "fix" it into a hard delete without
  re-reading §9 and the admin-purge note in the code.
- **Read-only enforcement on every mirror room, stamped at creation.**
  `create_mirror()`'s `power_level_content_override` sets
  `{users: {master_user: 100, manager_mxid: 0}, events_default: 50}` — the
  manager can read, never send. Any new mirror-room-creation path must set
  the same override. The per-teammate proposals room uses a *different*,
  narrower override (`events_default: 100`, `events: {com.jkali.proposal:
  50}`) so the manager can send *only* that one event type there, never
  `m.room.message`.
- **Idempotency via `event_map`/`proposal_map`, watermark advances only on
  confirmed delivery.** `forward_events()` filters through
  `reconcile.select_new_events()` before posting; `tail_once()` and
  `backfill()` only persist a new `last_synced_pos` after the forward
  succeeded (an exception, including `MasterUnreachable`, propagates before
  the watermark write). Never move a watermark write earlier than the
  corresponding master call's success.
- **Proposal pull has hard limits, checked before every write:** the write
  target must equal the recorded `local_proposals_room` (checked in
  `forward_proposals()`, and it also refuses if that id collides with any
  `mirror_rooms.master_room_id` — a proposals target can never be a mirror
  room); the event type is always the hardcoded literal
  `com.jkali.proposal`, never `m.room.message`; `target_room` inside a
  pulled proposal is validated by *shape* only (`ROOMID_RE`) — the uplink
  never sends to it itself, it only carries it down for the teammate's own
  guarded local send path (`shared/ui/chat.js`'s `sendConvoMessage`) to
  re-validate against the live joined set.
- **Media re-upload has a size cap and always falls back safely.**
  `_reupload_media()` returns `None` (→ v1 placeholder, never dropped or
  blocked) on any failure: bad/missing/encrypted mxc, over
  `UPLINK_MEDIA_MAX`, or a download/upload error. A placeholder is never
  durably committed while master is truly unreachable — the subsequent
  message `PUT` independently raises `MasterUnreachable` and the whole
  forward rolls back with the daemon's normal buffer/retry.
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
   `com.jkali.source`/`com.jkali.profile`/`com.jkali.mirror_of`), remember
   `apps/master/main.js`'s `parseSnapshot()` is what reads it — update both
   sides together.
5. Run the full integration suite after any change here (see
   `tests/CLAUDE.md`) — this daemon has no meaningful "does it look right"
   check short of the real two-homeserver scenarios.
