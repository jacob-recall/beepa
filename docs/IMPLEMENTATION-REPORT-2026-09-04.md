# Beepa implementation report

This implements the approved repair plan while preserving the code's authority
model: explicit Private, Share and Direct; separate contact sharing; scoped
accounts; and all existing manager, content, target, freshness, rate and
identity-reconfirmation checks. The initial audit is preserved in
[Codebase review](CODEBASE-REVIEW-2026-09-04.md).

## What changed

| Area | Implemented behavior |
|---|---|
| Mac apps | Separate Beepa and Beepa Master launchers, using the supplied branded icons. Both install under `~/Applications`. They open the configured app URL without requiring the user to type localhost. |
| Portable identity | Existing identities are adopted; new identities come from an explicit installation value or the current computer account. Generated launch jobs use neutral labels and recorded paths. Historical Matrix event names remain compatible. |
| Remote master | A master gateway serves the manager console and authenticated API routes over Tailscale. Local bootstrap credentials are excluded from remote static serving. Pairing uses the supplied enrollment endpoint. |
| Durable sync | Source event references, history cursors, delivery outcomes, mirror generations and pending cleanup survive restart. Local ingestion continues while the master is offline. Live delivery takes priority over bounded history work. |
| Expanded coverage | Locally retained history is paginated rather than silently capped at 500 messages. Contacts page beyond 2,000 entries. Nested source spaces and international telephone numbering have regression coverage. |
| Sharing removal | Disconnect and per-room revocation stop subsequent forwarding once observed. Failed cleanup remains pending and retries. Historical copies can remain readable; revocation does not promise remote deletion. |
| iMessage | Durable inbound/outbound outcomes, stable Matrix transactions, explicit ambiguous native sends, resumable history and attachment handling. App-only updates preserve the native executable and its recorded location. |
| Master operations | Separate restart, backup, restore and archive-rebuild commands. Retained authority and scoped recovery credentials support master database recovery; a new archive epoch invalidates obsolete destination mappings. |
| Updates | Committed code is staged as an immutable release. A durable journal coordinates writer shutdown, consistent backups, configuration merge, restart and account verification. Compatible code rollback retains current send ledgers and recovery revocations. |
| UI structure | Conversation watchers have session/generation ownership. Message previews and source definitions are shared pure modules. Settings shows aggregate sync backlog, errors and last-success times. |
| Verification | Native unit discovery, consent conformance, generated two-server integration stacks, fault-injection tests and a pinned CI workflow replace dependence on the developer's live accounts. |

## Operating the installed system

Use [Master operations](MASTER-OPERATIONS.md) for restart, backup, restore and
archive rebuilding. A normal restart preserves identities and pairings. A
database restore uses retained current revocation records, changes the archive
epoch, and leaves remote ingress off until local verification. Losing all
authority/recovery material requires fresh pairing.

Use [Updates](UPDATES.md) to propagate a release through a canonical upstream
repository and each person's fork. From a clean installed checkout:

```sh
git pull --ff-only
./update.sh
./update.sh --apply
```

The middle command inspects the release. Routine updates preserve the accounts,
bridge sessions, native executable and installation identity. An expired
provider session may still require that provider's login flow. The first
transition from unmanaged code has no prior managed release to roll back to.

## Verification and release limits

All 63 discovered unit scripts passed in the final publication run, along with
114,235 JS/Python consent-conformance vectors (Python 3.9.6 and Node 26.8.1).
CI separately pins Python 3.11.11 and Node 22.14.0; published commits trigger
the [hosted verification workflow](https://github.com/jacob-recall/beepa/actions/workflows/verify.yml).
The sync scale fixture uses 100 rooms and 100,000 event references after a
simulated 72-hour outage. All 15 disposable sync scenarios passed. Additional
integration checks exercise actual Synapse
accounts, history retention, master outages, enrollment, roster preservation,
database replacement and backup restoration. The production uplink recovered
its scoped account after database loss; current Direct outcomes and rate
counters survived, and background recovery did not rewrite the user's
connection control record. Test roots and container projects
are uniquely marked; tests refuse production endpoints and state paths.
The disposable nginx test also passed: login/discovery files created or
atomically replaced after container startup were served correctly from external
runtime storage. Temporary Git repositories exercised fast-forward fork
propagation and divergence refusal.

Hosted Linux verification exposed a bootstrap ownership issue masked by Docker
Desktop. The views container now runs as the installing user while bootstrap
files remain mode 600. Regression checks cover both host ownership and native
Linux UID 1001. iMessage contact-name lookup also follows retained installation
state when code moves into a staged release.

Live iMessage checks also passed on the owner's Mac: local hub → iMessage →
inbound return, and manager over Tailscale HTTPS → Direct proposal → local hub
→ iMessage → inbound return. Each fresh nonce appeared exactly once outgoing
and once incoming in the native source. The manager could read both mirrored
directions over Tailscale, with correct sender attribution. The native binary's
inode and SHA-256 were unchanged. Sanitized evidence is in
[the live validation record](review-evidence/2026-09-04-live-validation.json).

The first live check exposed a receive bug missed by the original fixtures:
late inbound messages can leave the chat timestamp unchanged. A bounded,
periodic rescan fixed it, and the original nonce arrived without a resend.
Regression cases cover late arrival, echo suppression and fair rescan timing.

Fresh-machine iMessage authorization, actual laptop sleep/wake, other networks'
live sends and a second-device Tailscale canary remain outside this verification.
No permission grant is assumed to transfer to another Mac.

Backup commands create protected local files containing secrets. An encrypted
off-device destination, retention policy and scheduled execution must be
configured for the deployment. The owner has been asked for these choices and
the second test Mac/account.

The owner subsequently authorized topical commits and publication after live
verification. The iMessage and uplink daemons were restarted on the repaired
source for those checks; account state and the native executable were retained.
The remaining installer/update changes were exercised with disposable fixtures,
without reprovisioning the live accounts.
