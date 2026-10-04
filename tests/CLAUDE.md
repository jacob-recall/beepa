# Tests and validation

Run `tests/run.sh` (or the configured host-runtime Python with `tests/run.py`).
The runner discovers every `tests/unit/*.test.py` and `*.test.js`, then runs
114,235 deterministic/fuzz consent vectors through both real resolvers and the
bridge-invite vectors through both real invite predicates
(`tests/conformance/invites_conformance.py`: `apps/user/invites.js` vs
`agents/uplink/invites.py`).
`--unit-only` omits conformance. Install the hash-locked `requirements-host.txt`
into a virtual environment when testing without an installed Beepa runtime.
The CI workflow pins Node/Python versions and records actual runtime versions.

## Live propagation probe (operator's own account only)

`python3 tests/integration/probe_propagation.py --i-am-sending-real-imessages`
sends ONE canary iMessage from the operator to their own handle (the
self-chat mapped in `imessage/state.db`) and times every hop with budgets
(daemon 30s, local echo 60s, master 90s). It addresses no other chat, changes
no setting, and prints only its own canary text. Exit 1 = a hop missed its
budget. Not part of `tests/run.py`; run it after any change to the iMessage
daemon, the uplink, or the Synapse stacks.

`--passive` sends nothing: it reads the hop stamps on the newest mirrored
events (median/max mirror lag), the uplink's health age and errors, and the
daemon's `/health`, exiting 1 when something is stale or retrying.
`tests/integration/launchd/com.jkali.beepa-probe.plist` runs it hourly and
appends to `agents/uplink/logs/probe.log` (install with
`launchctl bootstrap gui/$(id -u) tests/integration/launchd/com.jkali.beepa-probe.plist`).

## Disposable integration

`tests/integration/run.sh` creates BOTH local and master Synapse/PostgreSQL
stacks using unique project names, generated credentials, fixed-per-run allocated
loopback ports, a marked temporary state root, and synthetic accounts. It runs
the real uplink and cleans up only those stacks. It does not read live tokens,
provision the real master, send through real messaging bridges or touch real
Messages/Contacts data.

Commands:

```sh
tests/integration/run.sh                 # all sync scenarios
tests/integration/run.sh 3_offline        # filtered scenario
tests/integration/run.sh --enrollment    # one-time code/scoped account behavior
tests/integration/run.sh --roster        # additive/repeated setup retains accounts
tests/integration/run.sh --recovery      # scoped recovery and disposable database loss
```

Directly running a scenario module without its generated `SYNCTEST_MANIFEST`
is refused. Never substitute production URLs/tokens/rosters. Do not use the
old hardcoded `matrix-synctest` compose fixture to run these suites; it is
historical and the runner generates all configuration itself.

## Behavior that tests must preserve

- Conversation authority is explicit Private/Share/Direct, with unknown values
  private. Contact sharing is separate. Match JS/Python resolver cases.
- Direct sends retain current manager/target/freshness/rate/identity gates.
  Stale proposals and ambiguous external sends must not be replayed as new sends.
- Scheduled sends are a SEPARATE gate and dispatcher, never a branch of the
  Direct ones. Keep: the fire-time re-read of the teammate's own event, the
  fail-closed cancellation point-read, the fire window (never a late silent
  fire), the attributed non-space target set, the shared rate counter, and the
  rule that losing the local queue cancels schedules instead of replaying them.
  The daemon must never WRITE a `com.jkali.scheduled_send`.
- A disconnected link suppresses forwarding even if environment credentials
  remain. Failed revocation cleanup remains durable and retryable.
- History/discovery gaps, re-share generations and master epochs have separate
  delivery state. Do not clear Direct outcomes when rebuilding an archive.
- iMessage unit tests use injected transports/fake CLI and temporary databases.
  Native executable updates, real grants, and self-send verification belong to
  explicitly authorized hardware tests, not unit or integration fixtures.
- Installer/update tests must retain credentials, configured identity, original
  volume names and the signed iMessage executable. Never fix a test by running
  setup or reset against this workspace's real account state.
- Master API changes require isolated real enrollment/scoping tests. Backup and
  restore tests may delete only a marked disposable test database.

Hardware acceptance on a second Mac and sustained load measurements remain
separate from passing an automated suite. See the implementation plan's gates.
