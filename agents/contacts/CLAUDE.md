# agents/contacts/ — durable contacts store + macOS importer

A local, durable address-book store (`contacts_store.py`) plus a headless
importer (`import_macos.py`) that reads macOS Contacts.app and keeps the
store in sync. Python 3.9+ stdlib only (`sqlite3`, `subprocess`, `json`,
`re`) — no pip dependencies.

## What lives here

- `contacts_store.py` (Task 1) — the schema and mutation API. Rows are
  keyed by `(source, network_id)`, never hard-deleted, and every real
  change stamps a monotonic `version` so `shared_since(conn, source,
  after_version)` can hand a consumer (e.g. the uplink) only what changed.
  `upsert_contacts` treats an **empty** `seen` list as "nothing observed"
  — it is a no-op, not "everything was deleted". A **non-empty** `seen`
  list is treated as a *complete* snapshot of that source: anything
  previously present but absent from `seen` gets soft-deleted
  (`deleted=1`), never dropped. `open_store` sets `PRAGMA
  busy_timeout=5000` because `contacts.db` is written by both this
  importer and the uplink process — concurrent writers serialize instead
  of raising "database is locked".
- `import_macos.py` (Task 2) — the macOS Contacts reader/importer.
  - `read_macos_contacts() -> list[dict]` shells out to `osascript -l
    JavaScript` with an **inline** JXA script (never string-interpolated —
    `subprocess.run([...], shell=False)` with a fixed script constant) that
    walks `Application('Contacts').people()` and emits `[{name, phones,
    emails}]` as JSON on stdout. Normalizes phones to E.164
    (`^\+[1-9]\d{6,14}$`) and emails to lowercase + strict-validated;
    anything that fails validation is dropped, and a contact left with
    zero usable handles is dropped entirely (it can never be a
    `start-chat` target). A `_RAW_FOR_TEST` module-level seam lets tests
    inject raw contact dicts and skip the OS call entirely.
  - **Phone country-code inference (fix round 1, I1).** A phone that
    already carries a country code (leading `+`, or a `00` international
    prefix) is normalized as-is — never touched further. A **bare**
    national number (macOS commonly stores US numbers as e.g. `"(555)
    123-4567"`, with no country code at all) is *not* naively prefixed
    with `+` — doing that mints a fabricated, wrong E.164 value that
    happens to pass the regex but will never match the real iMessage
    handle. Instead, `_get_system_region()` reads the Mac's own region out
    of `defaults read -g AppleLocale` (e.g. `"en_US"` -> `"US"`), and
    `_REGION_CALLING_CODES` (a compact, hardcoded dict — not
    libphonenumber-grade, see the bd follow-up below) maps that region to
    a calling code, which gets prepended to the bare digits. This lookup
    happens **once per import run**, not once per number
    (`_normalize_raw_contacts` calls `_get_calling_code()` a single time
    and threads the result through every phone in the batch).
    - **Limitation, by design, not an oversight:** this only resolves a
      bare number that belongs to the Mac's *own* region. A bare-format
      number from a different country (e.g. a UK contact's local-format
      number on a US-region Mac) has no signal to disambiguate and is
      **dropped, never guessed** — counted in `import_once`'s
      `dropped_ambiguous` so it's observable, not silently lost. See bd
      issue `pm_mng-syy` for the real fix (a proper phone-number library
      with per-contact region hints instead of one global region guess).
  - `import_once(db_path) -> dict` reads, flattens to per-identifier
    `seen` rows, and calls `upsert_contacts(conn, "imessage", seen)`.
    Returns `upsert_contacts`'s `added`/`updated`/`soft_deleted` plus
    `dropped_ambiguous` (the count of bare national numbers dropped for
    lack of a resolvable country code — see above).
    **Fail-closed**: any non-zero `osascript` exit, empty/unparseable
    output, or a garbled-but-list payload (e.g. a non-dict contact entry —
    fix round 1, I2) raises internally and `import_once` returns
    `{"error": ...}` without ever calling `upsert_contacts` — a failed
    read must never look like "everyone was deleted".
  - Never logs a handle value or display name; the CLI entry point prints
    only aggregate counts.
- `run-import.sh` / `com.jkali.contacts-import.plist` — launchd wiring.
  One-shot script (`exec python3 import_macos.py`, no loop, no
  backgrounding); the plist's `RunAtLoad` + `StartInterval=3600` handle
  the repetition, matching `agents/uplink/`'s pattern. No env file needed
  — the importer takes no secrets, only local OS Contacts access.

## Security / durability invariants (do not weaken)

- **A failed OS read never mutates the store.** `import_once` always reads
  and validates the *whole* contact list before calling
  `upsert_contacts` once; it never streams partial results into the
  store. If you change `read_macos_contacts` to be more failure-tolerant
  internally (e.g. partial reads), that tolerance still has to resolve to
  either "raise" or "a complete, validated list" before it reaches
  `import_once`.
- **The JXA script is a fixed string constant, never built from
  interpolated input.** `subprocess.run` always passes an argument list
  (`shell=False`); nothing about a contact's data ever becomes part of the
  script being executed.
- **Invalid handles are dropped, not coerced.** A phone that doesn't
  reduce to `^\+[1-9]\d{6,14}$` or an email that fails the strict regex is
  simply excluded from that contact's handles — never written to the
  store as garbage, and never used to fabricate a not-actually-E.164
  network_id. This includes a bare national number whose country code
  can't be resolved: it is dropped (and counted in `dropped_ambiguous`),
  never assumed.
- **A garbled-but-list OS payload fails closed too.** `_normalize_raw_contacts`
  raises `RuntimeError` on any non-dict element in the parsed JSON, so
  `import_once` returns `{"error": ...}` instead of an uncaught traceback
  for that shape — same durability guarantee as a non-zero `osascript`
  exit.
- **No PII in logs.** `import_macos.py`'s `__main__` block and any error
  path print only counts / exception type names, never a name, phone, or
  email.

## TCC (Contacts) permission

The **first** run of `osascript` against `Application('Contacts')`
triggers the standard macOS "`osascript` wants access to your Contacts"
prompt (System Settings → Privacy & Security → Contacts). That prompt
*is* the intended consent surface for this importer — do not try to
suppress, pre-authorize, or script past it. If it was previously denied,
re-enable it for `/usr/bin/osascript` in System Settings and re-run
`python3 agents/contacts/import_macos.py` once interactively before
relying on the launchd job (a background launchd invocation cannot itself
answer the TCC prompt).

## How to run / test

```bash
# unit tests (parser only; the OS call is mocked via _RAW_FOR_TEST):
python3 tests/unit/contacts_store.test.py
python3 tests/unit/import_macos.test.py

# manual smoke (real Contacts.app; not automated — approve the TCC prompt
# on first run):
python3 agents/contacts/import_macos.py
sqlite3 agents/contacts/contacts.db 'select count(*) from contacts'

# install as a launchd job (repeats hourly):
launchctl load agents/contacts/com.jkali.contacts-import.plist
```

## How to change this safely

1. Any change to the E.164 or email validation regex changes what
   `network_id` values can enter the store — re-run
   `tests/unit/import_macos.test.py` and check it against
   `contacts_store.py`'s assumption that `network_id` is a stable,
   dedupable key (Task 1's PRIMARY KEY is `(source, network_id)`).
2. If you add a new field to the JXA output (e.g. a third handle kind),
   update both the inline script's JSON shape and
   `read_macos_contacts`'s flattening loop in the same change, and add a
   `_RAW_FOR_TEST` case for it.
3. Never make `import_once` call `upsert_contacts` more than once per
   run, and never call it before `read_macos_contacts()` has returned a
   complete list — that ordering is what keeps a failed read from being
   mistaken for "the user deleted all their contacts".
