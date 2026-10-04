# Beepa — system design and authority

Updated September 4, 2026 to describe the current code contract. The authority
model is unchanged by the reliability and deployment repairs.

## What it does

Each teammate has a private local Matrix hub and bridges for WhatsApp,
iMessage, Google Messages, Instagram, LinkedIn, X and Discord DMs. The master receives
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
| Private (explicitly set on the room) | No new forwarding | No automatic send |
| Share | Mirror authorized history and new events | Teammate reviews, edits, sends or dismisses |
| Direct | Mirror authorized history and new events | Eligible proposals may be sent automatically by the local uplink |
| Absent or unrecognized | Whatever the ACCOUNT DEFAULT says | Whatever the account default says |
| Absent, and no account default (or an unrecognized one) | No new forwarding | No automatic send |

A conversation's setting is per room (`com.jkali.share_override`) and always
wins, in both directions: an explicit Private beats a Direct account default.
A room with no recognized setting takes the account default,
`com.jkali.share_policy`'s `default_level` ∈ `private | share | direct`, which
is absent (therefore `private`) unless the account holder sets it. An
unrecognized override is "no setting", not a share: it falls through to the
default exactly as an absent one does, and can never share a room on its own.

The account default is the ONLY standing setting that decides a conversation.
The per-source policy, the global share-all and a contact profile's share flag
remain dead and do not share anything. Existing migration code can materialize
older choices as explicit room overrides once; it does not enable new rooms.

Consequences of a non-private account default, stated plainly:

- Every conversation the holder has not set individually is shared — including
  conversations that arrive later, and conversations joined unattended by the
  uplink's `invites` stage. Joining such a room now DOES share it.
- Under a `direct` default those conversations are also auto-sendable, behind
  the same twelve teammate-side gates, with no per-conversation confirm.
- Read position (F10) mirrors for every shared conversation, so a default
  mirrors the holder's read position across every defaulted conversation too.
- Clearing a room's setting means "take the default", which under a non-private
  default re-shares it. Making a room private requires writing `private`.
- Setting an existing conversation to Direct in bulk writes EXPLICIT overrides:
  changing the account default back later does not un-Direct them.

Both resolvers read the two inputs through one function
(`resolvedLevel` / `resolved_level`). A policy read the daemon could not
perform is never treated as a revocation: the pass aborts and retries. In the
one place where a read authorizes rather than revokes — the uplink's send-time
point-read — an unreadable policy resolves `private` instead.

Contacts have a separate sharing policy with global/source settings and
per-contact overrides. Sharing a contact does not authorize sending to them
or share their conversations. Grouping a person's rooms changes presentation;
each conversation retains its own authority setting.

Direct requires the configured manager identity, the expected proposal room,
a current mapped conversation, a fresh proposal, a current Direct setting at
dispatch, valid content/target, available rate allowance, and a conversation
that has not moved on since the proposal was made — a later message in that
conversation refuses the automatic send, so an already-answered suggestion
does not become a second reply. Identity changes
suspend automatic sending until the existing reconfirmation requirement is
met. Historical catch-up does not turn stale proposals into fresh sends.
Durable outcome and ambiguity records prevent blindly resending an uncertain
external action. These records and rate counters survive archive rebuilds.

## Scheduled send

The local uplink has **two** send paths into a conversation, bounded by
different things and implemented as separate code rather than one path with a
switch.

1. **Direct auto-send** is bounded by a live manager identity and a live
   consent read, as described above.
2. **Scheduled send** is a message the teammate wrote and scheduled
   themselves. It is bounded by a durable record of the teammate's own intent
   that every later check can only weaken: the daemon re-reads that event at
   fire time and requires the server-stamped author to be the teammate, the
   event type and room to match, no cancellation (a state event keyed by the
   scheduled event, where only a definite "absent" counts and any read failure
   holds), the same send-grade content rules, a fire window that never fires
   late in silence, a 30-day horizon, a source-attributed non-space
   conversation target, the same rate allowance the Direct path consumes, and a
   conversation that has not moved on. There is deliberately no consent gate:
   it is the teammate's own message, so attribution — not sharing — is the
   boundary. There is deliberately no cold-start rule either, because the
   schedule queue lives only in local durable state: **losing that state
   cancels schedules**, and the queue is never reconstructed by rescanning a
   room, so a restored copy can never replay history as real sends.

The manager may put a time on a suggestion. For a conversation that is not
Direct this is only a label: it arrives as an ordinary draft, and accepting it
writes the teammate's *own* schedule, which from then on is theirs. For a
Direct conversation the suggestion is parked and re-evaluated at that time
against every one of the Direct checks on a freshly re-read copy, with a single
difference — freshness is measured against the requested time instead of the
authoring time. The "conversation has moved on" check is *not* moved, so
anything said while the suggestion waited still refuses it. Any refusal files
the ordinary draft; the manager never gets a "send it anyway" affordance.

The teammate-scheduled queue runs above the connectivity gate, so it fires with
the master unreachable or the organization link off. The browser refuses to
create a schedule when its clock disagrees with the server's by more than five
minutes; that is a usability guard, **not** a security boundary — the daemon's
clock is the only one that decides when anything fires, and it re-checks every
condition at that moment.

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
| Scheduled send queue | Arming rows for the teammate's own scheduled messages and for parked manager-timed suggestions; holds no message body and no room identifier, only a hash. Losing it cancels schedules by design |
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
