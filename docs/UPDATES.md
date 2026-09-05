# Applying releases without disconnecting accounts

Keep a canonical upstream repository and a `stable` deployment branch. Each
person's repository should preserve that Git history; deployment values and
credentials belong in local state, not tenant-specific source edits. Propagate
the tested upstream release to the person's fork before asking them to pull.

From the installed checkout:

```sh
git pull --ff-only
./update.sh
./update.sh --apply
```

The first update command inspects compatibility and tracked modifications. The
second explicitly applies the reviewed release. A divergent fork or modified
tracked file stops the operation; the updater never resets or force-pushes it.
Updates do not run provisioning, log out accounts, delete volumes, rebuild the
iMessage CLI or change its authorization identity.

The updater stages the exact committed Git tree, pulls images, records a
durable operation journal, stops the installation's writers, and backs up
PostgreSQL plus runtime files before applying configuration and restarting.
Standalone SQLite stores use the SQLite backup API so committed WAL rows are
included even after an abrupt daemon exit.
Python services and local web assets then run from the recorded staged release
under `.beepa-update/releases/`, so a later Git pull does not replace their
active code. Existing mutable paths and Compose project names are preserved.
The master gateway receives the same staged code root.

Local service health and existing-account `whoami` are checked. Remote sync
verification remains pending until the uplink reports authenticated delivery
progress; a sleeping master does not cause re-enrollment. Bridge-provider
session expiry is separate from an update deleting account state.

If interrupted, rerun **the same release's** `./update.sh --apply`. Its journal
preserves the original completed backup and resumes activation. A failure after
writers stop can leave those services stopped until the update is resumed;
the error and `.beepa-update/journal.json` identify that operation. Do not pull
another release over an unfinished update or use `reset.sh` to recover it.

Backups are under `.beepa-update/backups/<commit>-<attempt>/`, with checksums, PostgreSQL
dumps and a runtime archive. They contain account secrets and should be copied
only to the operator's protected backup destination. Automatic retention and
off-device scheduling are separate operator configuration. A code update
backup is not a demonstration that a cross-machine restore has succeeded.

After a successful managed update, `./update.sh --rollback` can activate the
previous managed code only when its release metadata supports the active state
version. It never restores old Direct/iMessage send ledgers or an old recovery
revocation registry. The first transition from an unmanaged installation has no
recorded previous source release; it cannot promise automatic source rollback.

## Configuration and identity

`.beepa-install.json` records the stable installation/account identity, roles,
paths, Python executable and Compose projects. Existing identity is adopted;
new installations accept `LOCAL_LOCALPART`/`LOCAL_DISPLAYNAME` or derive the
initial account suggestion from the OS session. Changing GitHub ownership or
the OS username later does not change the Matrix account. Conflicting existing
identities stop setup for reconciliation.

New installations keep mutable data under
`~/Library/Application Support/Beepa/<installation-id>/` and host logs under
`~/Library/Logs/Beepa/<installation-id>/`. Set `BEEPA_STATE_ROOT` and/or
`BEEPA_LOG_ROOT` before the first install to choose other locations. The
installer records these paths and creates local checkout projections for
compatibility; services use the actual state paths, including SQLite sidecars.
An explicit `BEEPA_MASTER_STATE_DIR` is also recorded for an independently
placed master store. A later setup/update preserves the recorded paths. Setting a different data
root on an existing installation is refused rather than moving an authorized
native executable or splitting account state.

Existing runtime files stay in their current locations. Moving those files or
the already-authorized native iMessage executable is not an update operation.
The legacy `com.jkali.*` Matrix event names are retained for stored-data
compatibility; installed launch agents use `org.beepa.*` and replace their
legacy installed label without running two instances.

Host dependencies are installed from `requirements-host.txt` into a versioned
private virtual environment. The installer records its Python path; system
Python packages are not modified. Tests use `BEEPA_PYTHON` if supplied, then
the manifest's interpreter. Phone normalization uses the pinned
[python-phonenumbers core metadata](https://github.com/daviddrysdale/python-phonenumbers)
through the smaller [phonenumberslite package](https://pypi.org/project/phonenumberslite/9.0.38/).
National numbers require the Mac's region or explicit `PHONE_REGION`; extension
and post-dial targets are excluded from automatic person matching.

Hub configuration uses snapshots under `.beepa-config/defaults/`; master
Synapse configuration uses `master/.beepa-config/defaults/`. Operator
edits survive reruns. Optional complete override files can be stored under
`.beepa-config/overrides/` with the same relative path, for example
`whatsapp/config.yaml`. Disjoint upstream changes merge; overlapping changes
stop the entire configuration activation and write a proposed upstream file
under `.beepa-config/conflicts/`.

Review that candidate. To adopt the upstream value, merge it into the effective
configuration (and override file if one exists), then rerun. To deliberately
retain your local value for a reported conflict:

```sh
python3 hub/managed_config.py . --accept-local whatsapp/config.yaml
./update.sh --apply
```

For master conflicts, use the same command with the recorded master data
root and `synapse/homeserver.yaml`. Updates never mint missing master authority
keys or apply password-key environment overrides; missing authority state
requires the reviewed recovery workflow.

Local web assets come from the staged release. Bootstrap sessions and helper
discovery remain in a separate runtime directory mount, so files created or
atomically replaced after startup are visible. Only the existing three JSON
URLs are served by the loopback web interface; the remote master gateway
continues to exclude local bootstrap credentials.

Only services with changed effective configuration receive an explicit
configuration restart. Image or service-definition updates follow Compose's
normal recreation behavior while retaining existing volumes.
