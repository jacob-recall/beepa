# Tests and validation

Run `tests/run.sh` (or the configured host-runtime Python with `tests/run.py`).
The runner discovers every `tests/unit/*.test.py` and `*.test.js`, then runs
114,235 deterministic/fuzz consent vectors through both real resolvers.
`--unit-only` omits conformance. Install the hash-locked `requirements-host.txt`
into a virtual environment when testing without an installed Beepa runtime.
The CI workflow pins Node/Python versions and records actual runtime versions.

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
