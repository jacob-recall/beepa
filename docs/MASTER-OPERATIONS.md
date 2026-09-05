# Operating a personal-laptop master

The master serves the manager console and APIs through Tailscale. The local gateway listens on `127.0.0.1:8017`; Tailscale HTTPS `443` forwards to it. Matrix stays on loopback `8018`, enrollment on `8019`, and the existing HTTPS `8443` enrollment address remains supported. Open `https://<master-magicdns>/apps/master/` from another tailnet device. The remote console uses its own origin and never downloads the local manager bootstrap file.

Keep Docker and Tailscale running in the logged-in macOS session. Sleep/offline periods pause access; durable teammate queues catch up when both ends return. Recovery depends on retained local source history. It cannot reconstruct messages never imported from a provider. Use uplink backlog/coverage status to distinguish a running process from completed synchronization.

## Restart and routine updates

From the installation checkout:

```sh
python3 master/lifecycle.py status
python3 master/lifecycle.py restart
```

Restart actually restarts only the master database/Synapse, verifies readiness, and reinstalls the master enrollment/gateway LaunchAgents on macOS. It preserves the authority ID, data epoch, pairing, account sessions, teammate databases, and send ledgers. It refuses while a backup/restore/rebuild is unfinished. It does not change Tailscale Serve mappings; after local verification, run `./master/tailscale-serve.sh` if mappings need repair.

The install manifest resolves the real state directory, including external macOS application support storage. Lifecycle operations use the active installed release, even when invoked from a checkout containing a newer unapplied pull. Do not move or edit the active release directory by hand.

For a release distributed to other people's repositories, keep installation state and secrets out of Git; preserve their upstream relationship and merge custom code through ordinary Git branches. After publishing a tested compatible release, recipients use:

```sh
git pull --ff-only
./update.sh
./update.sh --apply
```

The first updater command inspects compatibility; apply stages and activates code with a resumable journal. Pulling code alone does not activate an installed service release. Keep the published `release.json` state/wire compatibility declarations accurate. Ship incompatible migrations as separately reviewed releases with proven recovery. `./update.sh --rollback` selects a retained compatible code release; it does not roll back account state or send outcomes. Do not use `reset.sh` as an update or restart command.

## Back up and verify

Choose a new protected directory outside the master state, preferably on encrypted storage outside the laptop:

```sh
python3 master/lifecycle.py backup --output '/Volumes/Protected/Beepa/master-2026-09-04'
```

This briefly pauses master writers, drains gateway requests, exports PostgreSQL, archives master media/config/signing and recovery material plus install/release metadata, checks hashes and archive paths, then resumes the services that were running. Backup files contain credentials and personal data. The command creates restrictive filesystem permissions; it does not encrypt files or upload them. No teammate database or native iMessage executable is copied or reset.

Keep several dated backups, including one from before an update, and perform periodic disposable restore drills. Scheduled backup activation and deletion/retention remain pending the owner's destination and retention choice; no automatic pruning is enabled. A backup checksum proves integrity, not successful disaster recovery. The current restore flow targets an installed master with a compatible active release and Docker environment; the archive is not a one-command bare-machine installer.

Preserve the latest `master/recovery.local.json` and current enrollment state separately from historical archive rollback. They record pairing revocations and consumed codes that a snapshot may predate. Losing the latest recovery records requires fresh pairing rather than trusting stale credentials.

## Restore a snapshot

After choosing the correct authority's verified backup:

```sh
python3 master/lifecycle.py restore --backup '/Volumes/Protected/Beepa/master-2026-09-04' --confirm-restore
python3 master/lifecycle.py status
```

Restore replaces only master data. It keeps the latest available recovery registry, post-backup valid enrollments, consumed enrollment codes, current public endpoints, and the target database's current password. It allocates a fresh data epoch under the same authority, invalidates restored managed Matrix sessions, deactivates revoked teammates, and reprovisions currently valid scoped accounts. Upgraded paired clients recover their own scoped sessions automatically. The manager may need to sign in again. If the current recovery registry is missing, all snapshot pairing credentials are quarantined and fresh enrollment is required.

Restore deliberately turns off both master Tailscale mappings before restoring historical authorization and leaves them off. Inspect local health and authenticate with current credentials before reopening:

```sh
curl --fail http://127.0.0.1:8018/health
curl --fail http://127.0.0.1:8017/health
./master/tailscale-serve.sh
```

Then verify the remote manager console and teammate synchronization status. Ordinary restart preserves the epoch; restore changes it so clients rebuild stale destination mappings. Restoring does not clear teammate Direct dispatch outcomes, ambiguities, rate counters, or provider sessions. A changed authority still follows the existing reconfirmation rules.

## Refresh the archive without replacing accounts

```sh
python3 master/lifecycle.py rebuild-archive --confirm-rebuild
```

This places the master in maintenance, turns off ingress, and discovers explicitly managed archive rooms using existing scoped teammate credentials. It durably records each room's retirement: unlink from its space, remove the manager, then leave as the owner. After every retirement succeeds, it changes the data epoch. Accounts and their access tokens remain unchanged; currently authorized local shares rehydrate through the uplinks. Private decisions continue to govern forwarding. Unmarked unrelated rooms are retained.

Retirement uses supported room operations, not SQL table deletion or hard purge. It does not promise deletion of server-retained history or previously downloaded copies. Inspect status and local health, then explicitly reopen Serve as above. Do not use archive rebuild to fix a missing signing/authority bundle; loss of that bundle needs fresh installation/pairing recovery.

## Interrupted operations

The state directory holds `master/runtime/operation.json` and `master/runtime/maintenance`. The journal records original running services and per-room retirement progress. While maintenance is present, the gateway returns `503`. A failed restore/rebuild or failed resume leaves maintenance enabled; competing operations and ordinary restart refuse.

Fix the reported filesystem, Docker, credential, or health error, then repeat the **same** command with the same backup path/output. A verified completed backup whose resume failed is reused without a second dump. Interrupted archive retirement resumes its recorded steps before changing the epoch. Do not delete the journal/maintenance marker, enable Serve prematurely, or run a factory reset to clear an error. Preserve the error and journal for diagnosis; journals contain operational room/account identifiers and should stay private.

## Validation and iMessage acceptance

`tests/integration/run.sh --lifecycle` creates and destroys its own marked two-homeserver Docker stack. The drill restores a real database snapshot and proves post-backup token/pairing/code revocation, retained new enrollment, password rotation, fresh epoch, archive retirement, and unchanged local event/dispatch ledger/native fixture. Fault-injection unit tests cover interruption and failed health/agent restart. CI runs this drill separately from synchronization and recovery suites.

These tests do not validate macOS Messages authorization. Ordinary app upgrades preserve the installed native executable/path/signature. New native installs retain the pinned verified download route; a native binary upgrade is a separate hardware acceptance event. Test on a fresh Mac/account with explicitly approved self-chat destination, validate Full Disk Access/Accessibility behavior and actual inbound echo, and never infer delivery from the outgoing bot event. No real Messages reads, native invocation, or recipient sends are part of the disposable suite.
