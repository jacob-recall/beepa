# Conversation number enrichment — A1: read-only resolver

`number_resolver.py` maps each 1:1 conversation's Matrix `room_id` to the
counterpart's real E.164 phone number (or, for iMessage, an email
address), by reading the bridge databases directly. This is the first
piece of the "conversation number enrichment" feature — a later task
exposes it over a loopback endpoint so the Hub can auto-merge contacts
that share a number.

**This module is read-only.** Every query below is a `SELECT`; nothing
here ever writes to a bridge database, a bridge config, or a bridge
container.

## Sources

| Source | Where | Query |
|---|---|---|
| WhatsApp | postgres `mautrix_whatsapp` in `matrix-wa-postgres-1` | `portal` (1:1, `room_type='dm'`) joined to `whatsmeow_lid_map` on the LID extracted from `other_user_id` (`lid-<LID>`), giving the real `pn` phone |
| Google Messages | postgres `mautrix_gmessages`, same container | `portal` joined to `ghost`, reading `ghost.metadata->>'phone'` |
| iMessage | sqlite `imessage/state.db` (bridge-local map, **not** macOS `chat.db`) | `map.chat_id`/`map.room_id`; 1:1 chats are shaped `any;-;<handle>` |

Postgres access goes through `docker exec <container> psql -U matrix -d
<db> -tAc "<query>"` — a fixed argv list (no shell string), same
`subprocess.run(..., shell=False)` pattern as `session-connect/connect.py`'s
`api()`. iMessage is read directly via the sqlite3 stdlib, opened
read-only (`file:...?mode=ro`).

## Normalization rules

- **`_norm_phone(raw)`** — general-purpose normalizer to strict E.164
  (`^\+[1-9]\d{6,14}$`). A value that already carries a `+` (or a `00`
  international dial prefix) is validated as given. A **bare** all-digit
  value with no leading `+` is only accepted if it's long enough (≥11
  digits) to already be carrying a country code itself — a shorter bare
  digit string is indistinguishable from a national number missing its
  country code, so it's dropped rather than guessed. This mirrors the
  "never fabricate a country code" philosophy of
  `agents/contacts/import_macos.py`'s `_normalize_phone`, minus the
  system-region guess (there's no region signal available here).

- **`_norm_wa_pn(raw)`** — WhatsApp's `whatsmeow_lid_map.pn` is a
  different case: by whatsmeow's own construction it's *always* digits
  that already include the country code (that's what `pn` means), just
  missing the leading `+`. Applying `_norm_phone`'s ambiguity-guard
  length gate here would wrongly drop legitimate numbers from countries
  with a short (1–2 digit) calling code, which total fewer than 11
  digits. So this just adds `+` and strict-validates the result — no
  length gate, because the source itself isn't ambiguous.

- **gmessages phone filter** — kept only if it's already strict E.164 as
  stored. Deliberately *not* run through either normalizer above: an SMS
  short code or an RCS business id shaped `<slug>@rbm.goog` could
  otherwise survive non-digit stripping and be misread as a phone
  number. A plain regex match against the raw value is the only correct
  filter here.

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
