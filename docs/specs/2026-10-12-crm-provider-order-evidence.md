# CRM provider ordering evidence

Research date: 2026-10-12. This is an implementation contract for response attribution, not authorization to change native send paths. No live accounts, message content, or credentials were read. Distinguish **observed native history** from a proved complete upstream history window.

## Existing evidence to reuse

Bridgev2 stores native message ID, part ID, exact Matrix event ID, native sender ID, Matrix sender, native timestamp in nanoseconds, and the portal's native room/receiver. Query a single verified current owner/login/portal; group parts by native message ID. A pending `~txn:` or synthetic `~fake:` ID is not native-send evidence. SQL row ID and Matrix arrival order are not native ordering. [Pinned bridgev2 message mapping](https://raw.githubusercontent.com/mautrix/go/v0.30.0/bridgev2/database/message.go), [schema](https://raw.githubusercontent.com/mautrix/go/v0.30.0/bridgev2/database/upgrades/00-latest.sql).

The source resolver already proves current account, connection, source, native portal and room ownership. Reuse that proof on both sides of the read-only history query. Return only identities, source order, direction, exact event mappings, and evidence bounds; never message text or authentication data.

## Provider contracts

| Provider | Ordering and exact identity available | Required exclusions and limits |
| --- | --- | --- |
| LinkedIn | The deployed `v0.2608.0` connector uses native `DeliveredAt` milliseconds for incoming events and acknowledged outgoing messages. Native message IDs map to exact Matrix events. | Equal milliseconds remain ambiguous without another documented native order. Edited/recalled events do not become additional responses. |
| WhatsApp | Connector incoming events use native message time; acknowledged sends use the server response time. Both stream orders are seconds. | Random message IDs cannot break a same-second tie. A connected socket does not prove the offline sync backlog has drained. |
| Instagram | Meta's Facebook-style events use native `TimestampMs`; outgoing response IDs encode native time. E2EE variants use their WhatsApp-style timestamp. | Reject timestamp/ID disagreement and outgoing local-time fallback. E2EE native seconds can tie. Keep the two message ID formats distinct. |
| Google Messages | The phone's message timestamp in microseconds is the explicit source stream order. Native IDs arrive in the remote echo, which resolves pending outgoing sends. | Do not use a pending send placeholder. Outgoing re-ID can move the original exact Matrix ID to metadata `OrigMXID`; validate this mapping explicitly. |
| Twitter/X | The connector stores server sequence IDs and sorts messages by numeric snowflake order. The connector explicitly distinguishes these from client UUID/transaction placeholders. | Some outgoing paths initially store `time.Now()` and client IDs; those are not native ordering evidence. Require a resolved numeric server identity within the exact verified portal; reject client UUIDs and integers outside the signed 64-bit contract. |
| Discord | Pinned legacy `v0.7.7` message rows contain native message/attachment IDs, native timestamp in milliseconds, exact Matrix event ID, native sender, channel and receiver. | Do not use attachment ID as message order. Use distinct native timestamp order initially; a snowflake tie-break requires an explicit pinned source/API contract. |

Primary sources supporting this table:

- LinkedIn: [incoming native timestamp and stream order](https://raw.githubusercontent.com/mautrix/linkedin/v0.2608.0/pkg/connector/handlelinkedin.go), [acknowledged outgoing mapping](https://raw.githubusercontent.com/mautrix/linkedin/v0.2608.0/pkg/connector/handlematrix.go).
- WhatsApp: [incoming event identity and time](https://raw.githubusercontent.com/mautrix/whatsapp/e7e5e57d9ed9ea12e3e54c2d9d3dcdc998516fb5/pkg/connector/events.go), [acknowledged outgoing mapping](https://raw.githubusercontent.com/mautrix/whatsapp/e7e5e57d9ed9ea12e3e54c2d9d3dcdc998516fb5/pkg/connector/handlematrix.go), [connection and offline-sync transitions](https://raw.githubusercontent.com/mautrix/whatsapp/e7e5e57d9ed9ea12e3e54c2d9d3dcdc998516fb5/pkg/connector/handlewhatsapp.go).
- Instagram: [native incoming timestamp/stream order](https://raw.githubusercontent.com/mautrix/meta/9e6484d7bb46078fda661b03e2aa28c0a1b4db70/pkg/connector/events.go), [outgoing response timestamp parsing](https://raw.githubusercontent.com/mautrix/meta/9e6484d7bb46078fda661b03e2aa28c0a1b4db70/pkg/connector/handlematrix.go).
- Google Messages: [native microsecond stream order and response handling](https://raw.githubusercontent.com/mautrix/gmessages/9743919f4884327db998fe0f227c073f3f3aceb3/pkg/connector/handlegmessages.go), [pending send and remote-echo handling](https://raw.githubusercontent.com/mautrix/gmessages/9743919f4884327db998fe0f227c073f3f3aceb3/pkg/connector/handlematrix.go).
- Twitter/X: [numeric sequence requirement and outgoing placeholder handling](https://raw.githubusercontent.com/mautrix/twitter/445f793df9ec53f18b52c921cbece2880cabba10/pkg/connector/handlematrix.go).
- Discord: [pinned native message mapping](https://raw.githubusercontent.com/mautrix/discord/v0.7.7/database/message.go).

The image digests in `docker-compose.yml` were inspected using temporary containers with no network and no mounted data. `--version` and embedded Go build metadata identify these source revisions:

| Image | Source revision | Bridge library |
| --- | --- | --- |
| WhatsApp `v26.08` | `e7e5e57d9ed9ea12e3e54c2d9d3dcdc998516fb5` | mautrix-go `v0.30.0` |
| Google Messages `v26.08` | `9743919f4884327db998fe0f227c073f3f3aceb3` | mautrix-go `v0.30.0` |
| Instagram `ig-v26.08` | `9e6484d7bb46078fda661b03e2aa28c0a1b4db70` | mautrix-go `v0.30.0` |
| Twitter pinned `latest` digest | `445f793df9ec53f18b52c921cbece2880cabba10` | mautrix-go `v0.30.1-0.20260828211758-e9466a65f64c` |
| LinkedIn | release `v0.2608.0` | bridgev2 schema |
| Discord | release `v0.7.7` | legacy schema |

All provider links above now point to these exact revisions/tags. The image's short `v26.08` release display corresponds to source tag `v0.2608.0`.

Additional exact source rules: [Twitter stores the server sequence in the portal message event](https://raw.githubusercontent.com/mautrix/twitter/445f793df9ec53f18b52c921cbece2880cabba10/pkg/connector/handletwit.go); [its message sorter compares numeric snowflake IDs](https://raw.githubusercontent.com/mautrix/twitter/445f793df9ec53f18b52c921cbece2880cabba10/pkg/twittermeow/methods/methods.go). [Meta's native ID parser](https://raw.githubusercontent.com/mautrix/meta/9e6484d7bb46078fda661b03e2aa28c0a1b4db70/pkg/messagix/methods/methods.go) independently validates the timestamp against the ID. [Google Messages metadata](https://raw.githubusercontent.com/mautrix/gmessages/9743919f4884327db998fe0f227c073f3f3aceb3/pkg/connector/dbmeta.go) defines the narrow `orig_mxid` and `is_outgoing` fields selected by the adapter.

`ProviderHistoryReader` enforces the audited image digest at runtime: find the active Compose service image, read only its immutable RepoDigests, and require the configured audited digest. Recheck the active image after the SQL read. An unknown or changing image is reconciling; a new bridge release must be independently verified before adding its digest to the allowlist. Queries also fail closed on missing or changed schema. Tests use an explicitly injected synthetic query, not an implicit production bypass.

## Completeness and correction contract

A PostgreSQL snapshot can prove the rows currently committed to that bridge database. It cannot prove that the phone/network has delivered every earlier message. The bridgev2 `backfill_task.is_done` flag is insufficient: its queue marks a task done when the configured maximum batch count is reached. Absence of a task is also not proof of upstream EOF. [Pinned backfill queue implementation](https://raw.githubusercontent.com/mautrix/go/v0.30.0/bridgev2/backfillqueue.go).

Use a separate `observed_native` evidence kind. It must never be presented as a proved complete native-history window. In the common case, an exact acknowledged outgoing mapping and a current, unambiguous native-ordered observed suffix can support an automatic link to its next incoming message. The link remains subject to explicit late-backfill correction. Persist the ID-only evidence and a correction audit whenever a newly observed earlier message changes attribution.

Pending backfill, known missing intervals, unresolved outgoing placeholders, unknown directions, missing incoming Matrix mappings, duplicate native identities with contradictory evidence, and equal source-order values make the affected scope reconciling. Retained links must be marked uncertain while that condition persists. Never skip an unknown earlier message to select a later response. A response must map to the most recent unanswered CRM outreach in the same account, native conversation and bridge generation; it must never make a delivery/read receipt stronger.

The read is bounded to the latest 2,001 SQL parts. If an exact requested outgoing message has aged outside that cache slice, this adapter returns reconciling rather than guessing; the current helper does not separately query historical outgoing mappings. The legacy Discord bridge keeps its forward-backfill lock in process memory, not in the SQL evidence exported here, so its observed-history label must not imply that live backfill is complete. [Pinned Discord backfill locking](https://raw.githubusercontent.com/mautrix/discord/v0.7.7/backfill.go).

A future complete-native-window claim needs a provider-owned recent-history endpoint or durable forward-sync watermark with documented coverage, surfaced by the existing bridge. The SQL cache and connection state alone do not provide that claim.

## Independent validation required

Use disposable schemas and injected transports to test exact outgoing ID mapping, resolved remote echo, multiple parts, crossed owners/logins/receivers, provider reconnect, same-time ambiguity, pending backfill, late inserts/corrections, deleted evidence, and a portal rebound during the read. Verify no body, contact secret, or token enters tracking tables/results. Keep all native send code unchanged.
