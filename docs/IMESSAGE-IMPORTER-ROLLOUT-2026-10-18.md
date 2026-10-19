# iMessage importer patch: 0.3.1

Use this supplement with [the October 18 rollout](PEER-ROLLOUT-2026-10-18.md).
It applies to existing installations. It does not enroll accounts, replace the
signed native CLI, change sharing, or authorize test sends.

## What changed and why

The master reported Elliot with 59 unresolved imports and David with 122 while
both native pollers and master sync kept advancing. Those totals alone did not
identify a throughput bottleneck. The following defects were reproduced in
isolated fixtures:

- Repeated failures consumed the per-chat message budget before later messages
  received a turn. Retries now retain attempt times and exponential cooldowns
  (15 seconds to 15 minutes), so untouched work can proceed. Cooldowns survive
  daemon restart. Completed component transaction receipts still prevent duplicate
  Matrix messages after partial success or a lost response.
- Messages missing from the returned native history stayed unresolved with no
  exact-message recovery path. The importer now performs at most four exact-ID
  source lookups per poll, each with a five-second timeout, across all mapped
  conversations. ID and returned thread identity are checked. Missing records
  remain unresolved with backoff; nothing is silently marked complete.
- A source-unavailable record was displayed as an actively draining queue.
  Health now separates retryable imports, unavailable source messages, missing
  attachments and deferred attempts. Per-conversation scan failures remain
  visible even when the outer poll loop succeeds. Legacy totals are described as
  unresolved, with the reason unavailable, rather than claimed progress.
- Refused attachment components are terminal safety records, not pending work.
  The user app and master `/ping` now label them accordingly. They remain visible
  because some history may be incomplete.

The additive `inbound_retry` table contains IDs, attempt counts and timestamps
only. This patch remains state version 2 / wire version 1. Older 0.3.0 readers
ignore the new table and health fields, but do not provide the improved scheduler.
0.2.x remains an incompatible rollback target. No database reset is needed.

## Instructions to send to the agent on each Mac

Supply the published patch SHA with this message. Substitute **David** or
**Elliot**; their master identities are `@david:master` and `@notelliot:master`.

> Update this existing Beepa installation to the supplied 0.3.1 patch SHA. Read
> AGENTS.md, run bd prime, then follow docs/PEER-ROLLOUT-2026-10-18.md and
> docs/IMESSAGE-IMPORTER-ROLLOUT-2026-10-18.md. Confirm the stored account identity
> before acting. Preserve enrollment, tokens, sharing choices, CRM batch opt-in,
> pending/uncertain sends, all journals, state roots and the signed iMessage CLI.
> Use the existing owner session and supported updater; a checkout pull alone
> does not activate a staged release. Do not create another daemon/poller, reset
> state, clear pending records, weaken attachment path checks, or send test
> messages. Run the read-only importer diagnosis before activation and after at
> least two polling cycles. Investigate the actual remaining reasons locally and
> report only counts/error categories, active SHA, verified service ownership,
> native polling health and fresh master ping evidence. Do not paste credentials,
> message text, handles, native IDs, attachment paths or database contents.

## Source-Mac procedure

1. Record the current install manifest, state root, active release, service labels
   and PIDs using the existing rollout procedure. Check for an unfinished updater
   journal before pulling. Do not replace Elliot's or David's credentials.
2. Fetch/fast-forward the intended branch to the supplied SHA without discarding
   local adaptations. Verify the source manifest. From the new checkout, run the
   diagnostic against the **existing installation root**, before activation:

   ```sh
   python3 imessage/diagnose-importer.py --root /absolute/existing/install/root
   ```

   The helper follows `.beepa-install.json` to retained state. For a verified
   custom daemon state directory, use `--state-dir /absolute/imessage/state`.
   It opens SQLite read-only, supports pre-patch databases, and never invokes
   the CLI, writes schema, or creates a missing database. Save its count-only
   output for comparison; no copying a live SQLite file without its WAL.
3. Let active sends/requests settle. Use `./update.sh` and then `./update.sh --apply`
   as described in the base rollout, with the existing protected backup. The
   source daemon creates its additive table on startup. Confirm exactly one
   verified iMessage daemon and one uplink using the activated code root. Preserve
   the configured CLI hash/inode and all native permissions. Refresh the user UI.
4. Run the diagnostic again after two polling cycles, then after a cooldown when
   work remains. Check that `retry_scheduler_installed` is true. Verify the master
   receives `inbound_retryable`, `inbound_source_unavailable`,
   `inbound_attachment_unavailable`, `inbound_deferred` and `poll_failed_chats`
   through the account's source-authored health event. The native health publisher
   and uplink cache can take several minutes to publish changed counts.
5. Compare actual reasons, not just whether the total reaches zero:

   | Counter / reason | Investigation |
   |---|---|
   | `pending` | Fresh work not attempted yet; repeated samples should show attempts/progress. |
   | `source_unavailable` | Exact native row absent or unreadable; inspect CLI read outcomes locally. Check source availability, never another account's database. |
   | `attachment_unavailable` | Valid allowed attachment path is not currently a file; check download/availability and native permissions locally. Do not widen allowed roots just to suppress a refusal. |
   | `delivery_failed` | Check local Matrix connectivity, membership and daemon error categories; stable component transactions must remain intact. |
   | `reconciliation_failed` | Text may already be imported; inspect edit/reaction errors. Do not resend the original message. |
   | `terminal_attachment_refusals` | Retained invalid/outside-allowlist component records. Not a queue to drain; never delete these to manufacture success. |
   | `pending_without_portal` | Exact recovery requires a verified existing portal. Investigate missing mappings; do not invent/relink a conversation. |
   | `retry_deferred` | Expected cooldown, not a stalled worker. Check again after its scheduled delay; do not restart to bypass it. |

Return before/after diagnostics and whether timestamps continue advancing. A
zero master queue proves delivery of imported shared messages, not native history
completeness. If the source no longer supplies an item, report the remaining gap
explicitly. A fresh end-to-end send test requires a separately authorized recipient.

## Central-master activation

The master needs the patched `master/control-domain.mjs` and
`master/control-core.mjs` to explain the new fields. Verify the existing control
container by immutable ID and its retained `/state` mount. Let commands and
deliveries settle, verify its mounted source matches the committed files, then
restart that worker alone. If file mounts still reference old content, use the
documented control setup with the recorded install/master state roots; preserve
configuration, pairing, deduplication, unknown outcomes and all keys. Do not
start a second Telegram worker. No gateway allowlist change is needed for this
patch. Teammate-only Macs must not run master setup.

## Verification scope

Regression coverage exercises restart-preserved cooldowns, progress past failed
messages, recovery of missing attachments without duplicate text, bounded/fair
exact lookups, mismatched source IDs, unavailable records, terminal refusals,
partial scan failures, legacy health and owner-verified count-only propagation.
The diagnostic has tests for read-only behavior, old/new schemas and redaction.
Peer-specific stuck record causes require the source-side checks above; tests
with synthetic data cannot establish whether Apple still has a particular item.
