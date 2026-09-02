# tests/ — 30 unit tests + the consent conformance harness, all wired into tests/run.sh + the 14-scenario integration harness

PLAN-MASTER-SYNC.md §13; PLAN-MASTER-SYNC-IMPL.md's "Cross-cutting:
documentation & tests". Every edge case named in the design doc has an
explicit, named test — see the scenario list below.

## What lives here

```
tests/
  run.sh                          runs all 30 unit tests (14 node + 16 python)
                                  + the consent conformance harness — see below
  unit/
    consent.test.js               shared/model/consent.js — the explicit three-level model
    uplink_migration.test.py      D0 migration to explicit levels + the retained old resolver
    master_invites.test.js        apps/master/invites.js — the console's auto-join/render trust gate
    user_invites.test.js          apps/user/invites.js — the hub's bridge-invite auto-join trust gate
    csp_parity.test.js            CSP header parity across apps
    contact_consent.test.js       contact-level consent precedence
    contacts_profile_handles.test.js  unified-contact profile/handle shaping
    proposal_identifier.test.js   proposal identifier derivation
    proposal_parse.test.js        proposal parsing
    auto_merge_number.test.js     the sanctioned autoMergeByNumber() path
    contacts_store.test.py        agents/uplink contacts store
    import_macos.test.py          macOS address-book import
    contact_consent_py.test.py    contact-level consent — MUST mirror contact_consent.test.js
    uplink_reconcile.test.py      agents/uplink/reconcile.py — reconcile/idempotency/watermark
    uplink_proposal_sanitize.test.py  proposal sanitization on the way down
    number_resolver.test.py       phone-number resolution
    consent_py.test.py            agents/uplink/consent.py — MUST mirror consent.test.js
    uplink_proposals.test.py      proposal handling
    uplink_direct_send.test.py    D2 'direct'-level auto-send gates + S3 schema migration
    uplink_share_level.test.py    D2b per-mirror com.jkali.share_level stamping
    uplink_sources.test.py        source-space derivation
    enroll_password_derivation.test.py  master derived-password scheme (no stored passwords)
  conformance/
    consent_conformance.py        JS vs Python consent resolvers on ~84k vectors (exhaustive+fuzz)
    consent_eval.mjs              pure dispatcher onto the real shared/model/consent.js exports
  integration/
    run.sh                        wrapper: `python3 tests/integration/harness.py "$@"`
    harness.py                    drives 2 real homeservers + the real uplink.py subprocess
    docker-compose.test.yml        the throwaway TEST-USER hub (compose project matrix-synctest)
    test_enroll.py                v1.5 enrollment-code exchange, end to end
    synapse/                       config for the throwaway test-user Synapse
```

### Unit tests

- `unit/consent.test.js` — plain-node test (no framework) for
  `shared/model/consent.js`'s resolver under the EXPLICIT three-level model
  (direct-share-level plan, D1): `'share'` and `'direct'` share, `'private'`
  does not, and **absent or ANY unrecognized value resolves private** — each
  case run alongside the loudest possible standing policy and a shared contact
  profile, because those inputs are now accepted and ignored. Also pins
  `effectiveLevel()`, the junk-value key deletion in `overridesFromSync()`, and
  the plain-object-only override-key lookup in `resolveAll()`.
- `unit/uplink_migration.test.py` — D0: the one-time migration that
  materializes standing-policy shares as explicit `migrated: true` `'share'`
  overrides. Asserts the S1 acceptance (a pre-S1 state.db with a
  standing-policy-shared, currently-mirrored room keeps the SAME
  `master_room_id` and sees **zero `delete_mirror` calls**), idempotency, that
  an existing explicit override is never rewritten, that an unmirrored room is
  never widened into a share, that a failed write aborts before the flag and
  before any deletion, and the F8 pin that the retained old resolver treats a
  `'direct'` override as INHERIT (so a partial rollback is caught here).
- `unit/master_invites.test.js` — plain-node test for `apps/master/invites.js`,
  the manager console's identity gate. Fixtures are copied from the REAL
  stripped `invite_state` (create/name/join_rules/member only, no custom
  types): the space is joined only when its name matches its own creator's
  localpart, children only when they are known children of a verified space
  AND their own creator matches, plus the refusals (cross-teammate spoof,
  missing sender, zero-width-char label, space via the child rule),
  the join caps, and malformed room ids. A failure here means the console
  would join or render something it must not.
- `unit/user_invites.test.js` — plain-node test for `apps/user/invites.js`,
  the teammate hub's bridge-invite gate. Three fixtures are captured
  **verbatim from this hub's live `/sync` `rooms.invite`** (a Google Messages
  DM portal, the Google Messages source space, a WhatsApp DM portal; only
  contact/account display names are renamed), the rest are hand-built spoofs
  derived from them: bridge ghost, self, unknown sender, missing
  `m.room.create`, an invite addressed to someone else, two invite events with
  different senders, cross-bridge laundering (created by one bot, invited by
  another), the space name/creator bind, the per-pass caps, malformed and
  foreign room ids, and `localpart()` edge cases. A failure here means the hub
  would auto-join a room it must not.
- `unit/consent_py.test.py` — the same case list against
  `agents/uplink/consent.py`. **These two files must assert the same
  cases with the same expected results.** If you add a case to one, add
  the matching case to the other in the same change.
- `unit/uplink_reconcile.test.py` — `agents/uplink/reconcile.py`'s pure
  functions: `reconcile_decisions` (create/delete/keep sets),
  `select_new_events` (idempotency filter, including within-batch dedup),
  `next_watermark` (advances only when `confirmed=True`).
- `unit/uplink_direct_send.test.py` — D2: the `direct` level's auto-send,
  the one path where code rather than the teammate puts a message into a
  real conversation. One case per gate and per failure mode: sender
  mismatch and `master_user`-as-sender refused, `created_by` spoof ignored,
  `!`-prefixed body refused, control/bidi stripped and 8000 clamp applied to
  what is actually sent, cold start and stale timestamps routed to the
  inbox, unmirrored and person-targeted proposals never auto-sent, the
  `direct → private` flip caught by the send-time point-read, the per-room
  cap enforced and surviving a restart, an interrupted send recovered as
  "may already have been sent" with no duplicate (deterministic txn id),
  identity rebinding suspending auto-send until the ack matches, exactly one
  inbox artifact per proposal in every path, hash-only logs and audit rows,
  the S3 schema migration against a pre-S3 state.db, and the one-time
  proposals-room topic re-PUT.
- `unit/uplink_share_level.test.py` — D2b: per-room LEVELS through
  `reconcile_decisions` (`level_is_shared`, so `'private'` can never read as
  shared through bare truthiness) and `plan_level_restamp`, plus a real
  `reconcile()` proving a mirror is stamped at creation, re-stamped on
  promotion AND demotion, backfilled once if it predates the stamp, and
  otherwise left alone.

### Integration harness (`tests/integration/harness.py`)

Drives two **real, running** homeservers end to end:

- **TEST-USER hub** — an isolated throwaway Synapse (`server_name:
  localhost`) on `127.0.0.1:8028`, compose project `matrix-synctest`
  (`docker-compose.test.yml`). Models one teammate's local stack. Fully
  separate from the live `matrix-wa` hub (8008/8009/8010) — **this harness
  never touches matrix-wa.**
- **MASTER hub** — the already-running `matrix-master` Synapse on
  `127.0.0.1:8018` (see `master/CLAUDE.md`); the harness reads teammate
  tokens/space ids from `master/tokens.local`.

It registers a local test user + a synthetic local "contact" (so `from_me`
is real, not faked: test-user-authored ⇒ `com.jkali.from_me` true,
contact-authored ⇒ false), builds bridge SOURCE spaces named exactly like
the real bridges (e.g. `"iMessage"`, `"LinkedIn"`) with synthetic DM rooms
linked via `m.space.child` (so `uplink.py`'s `sources_from_sync` derives
the source the same way `shared/ui/sources.js` does), sets consent via
account-data, runs the **real** `agents/uplink/uplink.py` as a subprocess,
and asserts on MASTER homeserver state.

**The 14 scenarios** (`SCENARIOS` in `harness.py`), each mapping to a named
requirement from PLAN-MASTER-SYNC.md §13 / IMPL P2.5, extended by the
direct-share-level plan's S5. Conversation sharing is EXPLICIT-ONLY (D1): a
scenario that used to lean on a standing share-all/private-all policy for
CONVERSATIONS now writes explicit per-room `com.jkali.share_override` levels
instead — `set_policy()` (the old global/per-source standing policy) still
exists in the harness ONLY for the separate, untouched contact-sharing
dimension (scenarios 12/13) and for scenario 5's D0-migration leg, which
deliberately simulates a PRE-S1 install:

1. `1_share_one_conversation` — share → mirror room appears with correct
   history, order, alignment, source badge.
2. `2_new_local_message` — a new local message appears on master.
3. `3_offline_online_catchup` — **the headline edge case**: kill the
   uplink, inject messages, restart, verify watermark-resume with no gaps
   and no duplicates.
4. `4_master_offline_buffer` — master unreachable → buffer + backoff →
   delivers once master is back, watermark not advanced meanwhile.
5. `5_explicit_share_and_migration` — ports the removed standing-policy
   share-all scenario to the explicit model: a bulk explicit `share` write
   across several rooms, an explicit `private` exclusion on another sharing
   the same source, and proof that a brand-new room no longer auto-mirrors
   under any standing policy (it stays private until it gets its own
   override, then mirrors once explicitly shared). Also exercises D0: an
   already-mirrored room is rewound to a simulated pre-S1 state (override
   cleared, an old-style share-all policy restored, the migration flag
   rewound) and the SAME uplink instance is restarted against the SAME
   state.db — the S1 acceptance is the SAME `master_room_id` throughout
   (zero delete/re-create) and an explicit `migrated: true` `'share'`
   override materialized.
6. `6_revoke_each_level` — ports the removed 3-tier standing-policy
   revocation scenario: revoking a `share` room and revoking a `direct` room
   (the two real per-room levels left under the explicit model) both fully
   revoke the master copy (space-child removed, manager kicked, room left).
7. `7_read_only_manager` — manager attempts to send: rejected by power
   level **and** impossible because `apps/master/` ships no composer.
8. `8_cross_user_isolation` — user A's token cannot write user B's rooms.
9. `9_media_reupload` — v1.5: media re-uploads to the master's media
   store and renders as real bytes; oversized/failed media falls back to
   the v1 placeholder, never dropped.
10. `10_proposal_down` — v2: a manager-authored proposal reaches the
    teammate's dedicated local proposals room, and only that room/event
    type — never a mirror room, never `m.room.message`.
11. `11_profile_span_platforms` — unified contacts: a shared contact
    profile's conversations across ≥2 platforms mirror up stamped with
    `com.jkali.profile`, so the master groups them under one person (each
    member room still needs its OWN explicit `share` override, D1 — the
    profile groups, it no longer mirrors); a per-conversation `private`
    override still excludes a member conversation.
12. `12_contact_share_and_propose` — a shared contact profile reaches the
    master's contacts index, and a manager-authored proposal against that
    contact reaches the right teammate's proposals room.
13. `13_contact_backfill_on_enable` — the contact mirror is a per-pass
    diff, not a forward-only cursor: contacts imported while sharing was
    off are **backfilled** when the source is switched on; a later import's
    new contact flows; switching the source off tombstones all of them;
    switching it back on re-pushes them.
14. `14_direct_proposal_autosend` — direct-share-level plan D2/D2b: a
    manager proposal against a `direct` room is auto-sent by the uplink with
    no review click — exactly one `m.room.message` lands in the real local
    conversation carrying the cosmetic `com.jkali.auto_sent_from_proposal`
    field, the local proposals room holds exactly ONE record for it and it
    is the non-actionable `com.jkali.auto_sent: true` one, and a second
    identical pull produces no duplicate. Negative leg: the same proposal
    shape against a `share` room lands as an ordinary actionable draft and
    NO message is sent. The F2 mautrix command-scope probe (does a bridge
    execute a `!`-prefixed auto-sent body as an admin command?) is
    documented as NOT EXECUTABLE against this harness — see the comment
    immediately above `scenario_14_direct_proposal_autosend` in
    `harness.py`: the throwaway TEST-USER hub is a bare Synapse with no
    bridge container, and the defense (`_direct_send_gate()`'s leading-`!`
    refusal, D2.2) ships regardless of the hypothesis.

`tests/integration/test_enroll.py` separately proves the v1.5 enrollment
flow end to end (valid/reused/expired/invalid code) against the running
master stack, through the actual loopback exchange endpoint
(`master/enroll.py serve`) and the teammate-side client
(`agents/uplink/enroll_client.py`) — see `master/CLAUDE.md`.

## How to run

```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:${PATH}"
cd /Users/jkali/work/pm_mng

# --- unit tests ---
# tests/run.sh runs all 30 (14 node + 16 python) + the consent conformance harness:
tests/run.sh

# --- integration (needs BOTH stacks up first) ---
# 1. matrix-master, provisioned (see master/CLAUDE.md):
docker compose -p matrix-master --env-file master/.env \
  -f master/docker-compose.master.yml up -d
# the harness needs TWO teammate slots (scenario 8 = cross-user isolation),
# so provision with the alice/bob test roster (default roster is the real
# single user "jkali"):
TEAMMATES="alice bob" master/provision.sh
# 2. the throwaway test-user hub:
docker compose -p matrix-synctest -f tests/integration/docker-compose.test.yml up -d

# 3. run all 14 scenarios, or filter by name substring:
tests/integration/run.sh
tests/integration/run.sh 3_offline          # just the catch-up scenario

# 4. enrollment flow:
python3 tests/integration/test_enroll.py

# tear down the test-user hub when done (matrix-master can stay up, it's
# the always-on stack):
docker compose -p matrix-synctest -f tests/integration/docker-compose.test.yml down -v
```

`harness.py` writes uplink state DBs/logs to a scratch dir outside the repo
(`SYNCTEST_STATE_DIR` env var, defaulting under the session scratchpad) —
safe to delete between runs.

## How to change this safely

1. **Never point any test at the live `matrix-wa` stack** (8008/8009/8010).
   Everything here targets `matrix-synctest` (8028) and `matrix-master`
   (8018) only.
2. Adding a consent precedence case → add it to **both**
   `consent.test.js` and `consent_py.test.py` in the same change.
3. Adding a new edge case from a future phase → add a new named scenario
   function to `harness.py` and append it to `SCENARIOS`, following the
   existing naming (`N_short_description`) so `tests/integration/run.sh
   <filter>` keeps working.
4. If you change `master/provision.sh`'s power-level shape or account
   layout, re-run the full integration suite — most scenarios assert
   directly on the power levels / space structure it provisions.
5. If you change the uplink's SQLite schema (`mirror_rooms`/`event_map`/
   `proposal_map`/`direct_send_log`/`direct_send_audit`/`meta` in
   `agents/uplink/uplink.py`), it needs a `Uplink._migrate_db()` block and a
   `SCHEMA_VERSION` bump — `unit/uplink_direct_send.test.py` drives a db
   built by the pre-S3 schema through the current code and is where that
   gets proven. Also check `harness.py` for any direct `sqlite3` inspection
   of the state DB that would need updating too.
6. `unit/uplink_direct_send.test.py` is the regression suite for the ONE
   path where code, not the teammate, sends a real message (the `direct`
   level's auto-send). Any change to `_direct_send_gate()`/`_auto_send()`
   belongs in the same commit as a change here; never delete a case to make
   a change pass.
