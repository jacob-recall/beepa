# tests/ — 19 unit tests, all wired into tests/run.sh + the 12-scenario integration harness

PLAN-MASTER-SYNC.md §13; PLAN-MASTER-SYNC-IMPL.md's "Cross-cutting:
documentation & tests". Every edge case named in the design doc has an
explicit, named test — see the scenario list below.

## What lives here

```
tests/
  run.sh                          runs all 19 unit tests (9 node + 9 python) — see below
  unit/
    consent.test.js               shared/model/consent.js — every precedence case
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
    uplink_sources.test.py        source-space derivation
  integration/
    run.sh                        wrapper: `python3 tests/integration/harness.py "$@"`
    harness.py                    drives 2 real homeservers + the real uplink.py subprocess
    docker-compose.test.yml        the throwaway TEST-USER hub (compose project matrix-synctest)
    test_enroll.py                v1.5 enrollment-code exchange, end to end
    synapse/                       config for the throwaway test-user Synapse
```

### Unit tests

- `unit/consent.test.js` — plain-node test (no framework) covering every
  precedence combination for `shared/model/consent.js`'s resolver:
  per-conversation override wins, profile share/private wins over source/
  global, per-source `share-all`/`private-all`, global `share-all` as a
  *standing* policy that also covers a conversation arriving later, and the
  safe default (`private`) when nothing says otherwise.
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

**The 12 scenarios** (`SCENARIOS` in `harness.py`), each mapping to a named
requirement from PLAN-MASTER-SYNC.md §13 / IMPL P2.5:

1. `1_share_one_conversation` — share → mirror room appears with correct
   history, order, alignment, source badge.
2. `2_new_local_message` — a new local message appears on master.
3. `3_offline_online_catchup` — **the headline edge case**: kill the
   uplink, inject messages, restart, verify watermark-resume with no gaps
   and no duplicates.
4. `4_master_offline_buffer` — master unreachable → buffer + backoff →
   delivers once master is back, watermark not advanced meanwhile.
5. `5_share_all_standing_policy` — Share-All (global or per-source) also
   covers a conversation arriving *later*; a per-conversation `private`
   exception still stays out.
6. `6_revoke_each_level` — unshare at any consent level → master copy
   revoked (space-child removed, manager kicked, room left).
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
    `com.jkali.profile`, so the master groups them under one person; a
    per-conversation `private` still excludes a member conversation.
12. `12_contact_share_and_propose` — a shared contact profile reaches the
    master's contacts index, and a manager-authored proposal against that
    contact reaches the right teammate's proposals room.

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
# tests/run.sh runs all 19 (9 node + 9 python):
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

# 3. run all 12 scenarios, or filter by name substring:
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
   `proposal_map`/`meta` in `agents/uplink/uplink.py`), check
   `harness.py` for any direct `sqlite3` inspection of the state DB that
   would need updating too.
