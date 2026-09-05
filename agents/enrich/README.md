# Conversation number enrichment — A1: read-only resolver

`number_resolver.py` maps each 1:1 conversation's Matrix `room_id` to the
counterpart's real E.164 phone number (or, for iMessage, an email
address), by reading the bridge databases directly. This is the first
piece of the "conversation number enrichment" feature. It is exposed over
a loopback endpoint (`POST /enrich/numbers` in
`session-connect/connect_server.py`), and the Hub auto-merges contacts
that share a number via `shared/model/contacts.js`'s `autoMergeByNumber()`.

**This module is read-only.** Every query below is a `SELECT`; nothing
here ever writes to a bridge database, a bridge config, or a bridge
container.

## Sources

| Source | Where | Query |
|---|---|---|
| WhatsApp | postgres `mautrix_whatsapp` in `the configured Compose postgres service` | `portal` (1:1, `room_type='dm'`) joined to `whatsmeow_lid_map` on the LID extracted from `other_user_id` (`lid-<LID>`), giving the real `pn` phone |
| Google Messages | postgres `mautrix_gmessages`, same container | `portal` joined to `ghost`, reading `ghost.metadata->>'phone'` |
| iMessage | sqlite `imessage/state.db` (bridge-local map, **not** macOS `chat.db`) | `map.chat_id`/`map.room_id`; 1:1 chats are shaped `any;-;<handle>` |

Postgres access goes through `docker exec <container> psql -U matrix -d
<db> -tAc "<query>"` — a fixed argv list (no shell string), same
`subprocess.run(..., shell=False)` pattern as `session-connect/connect.py`'s
`api()`. iMessage is read directly via the sqlite3 stdlib, opened
read-only (`file:...?mode=ro`).

## Normalization rules

- **`_norm_phone(raw)`** uses the shared `phone_numbers.py` metadata parser.
  This resolver has no region signal, so a freeform value must carry an explicit
  `+` or `00` international prefix. Bare digits are never inferred from length.
  Extensions, post-dial targets and malformed values are excluded from matching.

- **`_norm_wa_pn(raw)`** explicitly marks WhatsApp's `pn` field as a provider ID
  whose digits include the country code. Only that attested source gets a `+`
  prepended before metadata validation.

- **gmessages phone filter** accepts only an explicit `+` phone validated by the
  shared parser. SMS short codes and RCS business identifiers are excluded.

- **`_parse_imessage_handle(chat_id)`** — the 1:1 shape is exactly
  `any;-;<handle>` (splitting on `;-;` yields exactly two parts, the
  first being `any`). Anything else (a group chat's multi-handle
  `chat_id`, or an unexpected prefix) returns `None`. The handle is
  either a `+`-prefixed phone (normalized via `_norm_phone`) or an email
  (lower-cased, validated against a conservative regex).

## Fail-soft contract

Each `resolve_*()` function catches its own failure mode (container not
running, `psql`/docker error, sqlite file missing or locked) and returns
`{}` with a count-only warning logged — it never raises out of this
module. `resolve_all()` merges the three; a `room_id` is expected to be
owned by exactly one bridge, so a collision (kept-first, logged by
`room_id` only) would indicate a bug elsewhere, not routine behavior.

## Privacy

No phone number or email is ever logged or printed by this module. The
`__main__` block prints only per-source resolved/total counts, e.g.:

```
whatsapp: 202/210, gmessages: 216/253, imessage: 4/4, total: 422/467
```

## Running

```bash
python3 agents/enrich/number_resolver.py     # live counts, against the running stack
python3 tests/unit/number_resolver.test.py   # pure logic unit tests, no DB/docker needed
```
