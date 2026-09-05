# Beepa — system design and authority

Updated September 4, 2026 to describe the current code contract. The authority
model is unchanged by the reliability and deployment repairs.

## What it does

Each teammate has a private local Matrix hub and bridges for WhatsApp,
iMessage, Google Messages, Instagram, LinkedIn and X. The master receives
copies of conversations that the teammate explicitly shares. The master can
run on a mostly-on personal Mac, reached through a private Tailscale network.
When either computer is offline, live access waits; retained source history
can catch up when connectivity returns.

The master holds scoped Matrix credentials, not teammates' network login
sessions. A manager can create proposals. In a conversation explicitly set
to **Direct**, the teammate's uplink can automatically turn an eligible
manager proposal into an external message using the teammate's local account.
This capability is intentional and remains governed by the existing gates.

## Conversation authority

| Conversation setting | Copy to master | Manager proposal behavior |
|---|---|---|
| Private, absent, or unrecognized | No new forwarding | No automatic send |
| Share | Mirror authorized history and new events | Teammate reviews, edits, sends or dismisses |
| Direct | Mirror authorized history and new events | Eligible proposals may be sent automatically by the local uplink |

Conversation settings are explicit per room (`com.jkali.share_override`).
Global/source standing policies and a contact profile's share flag do not
implicitly share a conversation. Existing migration code can materialize
older choices as explicit room overrides once; it does not enable new rooms.

Contacts have a separate sharing policy with global/source settings and
per-contact overrides. Sharing a contact does not authorize sending to them
or share their conversations. Grouping a person's rooms changes presentation;
each conversation retains its own authority setting.

Direct requires the configured manager identity, the expected proposal room,
a current mapped conversation, a fresh proposal, a current Direct setting at
dispatch, valid content/target, and available rate allowance. Identity changes
suspend automatic sending until the existing reconfirmation requirement is
met. Historical catch-up does not turn stale proposals into fresh sends.
Durable outcome and ambiguity records prevent blindly resending an uncertain
external action. These records and rate counters survive archive rebuilds.

## Copies and revocation

Mirror rooms are owned by the teammate's scoped master account. The manager
cannot send ordinary Matrix messages into those mirror rooms. Proposals use
a dedicated room and event type. That room boundary remains distinct from
the local uplink's guarded Direct execution capability.

Setting a room Private or disconnecting suppresses subsequent forwarding.
Cleanup is durable: unlink the mirror, remove manager membership, then leave;
failed steps remain visible and retryable. A request already in flight cannot
be recalled. The application does not purge the remote database, retract
screenshots or downloaded messages, or guarantee removal of previously
readable history. Re-sharing creates a new mirror generation with its own
delivery mapping, so the previous generation's receipts do not suppress it.

## Durable state and recovery

| State | Purpose |
|---|---|
| Installation manifest | Stable installation ID, existing Matrix identity, role(s), paths, Compose projects and runtime |
| Local Matrix/bridge stores | Source history and network sessions |
| Uplink lifecycle/history queues | Pending event references, source pagination, generation mappings, cleanup and incomplete outcomes |
| Direct outcome ledger | Records accepted/refused/uncertain proposals and rate accounting; retained across rebuilds |
| iMessage journal | Inbound component receipts and outbound event claims/outcomes; retries do not imply delivery |
| Master recovery registry | Stable master authority, data epoch, scoped installation verifiers and revocations outside the archive DB |

A sync cursor alone does not prove delivery. Ingestion records durable work;
remote delivery commits separately. Limited timelines create recovery gaps.
History discovery and delivery are paginated and resumable. New events,
proposals and revocations can proceed while a large history catch-up runs.
All locally retained history is eligible under current sharing settings;
provider history that a bridge never imported cannot be reconstructed.

The master authority ID and data epoch have different meanings. Restart keeps
both. Rebuilding/restoring archive data changes the epoch, causing destination
state to reconcile while preserving Direct outcomes. Replacing the authority
requires fresh pairing and the existing Direct reconfirmation. A retained
per-install recovery credential can obtain only that installation's scoped
account; it grants neither manager privileges nor new Direct permissions.
Revoked pairings cannot silently recover.

A backup must include the database, media, signing and derivation material,
roster, configuration, recovery metadata and release information. An old
backup must not resurrect later-revoked access. Restore keeps current recovery
records and invalidates restored managed sessions before reopening access.
If current pairing records are unavailable, old recovery credentials are
quarantined and fresh enrollment is required. Teammates' network sessions
remain local and are not reset by a master restore.

## Desktop and network entry points

**Beepa.app** opens the teammate interface; **Beepa Master.app** opens the
manager interface. Both live in the installing user's Applications folder
and use the existing browser session behavior. They do not take over native
iMessage authorization or contain credentials.

The local interface stays on loopback. The master gateway provides an
independent manager frontend and same-origin API routes for tailnet access.
It serves an explicit static-file allowlist, excludes local bootstrap token
files, and forwards existing Matrix/manager authentication. Network membership
does not itself make a teammate the manager. Native cookie, Contacts and
iMessage helpers run on the teammate's own computer.

An existing Matrix identifier ending in `:localhost` is a persistent namespace,
not the address of the browser's machine. Network URLs are configured separately.
Historical `com.jkali.*` protocol keys remain compatible identifiers; they do
not select an installation's user. Runtime user identity comes from existing
configuration or a supplied/OS-derived first-install value.

## Updates and limits

Source releases, operator settings, credentials and message state have
separate lifetimes. The updater stages a committed source release, checks
compatibility, quiesces writers, backs up state and records activation progress.
Repeat application resumes safely. Compatible rollback switches code without
rolling delivery outcomes backward. Routine updates retain the signed iMessage
executable at its authorized path. New Macs must grant their own permissions.

See [update operations](UPDATES.md), [implementation plan](IMPLEMENTATION-PLAN-2026-09-04.md)
and [original review](CODEBASE-REVIEW-2026-09-04.md). Automated tests do not certify
new-device macOS permissions, provider delivery, or unlimited laptop capacity.
The hardware and scale release gates distinguish those checks from unit tests.
