> STATUS: DONE 2026-08-30. Tasks A–D implemented and deployed on this Mac; unit
> suite green; integration scenarios 12 + 13 pass; fresh verifier CONFIRMED.
> Deferred: `pm_mng-1qf` (daemon-wide 429/hot-loop/master-identity + the
> verifier's P4 note on catching a JSON-decode error in `read_contact_profiles`).

# macOS Contacts — deploy the importer + backfill-on-enable to the master

**Goal:** macOS Contacts are actually imported on a teammate's Mac (hourly, via
launchd), and the *shared* ones reach the master according to the existing
contact-share consent infrastructure — including **backfill when sharing is
enabled after an import** and **new contacts added later**. Closes the gap
where the shipped iMessage slice (`2026-08-29-imessage-contact-import-slice.md`)
was never deployed and could only mirror rows imported *after* the share flip
(`pm_mng-q5u.2`).

**Verified starting state (2026-08-30, this machine):** importer code exists
and its unit tests pass; `contacts.db` did not exist, `com.jkali.contacts-import`
was not loaded, `install.sh`/`setup.sh` never install it. A real run
(`python3 agents/contacts/import_macos.py`) works on macOS 26.6 (JXA path OK,
TCC granted) but this address book is empty (1 record, 0 handles), so full
functionality cannot be exercised with real data here — verification uses a
fake `osascript` shim (unit) and a seeded `contacts.db` against the two real
homeservers (integration scenario 13).

**Non-goals:** Hub address-book browsing/linking UI (slice D of the scoping,
future); libphonenumber (`pm_mng-syy`); new imported fields; renaming the
`imessage` store source.

## Global constraints (unchanged from the shipped slice)

- Master stays send-incapable; `apps/master/` untouched.
- Contact-share stays its own consent dimension, default `private`; a row
  whose source does not resolve shared never leaves the machine (no network
  call). `consent.py`/`consent.js` are **not** modified by this plan.
- Exactly-once: a `contact_mirror` row is written only after the master's
  2xx; `MasterUnreachable` propagates before any record is written.
- `contacts.db` chmod 600; no handle values or names in logs.
- Importer fail-closed contract unchanged (`import_once` never calls
  `upsert_contacts` on a failed/garbled read).

---

## Task A — importer hardening (`agents/contacts/import_macos.py`)

Files: `agents/contacts/import_macos.py`, `tests/unit/import_macos.test.py`,
`agents/contacts/CLAUDE.md`.

1. **Don't leave Contacts.app open.** `osascript` launches Contacts.app to
   answer `people()`; under an hourly launchd job that pops a GUI app in the
   user's session every hour. In the fixed `_JXA_SCRIPT` constant: read
   `var wasRunning = Contacts.running();` **before** any other call, and after
   `out` is built, `if (!wasRunning) { try { Contacts.quit(); } catch (e) {} }`.
   The script stays a fixed constant; no interpolation.
2. **Timeout.** `_run_osascript` timeout 60 → 600 s. Each person costs ~3
   Apple Events; a few-thousand-entry book can exceed a minute. The job is
   hourly and fail-closed, so a long read is safe; a hung one still errors.
3. **Test the real subprocess path.** New tests in `import_macos.test.py`
   that set `_RAW_FOR_TEST = None`, put a fake executable `osascript` first
   on `PATH` (temp dir; shell script that `cat`s a JSON fixture / exits 1),
   and assert: (a) `import_once` reads through `subprocess` and stores the
   row; (b) a non-zero exit yields `{"error": ...}` and no db file; (c)
   non-JSON stdout yields `{"error": ...}`. Restores `PATH` in `finally`.

Done when: `python3 tests/unit/import_macos.test.py` prints `ok import_macos`
with the new cases; a manual run on this machine exits 0 and Contacts.app is
not left running afterwards when it was not running before.

## Task B — deploy (`setup.sh`, `install.sh`, `.gitignore`, docs)

Files: `setup.sh`, `install.sh`, `.gitignore`, `INSTALL.md`,
`agents/contacts/CLAUDE.md`, `CLAUDE.md` (master-sync directory list).

1. `setup.sh`'s `install_agent` gets an optional health URL (skip the curl
   probe when empty). Add
   `install_agent "${HERE}/agents/contacts/com.jkali.contacts-import.plist" "com.jkali.contacts-import" ""`.
   `install_agent` already `mkdir -p`s the sibling `logs/` dir the plist
   writes to. Log, right before it: macOS will show an "osascript wants access
   to your Contacts" prompt on the first run — click Allow; denied → System
   Settings → Privacy & Security → Contacts → osascript.
2. `.gitignore`: `agents/contacts/logs/` (`*.db` already covers the store).
3. `INSTALL.md`: a short "Contacts (macOS)" subsection: what is imported,
   that nothing leaves the machine until the Hub's contact-share panel says
   so, and the TCC prompt.
4. `agents/contacts/CLAUDE.md`: "install as a launchd job" now points at
   `setup.sh`; `CLAUDE.md` master-sync list gains an `agents/contacts/`
   bullet.
5. Load it on this machine (`setup.sh`'s `install_agent` path, or the exact
   `cp` + `launchctl load` it does) and confirm `launchctl list | grep
   contacts-import` and a fresh `agents/contacts/logs/import.log` line.

Done when: `setup.sh` is idempotent with the new agent; the job is loaded
here and has logged one successful run.

## Task C — backfill-on-enable: full desired-set reconcile (`agents/uplink/`)

Files: `agents/uplink/reconcile.py`, `agents/uplink/uplink.py`,
`tests/unit/uplink_reconcile.test.py`, `agents/uplink/CLAUDE.md`.

**Why:** `mirror_contacts()` pushes only rows with `version > contact_cursor`
and advances the cursor over *skipped* (not-shared) rows. After a
private→share flip nothing bumps a version, so every already-imported contact
sits below the cursor forever. The revocation pass added in `129b9b5` already
diffs the desired set against `contact_mirror` for tombstones; make the push
side symmetric and drop the cursor.

1. `reconcile.py`: replace `select_contacts_to_mirror` (becomes dead) with

   ```python
   PUSH_CAP = 200  # per-pass push budget; tombstones are never capped

   def plan_contact_mirror(rows, mirrored, policy, sources, push_cap=PUSH_CAP):
       """rows: store rows (deleted included). `sources` (the daemon's
       SOURCE_ID_TO_LABEL keys) filters ONLY the rows: a row whose source
       is not in `sources` can never be a push candidate and never counts
       as live-shared, so the gate is self-contained (SR-4).
       mirrored: {(source, network_id): mirrored_version} = the COMPLETE
       contact_mirror table, deliberately NOT filtered by `sources`: a
       mirrored handle whose source is unknown/renamed/removed is no
       longer live-shared and is therefore tombstoned, never stranded on
       the master (same as today's unfiltered revocation pass).
       policy: normalized contact-share policy.
       Returns {"tombstone": [(source, network_id), sorted],
                "push": [rows, ascending version, at most push_cap],
                "not_shared": int  # live rows in `sources` that resolved private
                "pending": int}    # shared pushes deferred by push_cap
       tombstone = select_contacts_to_tombstone(ALL mirrored keys, live-shared keys)
       push      = live (deleted=0) rows in `sources` whose source resolves
                   shared AND (handle not mirrored OR mirrored_version != row version)
       `!=`, not `<`: version is a per-store change token, not a clock — a
       rebuilt contacts.db restarts at 1 and `<` would leave stale PII on the
       master forever (SR-1). The cap bounds one pass; the remainder is
       re-planned next pass, so a backfill resumes naturally (SR-3).
       A not-shared row appears in neither list."""
   ```
   `select_contacts_to_tombstone` stays (reused). Pure, no I/O.
2. `uplink.py` `mirror_contacts()`:
   - `read_contact_profiles()` distinguishes 404/absent (→ `[]`, relink
     normally) from any other error (→ `None`); on `None` the relink step
     (a) is **skipped this pass** and a warning logged, so a transient local
     HTTP blip can no longer unlink every row and version-bump the whole
     book twice (SR-5). On that `None` the **push leg is skipped too** (a
     push made without profiles would stamp `person_display: null` and,
     under `!=`, never be corrected until the row's version changes);
     **tombstones still run** — revocation never waits on the local
     homeserver being healthy.
   - then read all rows fresh (`shared_since(conn, source, 0)` per
     `SOURCE_ID_TO_LABEL`), read `contact_mirror` as
     `{(source, network_id): mirrored_version}`, call `plan_contact_mirror`,
     and apply **tombstones first, then pushes** (revocation is never queued
     behind a backfill — SR-2):
     - for each `tombstone` handle: `_put_contact({... deleted: 1})` →
       `DELETE` the mirror row, commit;
     - for each `push` row: `_put_contact` → on return `INSERT OR REPLACE
       contact_mirror (source, network_id, mirrored_version=row version,
       master_state_key)`, commit.
   `MasterUnreachable`/`HTTPError` from `_put_contact` propagate before
   either write, so the next pass re-plans and retries the same handle
   (idempotent: the state PUT keys on `sha1(source|network_id)`). Remove
   `contact_cursor` reads and writes; the `DELETE FROM meta` of that key in
   `ensure_contacts_room` stays with a one-line "legacy cleanup" note.
   Rewrite the two in-file invariant texts that describe the cursor —
   the "HARD LIMITS enforced here" block (~1109–1120) and
   `mirror_contacts()`'s docstring — to describe the per-handle diff (SR-7).
   Log line: `contacts: relinked=%d pushed=%d tombstoned=%d not_shared=%d
   pending=%d` (counts only; `not_shared`/`pending` come straight from the
   planner's return value), kept behind the existing non-zero guard (SR-8).
3. `tests/unit/uplink_reconcile.test.py`: replace the
   `select_contacts_to_mirror` block with `plan_contact_mirror` cases:
   backfill (rows at versions 1–3, nothing mirrored, shared → push all
   three, in order); not shared → push and tombstone both empty; already
   mirrored at the same version → not re-pushed; mirrored at a different
   version (older **and** newer, the rebuilt-store case) → re-pushed;
   deleted row never mirrored → nowhere; deleted row that is mirrored →
   tombstone; source flipped private with mirrors → tombstone all, push
   none; mixed sources → only the shared source pushes; per-source
   `private-all` beats global `share-all`; a row whose source is not in
   `sources` is ignored even under global `share-all`; **a mirrored handle
   whose source is not in `sources` (and has no row) is tombstoned, not
   stranded**; push cap: 5 shared rows with cap 2 → exactly the 2 lowest
   versions, `pending == 3`, tombstones unaffected; `not_shared` counts
   live in-`sources` rows that resolved private; None/empty inputs safe.
4. `agents/uplink/CLAUDE.md`: add the contact mirror to "What lives here"
   (`contact_mirror` table, `mirror_contacts()`, `plan_contact_mirror`) and
   security-invariant bullets: the mirror is a per-pass diff of
   *desired-shared-and-live* vs *mirrored*; the plan function is the consent
   gate for contacts, must call `consent.resolve_contact_share` (never
   re-derive) and only considers sources in `SOURCE_ID_TO_LABEL` — adding a
   store source means adding it there; tombstones precede pushes; backfill
   volume relies on the master's loosened `rc_message`
   (`master/setup.sh`); the `sha1(source|network_id)` state key is
   pseudonymous (small enumerable domain), not confidential, and tombstones
   legitimately transmit keys of handles that were already on the master.

### Security review (pilotfish:security-reviewer, 2026-08-30) — NO P0/P1

| # | Finding (severity) | Disposition |
|---|---|---|
| SR-1 | `mirrored_version < version` leaves stale PII on the master after a `contacts.db` rebuild (versions restart at 1) (P2) | **FIX** — `!=` (step 1) |
| SR-2 | Tombstones after a multi-thousand-row backfill: revocation head-of-line blocked (P2) | **FIX** — tombstones first (step 2) |
| SR-3 | A first backfill blocks `tail_once`/`pull_proposals` for minutes; an HTTPError mid-batch skips `_last_reconcile` → 5 s hot loop; no 429/`Retry-After` handling (P2) | **FIX** the contact part — per-pass push cap (step 1). **DEFER** the daemon-wide 429/`Retry-After` + `_last_reconcile`-on-exception handling → bead `pm_mng-1qf` |
| SR-4 | `plan_contact_mirror` had no source allowlist of its own; unknown-source rows are shared under global `share-all` (P2) | **FIX** — `sources` param filtered inside (step 1), CLAUDE.md sentence (step 4) |
| SR-5 | `read_contact_profiles` fails open to "no groupings" on non-404 errors → mass unlink + double full re-push (P2, pre-existing, amplified) | **FIX** — 404 vs other; skip relink on other (step 2) |
| SR-6 | `master_contacts_room`/`contact_mirror` not reset when the master identity changes (P3, pre-existing, affects all room kinds) | **DEFER** → bead `pm_mng-1qf` (same daemon-wide bead) |
| SR-7 | In-file invariant texts still describe the cursor (P3) | **FIX** — step 2 |
| SR-8 | Log line dropped the `skipped` (not-shared) evidence and the non-zero guard (P3) | **FIX** — step 2 |

Done when: unit tests pass; scenario 13 (below) passes; scenario 12 still
passes.

## Task D — integration scenario 13 (`tests/integration/harness.py`)

Files: `tests/integration/harness.py`, `tests/CLAUDE.md`, `CLAUDE.md`
(scenario count 12 → 13).

`scenario_13_contact_backfill_on_enable`, same shape as scenario 12 (seeded
`contacts.db` through `contacts_store`, `UPLINK_CONTACTS_DB`, synthetic
`+1555…` self-style numbers, self-directed only):

1. Seed two `imessage` contacts; **no** contact-share policy (default
   private). Start the uplink. Wait for `master_contacts_room`; after one
   reconcile interval assert **zero** `com.jkali.contact` states.
2. PUT `com.jkali.contact_share_policy = {global: private, sources:
   {imessage: share-all}}`. Wait until **both** handles' states are present
   with `deleted: false` (backfill of rows that pre-date the flip — the
   `pm_mng-q5u.2` case).
3. Re-run `upsert_contacts` with a 3-row snapshot (the two + one new). Wait
   until the third appears (new contacts flow).
4. PUT policy `global: private` (no sources). Wait until all three states are
   `{deleted: true}` (revocation still works after the rewrite).
5. PUT `imessage: share-all` again. Wait until all three are back with
   `deleted: false` (re-share after tombstone re-pushes; mirror rows were
   dropped on the tombstone 2xx).

Evidence string lists the counts observed at each step. Register it in
`SCENARIOS`; update the "12 scenarios" mentions in `tests/CLAUDE.md`,
`tests/integration/run.sh`'s header, and root `CLAUDE.md`.

Prereqs to run: `matrix-master` is already up on this machine; the
throwaway `matrix-synctest` hub must be brought up
(`docker compose -p matrix-synctest -f tests/integration/docker-compose.test.yml up -d`)
— it is separate from the live `matrix-wa` hub and is never touched by this
plan.

## Order and gates

A → B → C → D (D verifies C). C is the consent/uplink boundary:
security-reviewer (done, dispositions above) + plan-verifier before
implementation, fresh verifier after, on the claim "after a private→share
flip, previously imported contacts appear on the master; a later import's
new contact appears; a flip back tombstones them; the importer runs hourly
under launchd on this Mac."

**Task A/B status (2026-08-30):** done and deployed here. Findings from the
real runs: (1) `osascript -e` SIGKILLs a JXA script containing `//` line
comments on macOS 26.6 — the constant is now comment-free with a unit
guard; (2) the Contacts read intermittently fails with `osascript exited 1`
after the 2-minute Apple-event timeout (2 of ~12 runs, terminal and launchd
alike, not TCC — kickstarted launchd runs succeed with Contacts.app both
closed and open); the importer fails closed and now retries the read once
after a short pause before giving up for that hour.

Beads: `pm_mng-q5u.2` (Task C), plus new children of `pm_mng-q5u` for A, B, D.
