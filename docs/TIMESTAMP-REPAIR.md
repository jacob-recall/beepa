# Original message dates

The iMessage bridge now preserves the pinned CLI's Unix-millisecond
`timestamp` as message content `com.jkali.origin_ts`, with
`com.beepa.timestamp_source: "imessage"`. The uplink preserves it, including
edits and media retries. The personal and master interfaces use this display
time. Missing/invalid native timestamps retain the Matrix event time as a
fallback with `timestamp_source: "matrix_event"` after mirroring. These fields
are never used for command freshness, replay protection or Direct authority.

## Repair existing imports on a source Mac

Activate the same tested Python and web changes on that Mac and on the master
before applying corrections. A managed installation runs its recorded active
release, not an arbitrary checkout. Keep the existing signed native executable.

From the installation checkout:

```sh
python3 imessage/repair_timestamps.py
python3 imessage/repair_timestamps.py --apply
python3 imessage/repair_timestamps.py
```

Use `--root <installation-root>` when necessary. The first and third commands
are read-only audits. The apply command backs up both SQLite stores with the
SQLite backup API, then journals prior correction metadata before each write
under `.beepa-timestamp-repair/<run>/`. This directory contains private state,
is ignored by Git, and is restricted to the owner. It does not copy message
bodies into the journal or rewrite original Matrix events.

The tool pages native history only for already-mapped chats and matches exact
native IDs through `imessage/state.db`'s `event_map`/`inbound_component` and
uplink destination mappings. It corrects local display timestamps and currently
shared master copies under the existing scoped account. Private conversations,
revoking mappings, unavailable sources and missing destination events are not
recreated. Incomplete pagination is reported. Repeating the tool skips already
correct metadata. Rerun after pending archive deliveries complete if any
destination mapping was unavailable. Reload the interfaces after repair.

## Refused native attachments

The native CLI can return `file:` URLs as well as `asset:` URLs. The bridge
decodes local file URLs and still applies its realpath allowlist. Remote file
hosts and paths outside the allowlist remain refused.

For an existing installation, audit the retained refused-component receipts:

```sh
python3 imessage/repair_attachments.py --root <installation-root>
```

Review the counts. To apply, stop **only this account's** iMessage launch agent,
run the command with `--apply`, and restart the same agent even if repair fails.
Use the detected installed label (`org.beepa.imessage-daemon` or its legacy
equivalent), owner UID and existing plist; do not reinstall or rebuild the CLI.

The apply command requires the agent stopped. It backs up both SQLite stores
and the daemon configuration, journals each stable component transaction under
`.beepa-repair-verification/`, and adds only the current user's native
`Library/Messages/Attachments` and `Library/Messages/StickerCache` directories
to the existing allowlist. It retries exact refused components only in existing,
currently shared, live rooms, preserving native dates and confirmed receipts.
It does not create rooms, change sharing, send native messages or reset ledgers.

Rerun the audit afterward and check each recovered component's local event and
current master delivery mapping. Native loading/unavailable files and private
or retired destinations remain skipped. This tool does not force an iCloud
download or claim that older, never-imported native history is complete.

## Peer compatibility contract

Historical corrections use a separate room **state** event:

```json
{
  "type": "com.beepa.timestamp_correction",
  "state_key": "$existing_event_id",
  "content": {
    "version": 1,
    "source": "imessage",
    "origin_ts": 1704110400000
  }
}
```

The local bridge bot authors the local correction; the scoped teammate authors
the master correction using the mapped master event ID. The personal interface
accepts corrections only from its configured iMessage bot; the master accepts
them only from the room's verified creator. This is a display overlay, not a
replacement message: it cannot invoke a native send or overwrite a later body
edit. Corrections stay in room state, so they survive history pagination. Initial
feed snapshots exclude them from the small message timeline window while still
reading correction state. Existing stored copies remain intact.

Old clients ignore correction state, so both interfaces must have the reader
before a peer's historical repair is visibly effective. No enrollment, account,
consent or send-ledger reset is part of this update.

Validation: `message_timestamps.test.py`, `message_timestamps.test.js`,
`personal_timestamps.test.js`, `master_timeline.test.js`, and disposable
integration scenario `16_original_timestamps` cover preservation, pagination,
repair idempotency, unchanged messages and correction rendering.

## Prompt for another computer

```text
Fix this Mac's Beepa iMessage sync to match Jacob's personal/master timestamp
contract. First identify the installation root, active release, running bridge
and uplink, configured identities, and the signed native CLI's timestamp schema.
Keep the existing account, sharing choices, tokens, native binary and send ledger.

For new messages, preserve the native Unix-millisecond timestamp in message
content com.jkali.origin_ts and com.beepa.timestamp_source = "imessage". Preserve
it through text, attachments, edits, forwarding and media retries. Use Matrix
event time only as an explicit fallback. Keep command freshness and send
authorization based on their existing trusted clocks and checks.

For existing imports, use exact native-message/component -> local-event ->
current destination-event mappings. Page native history to recover timestamps.
Use the compatible imessage/repair_timestamps.py if present; otherwise implement
the same protocol: a com.beepa.timestamp_correction room state event, state_key
equal to the existing event ID, content {"version":1,"source":"imessage",
"origin_ts":<native milliseconds>}. The local bot authors local corrections;
the currently scoped teammate account authors authorized master corrections.
Validate target events and current sharing before writing. Report and skip
unavailable sources, private/revoking destinations and missing targets.

Back up databases and journal old correction metadata before applying. Preserve
original message events and bodies; repairs must be idempotent and must not send
native messages. Ensure the personal UI reads trusted correction state and uses
original dates for ordering/previews. Jacob's master reader is already deployed.
Run preservation, pagination, rendering and no-send/idempotency tests. Activate
the actual installed code, audit, apply the verified correction, audit again,
and verify sync health. Report corrected/skipped counts and remaining gaps.
```
