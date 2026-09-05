# Installation, recovery and release contracts

The code authority model remains fixed: explicit Private/Share/Direct;
separate contact sharing; scoped Matrix accounts; current manager binding,
freshness, target, content, ambiguity and rate gates. Existing protocol event
names and Matrix namespaces stay compatible.

## Installation

`install_config.py` owns `.beepa-install.json` format 1. Its `install_id`,
`local_localpart`, `local_server_name`, `compose_project`,
`master_compose_project`, and `imessage_cli_path` survive updates. `roles` is
additive so one computer can host both teammate and master. Existing identity
is adopted and must agree with any explicitly supplied value. First installation
can use a supplied identity or the computer account; credentials are generated
by provisioning and are not the computer password.

`state_root` and `code_root` are distinct concepts. Legacy stores are adopted
in place. New Mac installs default to
`~/Library/Application Support/Beepa/<install_id>` and
`~/Library/Logs/Beepa/<install_id>`, with `BEEPA_STATE_ROOT` and `BEEPA_LOG_ROOT`
overrides. Managed updates stage code under `.beepa-update/releases/<commit>`
and set `BEEPA_INSTALL_ROOT` to the retained state root. Native iMessage stays
at its recorded path. Runtime dependencies live in a versioned private venv.
Moving legacy data into another directory is a separate migration, not an
automatic consequence of a repository rename or account change.

Host jobs use `org.beepa.*` labels and structured plists. Their environment
points to exact state/config paths; Python services must not infer mutable
state from their staged source directory. Legacy labels are stopped during
installation of their replacement.

## Master binding and recovery

`master_authority_id` identifies the retained master authority.
`master_data_epoch` changes after archive rebuild or backward restore.
Neither changes on ordinary restart. A mirror room generation identifies one
destination history. Delivery and discovery mappings use the destination;
Direct outcomes/rate counters remain a separate persistent ledger.

`com.jkali.master_link` retains existing credential fields and adds optional
`master_enroll_url`, `master_authority_id`, `master_data_epoch`, and `enabled`.
An explicit disabled/empty link overrides legacy environment credentials.
Only the user's connection action writes that control record. Background
recovery retains refreshed credentials in a private SQLite runtime overlay,
bound to the exact control-record fingerprint. Disconnecting or replacing the
link invalidates the overlay; a late recovery cannot write over the user's
choice. Legacy environment pairings use their original configuration binding.
Legacy clients can ignore additive fields. Recovery uses an explicitly known
enrollment URL; it does not guess a remote host's localhost address or port.
Manifests advertise wire version 1; omitted metadata is the legacy version 1
contract, while an unsupported advertised version stops automatic recovery.

| Endpoint | Authorization and result |
|---|---|
| `GET /enroll/manifest` | Public nonsecret authority/epoch metadata over the configured trusted transport |
| `POST /enroll/recovery/issue` | Existing scoped teammate bearer; body has `install_id` and client-generated `recovery_token`; records only verifier/binding |
| `POST /enroll/recovery` | Matching retained authority, installation and credential; returns only that teammate's fresh scoped enrollment data |

Recovery credentials are at least 32 random URL-safe characters. The master
stores verifiers and revocations in `master/recovery.local.json`, outside the
resettable Synapse database. Reissuing a credential cannot bind one installation
to another teammate. Revoked pairings are refused. Missing latest recovery
records require fresh enrollment; matching a hostname or username is insufficient.

History catch-up never clears external-send outcomes. Token rotation can change
server transaction-deduplication scope, so unresolved accepted-send ambiguity
must be reported rather than retried blindly.

## Release compatibility

`release.json` describes the package's `state_version`, `wire_version`, and
supported read versions. These are package compatibility contracts, distinct
from individual SQLite schema versions. Migration modules retain their own
idempotent schema transitions. Initial managed packaging is release 0.2.0,
state/wire contract 1 with legacy adoption fixtures.

The updater refuses incompatible releases, stages immutable committed source,
backs up quiesced state, then journals activation. Compatible rollback switches
code with current state; it does not restore old Direct/iMessage receipts or
overwrite newer recovery revocations. A first unmanaged installation has no
recorded previous managed source release and must not promise automatic code
rollback until one exists. N/N−1 and skipped-release checks are release gates,
not permission to assume arbitrary future version compatibility.
