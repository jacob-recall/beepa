# iMessage Contact-Import Vertical Slice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the whole "import an address book → durably store it → mirror the shared parts up to the manager → let the manager search people and queue drafts → let the teammate approve them one-by-one, and their own account sends" chain end-to-end for **one** network: iMessage.

**Architecture:** Add a teammate-side importer that reads **macOS Contacts** into a new durable, incremental SQLite store (`agents/contacts/contacts.db`, chmod 600) using the same watermark discipline the uplink already uses for messages. Add a **separate** contact-share consent dimension (sharing your address book is a bigger disclosure than sharing a conversation, so it is its own opt-in, default private, byte-parity JS/Python). The **uplink** mirrors only shared contacts up into a per-teammate `com.jkali.contacts` room on the master, exactly-once. The **master** app gains a read-only searchable contacts view and can enqueue **person-targeted** proposals (aimed at a contact identifier, not an existing room). The **teammate** proposal inbox already walks pending drafts; we add one branch so that approving a person-targeted iMessage draft runs the already-approved `start-chat <handle> | <body>` mgmt-room command (which both starts the chat and sends the first message). **The master never sends** — it only writes `com.jkali.proposal` events; the teammate's own daemon does the send.

This slice also lays the **cross-platform identity** groundwork (schema only; the linking UX comes later). One person can own many handles across networks (a WhatsApp number *and* an iMessage number *and* a LinkedIn URN) and many conversations. We do NOT invent a new grouping primitive: the existing `com.jkali.contact_profiles` (`ContactProfile`) is already the "one person, many conversations" anchor and is already mirrored to the master via the uplink's per-room `com.jkali.profile` stamp. We extend that same profile to also own **handles**, so a single `person_id` (= the profile id) is the join key across rooms *and* address-book handles, on both the teammate and the master. See "Identity model" below.

**Tech Stack:** Python 3 (stdlib `sqlite3`, `subprocess`) for the importer + uplink; JXA/`osascript` bridge to macOS Contacts (`Contacts.framework`, TCC-gated — prompts the user for Contacts permission, which is the correct consent surface); native ES modules (no bundler) for `shared/` + `apps/`; Matrix account-data + state events for consent + mirrored contacts.

**Spec:** This plan is self-contained; its design rationale is the "State of contact importing" assessment in the conversation that produced it, plus the existing approved amendment `PLAN-IMSG-STARTCHAT.md` (imsg-startchat-v1), which this plan **reuses unchanged** for the send leg.

## Global Constraints

- **Master stays send-incapable.** No task may add a send path to `apps/master/`. The master only writes `com.jkali.proposal` events into a teammate's proposals room. A contact identifier inside a proposal is inert data; the master never calls `start-chat` or `/send/m.room.message`.
- **Contact-share is a distinct consent dimension, default `private`.** It is NOT the conversation `com.jkali.share_policy`. A teammate sharing conversations does not share their address book, and vice versa. Stored under a new account-data key `com.jkali.contact_share_policy`. Safe default when absent or malformed: `private` (nothing leaves the machine).
- **The identity graph is user-authored and lives in account-data — never derived from the importer.** The person↔handle and person↔room links live in `com.jkali.contact_profiles` (the durable, homeserver-backed, already-mirrored unit), so a rebuild of `contacts.db` from macOS Contacts can NEVER lose a manual grouping. The importer only touches handle *existence* + display name; it must preserve, never clobber, the `person_id` a handle resolves to. A handle and a room each belong to **at most one** person (same invariant `normalizeProfiles()` already enforces for rooms).
- **Consent resolvers stay byte-parity JS↔Python.** Any change to `shared/model/consent.js`'s contact resolver requires the identical change to `agents/uplink/consent.py`, with the same reason strings, and both unit tests re-run. (Same hard rule the conversation resolver already lives under — see `shared/CLAUDE.md`.)
- **Secrets/PII at rest are chmod 600.** `agents/contacts/contacts.db` is `os.chmod(path, 0o600)` on every open, matching `agents/uplink/state.db`.
- **The iMessage send leg reuses imsg-startchat-v1 exactly.** Handle validated against `^\+[1-9]\d{6,14}$` (E.164) OR strict email; rate caps SC-3 (≤3/hr, ≤10/day); mgmt-room-only, `@jkali:localhost`-only; list-argv `shell=False`; never logs handle/message. No task weakens or bypasses SC-1..SC-6.
- **Exactly-once, resume-on-break.** Every mirror-up advances its cursor ONLY after the master confirms the write (HTTP 2xx); on `MasterUnreachable` the cursor is not advanced and the daemon retries with backoff. A broken or partial import rolls back and leaves the prior store intact.
- **textContent-only, no CSP change.** All new UI in `apps/user/` and `apps/master/` builds nodes with `el()`/`sanitizeLine()`/`textContent`; no `innerHTML`; neither app's CSP is loosened (`tests/unit/csp_parity.test.js` must still pass).

---

## File Structure

**New (teammate importer + store):**
- `agents/contacts/contacts_store.py` — the durable SQLite address-book store: schema, `upsert_contacts()`, versioning, soft-delete, incremental reconcile. Pure logic, no macOS calls. One responsibility: persistence + durability.
- `agents/contacts/import_macos.py` — reads macOS Contacts via a JXA `osascript` call, normalizes to `{display_name, handles:[{kind,value}]}`, and drives `contacts_store` transactionally. One responsibility: the macOS→store import pass.
- `agents/contacts/run-import.sh` + `com.jkali.contacts-import.plist` — launchd wrapper so the import runs on login + on an interval (mirrors `session-connect/run-connect.sh` + its plist).
- `agents/contacts/CLAUDE.md` — dir doc: what it imports, where it stores, the durability contract, the TCC/permission note.

**Modified (identity model — cross-platform grouping):**
- `shared/model/contacts.js` — extend `ContactProfile` with `handleIds: [{source, network_id}]`, extend `normalizeProfiles()` to enforce "a handle belongs to at most one profile," and add `linkHandle()` / `unlinkHandle()` / `handleOwner(profiles, source, network_id) -> person_id|null`. `roomIds` and the `share` field are untouched. This is the single source of truth for who-is-who; the UX to drive it comes in a later slice.
- `agents/uplink/uplink.py` (profile read) — when it reads `com.jkali.contact_profiles`, also index `handleIds` so mirrored contacts can be stamped with their `person_id`.

**Modified (consent dimension):**
- `shared/model/consent.js` — add `resolveContactShare(source, contactPolicy)` + `normalizeContactPolicy()` + account-data path helpers for `com.jkali.contact_share_policy`. Nothing else in the file changes.
- `agents/uplink/consent.py` — byte-parity port of the two new functions.
- `apps/user/consent.js` + the `setSharingViewHook`/`setSourceViewHook` wiring — a "Share my iMessage contacts with my manager" switch (global + per-source), visibly separate from the conversation share switches, with copy stating it shares the address book.

**Modified (mirror-up):**
- `agents/uplink/uplink.py` — new `contact_mirror` table + `contact_cursor` meta key; `ensure_contacts_room()`; `mirror_contacts()` called each reconcile; reads `contacts.db` (read-only) + resolves `resolveContactShare`.

**Modified (master search + proposal shape):**
- `apps/master/main.js` — a Contacts view (read `com.jkali.contact` state from each teammate's contacts room, searchable), and a "Propose message" action that calls `submitProposal` with an identifier target. Extend `submitProposal` to carry `target_source`/`target_identifier`/`target_display`.
- `apps/master/index.html` — a contacts list-mode + a propose composer region (textContent nodes; no CSP change).

**Modified (teammate approve-walk send leg):**
- `apps/user/proposals.js` — `parseProposal` accepts the identifier form; `renderProposalDetail` renders "To: <name> (<handle>) — starts a new iMessage chat" with a verbatim confirm; `sendProposal` branches: existing room → `sendConvoMessage` (unchanged); identifier → `sendCmd('imessage', 'start-chat ' + handle + ' | ' + body)` after client-side handle re-validation (SC-7).

**Tests:**
- `tests/unit/contacts_store.test.py` — store durability/versioning/incremental logic.
- `tests/unit/contact_consent.test.js` + `tests/unit/contact_consent_py.test.py` — the new resolver, byte-parity.
- `tests/unit/proposal_identifier.test.js` — person-targeted proposal parse/validate.
- `tests/integration/harness.py` — new scenario `12_contact_share_and_propose`.
- `tests/unit/contacts_profile_handles.test.js` — the `handleIds` invariant + link/unlink/owner helpers.

---

## Identity model (cross-platform contact association)

One `person_id` unifies everything about a person. It IS the existing `ContactProfile.id`, so we reuse the mirrored, consent-bearing profile as the anchor rather than inventing a second grouping.

```
Person  ==  ContactProfile { id (=person_id), displayName, share,
                             roomIds:   [ "!room:hs", ... ],              # conversations (any network) — EXISTS today
                             handleIds: [ {source:"imessage", network_id:"+1..."},   # NEW: address-book handles
                                          {source:"whatsapp", network_id:"1...@s.whatsapp.net"} ] }
```

- **Source of truth = `com.jkali.contact_profiles` account-data** (homeserver-backed, durable, already mirrored). A handle or room belongs to at most one person. This survives a `contacts.db` rebuild because the grouping does not live in `contacts.db`.
- **`contacts.db` = handle inventory + a *derived* `person_id` cache.** Each handle row carries the `person_id` it currently resolves to (via `handleOwner()` over the profiles) or `NULL` when unlinked. The importer refreshes existence/display only and preserves `person_id`; the uplink recomputes `person_id` from account-data on each reconcile so the cache can never diverge authoritatively from the profiles.
- **Mirrored to the master by the same `person_id` on both sides.** Mirror rooms already carry `com.jkali.profile` = `person_id` (existing). New mirrored contacts carry `person_id` + `person_display` in their `com.jkali.contact` content. The master therefore groups **rooms and address-book handles under one person** by joining on `person_id` — with zero new grouping logic, just a wider join. A handle with `person_id: null` shows ungrouped.
- **Future-proofing (schema now, UX later):** because a room's portal state carries the remote party's own network id, a room and an imported handle that share `(source, network_id)` can later be auto-suggested as the same person. This slice only *stores* `network_id` on handles and keeps linking manual (matching the existing "linking is manual only" rule); the auto-suggest is a later slice.
- **Multi-network today, without other importers:** even before Google/WhatsApp importers exist, a person can already span networks — their iMessage *handle* (from this slice) plus their WhatsApp/LinkedIn *conversation rooms* (already mirrored) collapse under one `person_id` the moment they're linked to the same profile.

---

## Task 1: Durable contact store (`contacts_store.py`)

**Files:**
- Create: `agents/contacts/contacts_store.py`
- Test: `tests/unit/contacts_store.test.py`

**Interfaces:**
- Produces:
  - `open_store(path) -> sqlite3.Connection` — creates schema if absent, `chmod 0o600`.
  - `upsert_contacts(conn, source: str, seen: list[dict]) -> dict` where each `seen` item is `{"network_id": str, "kind": "phone"|"email", "display_name": str}`. Returns `{"added": int, "updated": int, "soft_deleted": int}`. Bumps a per-row `version` (monotonic, from `import_meta['version_seq']`) on any real change. Contacts present in the store but absent from a **complete** `seen` list are marked `deleted=1` (soft-delete) — never hard-deleted, never wiped on an empty `seen` (an empty list is treated as "import produced nothing, change nothing" and returns all-zeros).
  - `shared_since(conn, source: str, after_version: int) -> list[dict]` — rows with `version > after_version`, each `{"source","network_id","kind","display_name","person_id","deleted","version"}`, ordered by `version`. This is what the uplink tails.
  - `set_person_id(conn, source: str, network_id: str, person_id: str|None) -> bool` — sets/clears the derived person link for a handle and bumps its `version` if it changed (returns whether it changed). Called by the uplink after resolving `handleOwner()` from account-data. `upsert_contacts` NEVER changes `person_id` (import preserves the grouping).

**Schema (created by `open_store`):**
```sql
CREATE TABLE IF NOT EXISTS contacts (
  source        TEXT    NOT NULL,
  network_id    TEXT    NOT NULL,
  kind          TEXT    NOT NULL,
  display_name  TEXT,
  person_id     TEXT,                       -- derived cache of the account-data grouping; NULL = unlinked
  first_seen_ts INTEGER NOT NULL,
  last_seen_ts  INTEGER NOT NULL,
  deleted       INTEGER NOT NULL DEFAULT 0,
  version       INTEGER NOT NULL,
  PRIMARY KEY (source, network_id)
);
CREATE INDEX IF NOT EXISTS idx_contacts_person ON contacts(person_id);
CREATE TABLE IF NOT EXISTS import_meta (key TEXT PRIMARY KEY, value TEXT);
```
`person_id` is a *cache*: the authoritative person↔handle link lives in `com.jkali.contact_profiles`. `set_person_id` is the only writer of this column; `upsert_contacts` leaves it untouched so a re-import can never drop a grouping.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/contacts_store.test.py
import os, tempfile, sqlite3, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "contacts"))
import contacts_store as cs

def _fresh():
    d = tempfile.mkdtemp()
    return cs.open_store(os.path.join(d, "contacts.db"))

def test_add_then_incremental_then_soft_delete_and_never_wipe():
    conn = _fresh()
    # first import: two contacts
    r = cs.upsert_contacts(conn, "imessage", [
        {"network_id": "+15551234567", "kind": "phone", "display_name": "Alice"},
        {"network_id": "bob@example.com", "kind": "email", "display_name": "Bob"},
    ])
    assert r == {"added": 2, "updated": 0, "soft_deleted": 0}
    v_after_first = max(row["version"] for row in cs.shared_since(conn, "imessage", 0))

    # re-import identical set: no version churn, nothing "shared" as new
    r = cs.upsert_contacts(conn, "imessage", [
        {"network_id": "+15551234567", "kind": "phone", "display_name": "Alice"},
        {"network_id": "bob@example.com", "kind": "email", "display_name": "Bob"},
    ])
    assert r == {"added": 0, "updated": 0, "soft_deleted": 0}
    assert cs.shared_since(conn, "imessage", v_after_first) == []

    # rename Alice: one update, one new version > watermark
    r = cs.upsert_contacts(conn, "imessage", [
        {"network_id": "+15551234567", "kind": "phone", "display_name": "Alice Smith"},
        {"network_id": "bob@example.com", "kind": "email", "display_name": "Bob"},
    ])
    assert r == {"added": 0, "updated": 1, "soft_deleted": 0}
    changed = cs.shared_since(conn, "imessage", v_after_first)
    assert [c["network_id"] for c in changed] == ["+15551234567"]

    # EMPTY import must change nothing (a failed/partial fetch must never wipe)
    v_before_empty = max(row["version"] for row in cs.shared_since(conn, "imessage", 0))
    r = cs.upsert_contacts(conn, "imessage", [])
    assert r == {"added": 0, "updated": 0, "soft_deleted": 0}
    assert max(row["version"] for row in cs.shared_since(conn, "imessage", 0)) == v_before_empty

    # Bob genuinely gone from a COMPLETE import -> soft-deleted, not removed
    r = cs.upsert_contacts(conn, "imessage", [
        {"network_id": "+15551234567", "kind": "phone", "display_name": "Alice Smith"},
    ])
    assert r == {"added": 0, "updated": 0, "soft_deleted": 1}
    bob = [c for c in cs.shared_since(conn, "imessage", 0) if c["network_id"] == "bob@example.com"][0]
    assert bob["deleted"] == 1

def test_store_file_is_0600():
    conn = _fresh()
    path = conn.execute("PRAGMA database_list").fetchall()[0][2]
    assert (os.stat(path).st_mode & 0o777) == 0o600

def test_reimport_preserves_person_id_grouping():
    conn = _fresh()
    cs.upsert_contacts(conn, "imessage", [
        {"network_id": "+15551234567", "kind": "phone", "display_name": "Alice"}])
    # the uplink links this handle to a person (from account-data)
    assert cs.set_person_id(conn, "imessage", "+15551234567", "cp_alice") is True
    v_after_link = max(r["version"] for r in cs.shared_since(conn, "imessage", 0))
    # a later macOS re-import (even with a renamed display) must NOT drop the link
    cs.upsert_contacts(conn, "imessage", [
        {"network_id": "+15551234567", "kind": "phone", "display_name": "Alice Smith"}])
    row = cs.shared_since(conn, "imessage", 0)[0]
    assert row["person_id"] == "cp_alice", "re-import must preserve the grouping"
    # setting the same person_id again is a no-op (no version churn)
    assert cs.set_person_id(conn, "imessage", "+15551234567", "cp_alice") is False

if __name__ == "__main__":
    test_add_then_incremental_then_soft_delete_and_never_wipe()
    test_store_file_is_0600()
    test_reimport_preserves_person_id_grouping()
    print("ok")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 tests/unit/contacts_store.test.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'contacts_store'`.

- [ ] **Step 3: Implement `contacts_store.py`**

Key rules to honor: `upsert_contacts` with an empty `seen` returns all-zeros and touches nothing; the "complete import" soft-delete only runs when `seen` is non-empty; `version` comes from a single monotonic `import_meta['version_seq']` counter incremented per changed row; all mutations for one call happen in ONE transaction so a crash mid-import rolls back. `display_name`/identity comparison decides "updated". `upsert_contacts` NEVER writes `person_id`; `set_person_id` is its only writer and bumps `version` only on a real change. `open_store` runs `os.chmod(path, 0o600)` and creates the `idx_contacts_person` index.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 tests/unit/contacts_store.test.py`
Expected: `ok`.

- [ ] **Step 5: Commit**

```bash
git add agents/contacts/contacts_store.py tests/unit/contacts_store.test.py
git commit -m "feat(contacts): durable incremental address-book store (never wipes on empty import)"
```

---

## Task 2: macOS Contacts importer (`import_macos.py`)

**Files:**
- Create: `agents/contacts/import_macos.py`, `agents/contacts/run-import.sh`, `agents/contacts/com.jkali.contacts-import.plist`, `agents/contacts/CLAUDE.md`
- Consumes: `contacts_store.open_store`, `contacts_store.upsert_contacts` (Task 1).

**Interfaces:**
- Produces:
  - `read_macos_contacts() -> list[dict]` — returns `[{"display_name": str, "handles": [{"kind": "phone"|"email", "value": str}]}]`. Phones normalized to E.164 (`+` + digits; reject anything not matching `^\+[1-9]\d{6,14}$`); emails lowercased + strict-validated. Handles that fail validation are dropped (they can't be a `start-chat` target).
  - `import_once(db_path) -> dict` — reads Contacts, flattens to per-identifier `seen` rows, calls `upsert_contacts(..., source="imessage", ...)`, returns its counts. On a non-zero `osascript` exit or empty/garbled output, returns `{"error": "..."}` and does NOT call `upsert_contacts` with a partial list (durability: a failed read never mutates the store).

**macOS read mechanism:** shell out to `osascript -l JavaScript` with an inline JXA script that uses `Application('Contacts')` / the Contacts bridge to emit JSON `[{name, phones:[...], emails:[...]}]` to stdout. First run triggers the OS Contacts-permission prompt (the correct consent surface). `subprocess.run([...], shell=False)`; never interpolate anything into the script string.

- [ ] **Step 1: Write the failing test (parser only — the OS read is mocked)**

```python
# add to tests/unit/contacts_store.test.py OR a new tests/unit/import_macos.test.py
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "contacts"))
import import_macos as im

def test_normalize_drops_invalid_and_keeps_e164_and_email(monkeypatch=None):
    raw = [
        {"name": "Alice", "phones": ["+1 (555) 123-4567", "notaphone"], "emails": ["Alice@Example.COM"]},
        {"name": "NoHandles", "phones": [], "emails": []},
    ]
    im._RAW_FOR_TEST = raw            # test seam: read_macos_contacts uses this if set
    out = im.read_macos_contacts()
    alice = [c for c in out if c["display_name"] == "Alice"][0]
    kinds = {(h["kind"], h["value"]) for h in alice["handles"]}
    assert ("phone", "+15551234567") in kinds
    assert ("email", "alice@example.com") in kinds
    assert all(h["value"] != "notaphone" for h in alice["handles"])
    assert all(c["display_name"] != "NoHandles" for c in out)  # no usable handle -> dropped

if __name__ == "__main__":
    test_normalize_drops_invalid_and_keeps_e164_and_email()
    print("ok import_macos")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 tests/unit/import_macos.test.py`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `import_macos.py`** (with a `_RAW_FOR_TEST` seam so `read_macos_contacts` can be unit-tested without the OS), plus `run-import.sh` (activates a venv-free `python3 import_macos.py`) and the launchd plist (RunAtLoad + StartInterval, e.g. 3600s), and `CLAUDE.md` documenting the TCC permission requirement and the durability contract.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 tests/unit/import_macos.test.py` → `ok import_macos`.

- [ ] **Step 5: Manual smoke (documented, not automated)**

Run: `python3 agents/contacts/import_macos.py` once, approve the Contacts prompt, confirm `agents/contacts/contacts.db` now has rows (`sqlite3 agents/contacts/contacts.db 'select count(*) from contacts'`). Record the count; no handle/name printed to logs.

- [ ] **Step 6: Commit**

```bash
git add agents/contacts/import_macos.py agents/contacts/run-import.sh agents/contacts/com.jkali.contacts-import.plist agents/contacts/CLAUDE.md tests/unit/import_macos.test.py
git commit -m "feat(contacts): macOS Contacts importer (E.164/email normalize, fail-closed on read error)"
```

---

## Task 3: Contact-share consent — JS side

**Files:**
- Modify: `shared/model/consent.js`
- Test: `tests/unit/contact_consent.test.js`

**Interfaces:**
- Produces:
  - `normalizeContactPolicy(raw) -> {global: 'share-all'|'private', sources: {<source>: 'share-all'|'private-all'|'inherit'}}` — unknown values collapse to the safe default (`global:'private'`, source omitted → inherit).
  - `resolveContactShare(source, policy) -> {shared: boolean, reason: string}` — precedence: per-source `share-all`→shared `'all <source> contacts'`; per-source `private-all`→not shared `'private'`; else global `share-all`→shared `'all contacts'`; else `'private'`. Default (absent policy) not shared.
  - `contactSharePolicyPath(userId) -> string` — `/_matrix/client/v3/user/<uid>/account_data/com.jkali.contact_share_policy` (userId already validated by caller).

- [ ] **Step 1: Write the failing test**

```javascript
// tests/unit/contact_consent.test.js
import { resolveContactShare, normalizeContactPolicy } from '../../shared/model/consent.js';
function eq(a, b, m){ if (JSON.stringify(a)!==JSON.stringify(b)) throw new Error(m+': '+JSON.stringify(a)); }

// default = private
eq(resolveContactShare('imessage', normalizeContactPolicy(undefined)), {shared:false, reason:'private'}, 'default');
// global share-all
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'share-all'})), {shared:true, reason:'all contacts'}, 'global');
// per-source overrides global
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'share-all', sources:{imessage:'private-all'}})), {shared:false, reason:'private'}, 'src-private wins');
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'private', sources:{imessage:'share-all'}})), {shared:true, reason:'all imessage contacts'}, 'src-share wins');
// garbage collapses to safe default
eq(resolveContactShare('imessage', normalizeContactPolicy({global:'yolo', sources:{imessage:'maybe'}})), {shared:false, reason:'private'}, 'garbage safe');
console.log('ok contact_consent');
```

- [ ] **Step 2: Run it to verify it fails**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/contact_consent.test.js`
Expected: FAIL — `resolveContactShare is not a function`.

- [ ] **Step 3: Implement the three functions in `shared/model/consent.js`** (append; do not touch the existing conversation resolver).

- [ ] **Step 4: Run the test to verify it passes** → `ok contact_consent`.

- [ ] **Step 5: Commit**

```bash
git add shared/model/consent.js tests/unit/contact_consent.test.js
git commit -m "feat(consent): contact-share resolver (separate dimension, default private)"
```

---

## Task 4: Contact-share consent — Python parity

**Files:**
- Modify: `agents/uplink/consent.py`
- Test: `tests/unit/contact_consent_py.test.py`

**Interfaces:**
- Produces `normalize_contact_policy(raw)` and `resolve_contact_share(source, policy)` — identical precedence and **identical reason strings** to Task 3 (`'all contacts'`, `'all <source> contacts'`, `'private'`).

- [ ] **Step 1: Write the failing test** (mirror Task 3's cases exactly, asserting the same `{shared, reason}` outputs).

```python
# tests/unit/contact_consent_py.test.py
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "agents", "uplink"))
import consent
def eq(a,b,m):
    assert a==b, m+": "+repr(a)
eq(consent.resolve_contact_share("imessage", consent.normalize_contact_policy(None)), {"shared":False,"reason":"private"}, "default")
eq(consent.resolve_contact_share("imessage", consent.normalize_contact_policy({"global":"share-all"})), {"shared":True,"reason":"all contacts"}, "global")
eq(consent.resolve_contact_share("imessage", consent.normalize_contact_policy({"global":"share-all","sources":{"imessage":"private-all"}})), {"shared":False,"reason":"private"}, "src-private")
eq(consent.resolve_contact_share("imessage", consent.normalize_contact_policy({"global":"private","sources":{"imessage":"share-all"}})), {"shared":True,"reason":"all imessage contacts"}, "src-share")
eq(consent.resolve_contact_share("imessage", consent.normalize_contact_policy({"global":"yolo"})), {"shared":False,"reason":"private"}, "garbage")
print("ok contact_consent_py")
```

- [ ] **Step 2: Run it to verify it fails** → `python3 tests/unit/contact_consent_py.test.py` FAIL (attr missing).
- [ ] **Step 3: Implement `normalize_contact_policy` + `resolve_contact_share` in `consent.py`** — byte-parity with the JS.
- [ ] **Step 4: Run BOTH parity tests to verify they pass**

```bash
python3 tests/unit/contact_consent_py.test.py
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/contact_consent.test.js
```
Expected: both print `ok`.

- [ ] **Step 5: Commit**

```bash
git add agents/uplink/consent.py tests/unit/contact_consent_py.test.py
git commit -m "feat(consent): python parity for contact-share resolver"
```

---

## Task 5: Unified person model — handles under a profile

**Files:**
- Modify: `shared/model/contacts.js`
- Test: `tests/unit/contacts_profile_handles.test.js`

**Interfaces:**
- Produces (added to the existing module, existing exports unchanged):
  - `normalizeProfiles(data)` now also reads `handleIds: [{source, network_id}]` per profile, enforcing "a handle belongs to at most one profile" (first profile wins, same as rooms) and dropping malformed entries (`source`/`network_id` must be non-empty strings).
  - `linkHandle(profiles, profileId, source, network_id) -> profiles` and `unlinkHandle(profiles, source, network_id) -> profiles` — pure, return a new normalized structure; linking a handle first removes it from any other profile (upholds the invariant).
  - `handleOwner(profiles, source, network_id) -> string|null` — the `person_id` currently owning a handle, or null.

**Why this task:** this is the cross-platform identity anchor. `person_id` = `ContactProfile.id`. It stays the authoritative, homeserver-backed, already-mirrored grouping so a `contacts.db` rebuild can't lose it. The linking UX is a later slice; this task only makes the data model and its invariants real and tested.

- [ ] **Step 1: Write the failing test**

```javascript
// tests/unit/contacts_profile_handles.test.js
import { normalizeProfiles, linkHandle, unlinkHandle, handleOwner } from '../../shared/model/contacts.js';
function eq(a,b,m){ if(JSON.stringify(a)!==JSON.stringify(b)) throw new Error(m+': '+JSON.stringify(a)); }

// a handle can belong to at most one profile (first wins on normalize)
let p = normalizeProfiles({ profiles: [
  { id:'cp_a', displayName:'A', roomIds:[], handleIds:[{source:'imessage', network_id:'+1555'}] },
  { id:'cp_b', displayName:'B', roomIds:[], handleIds:[{source:'imessage', network_id:'+1555'}] },
]});
eq(handleOwner(p.profiles,'imessage','+1555'), 'cp_a', 'first profile wins');

// link moves the handle (removes from the old owner, upholds invariant)
p = linkHandle(p.profiles, 'cp_b', 'imessage', '+1555');
eq(handleOwner(p.profiles,'imessage','+1555'), 'cp_b', 'relink moves handle');
const aStill = p.profiles.find(x=>x.id==='cp_a').handleIds.length;
eq(aStill, 0, 'old owner lost the handle');

// unlink clears ownership
p = unlinkHandle(p.profiles, 'imessage', '+1555');
eq(handleOwner(p.profiles,'imessage','+1555'), null, 'unlinked');

// malformed handle entries are dropped; rooms + share untouched
const n = normalizeProfiles({ profiles: [
  { id:'cp_c', displayName:'C', roomIds:['!r:h'], share:'share',
    handleIds:[{source:'', network_id:'x'}, {source:'whatsapp'}, {source:'whatsapp', network_id:'1@w'}] },
]});
eq(n.profiles[0].handleIds, [{source:'whatsapp', network_id:'1@w'}], 'malformed dropped');
eq(n.profiles[0].share, 'share', 'share untouched');
console.log('ok contacts_profile_handles');
```

- [ ] **Step 2: Run it to verify it fails**

Run: `docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/contacts_profile_handles.test.js`
Expected: FAIL — `handleOwner is not a function` (and `handleIds` not yet normalized).

- [ ] **Step 3: Extend `shared/model/contacts.js`** — add `handleIds` to `normalizeProfiles` (dedup + at-most-one-profile via a `claimedHandles` Set keyed by `source|network_id`, mirroring `claimedRooms`), and implement `linkHandle`/`unlinkHandle`/`handleOwner`. Do not change `roomIds`, `share`, or the existing exports' behavior.

- [ ] **Step 4: Run the new test AND the existing contacts + consent tests to verify nothing regressed**

```bash
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/contacts_profile_handles.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/consent.test.js
```
Expected: both print `ok`.

- [ ] **Step 5: Commit**

```bash
git add shared/model/contacts.js tests/unit/contacts_profile_handles.test.js
git commit -m "feat(contacts): profiles own cross-platform handles (person_id = profile id; handle in <=1 profile)"
```

---

## Task 6: Mirror shared contacts up (uplink)

**Files:**
- Modify: `agents/uplink/uplink.py`
- Test: extend `tests/unit/uplink_reconcile.test.py` with a contact-cursor case (pure logic only — no live homeserver).

**Interfaces:**
- Consumes: `contacts_store.shared_since` + `contacts_store.set_person_id` (Task 1, read-write open of `contacts.db`), `consent.resolve_contact_share` (Task 4), and a Python `handle_owner(profiles, source, network_id)` mirroring `handleOwner` (Task 5 — add the same small helper to `agents/uplink/uplink.py`'s profile-read, byte-parity with the JS grouping rule).
- Produces (internal): `contact_mirror(source TEXT, network_id TEXT, mirrored_version INTEGER, master_state_key TEXT, PRIMARY KEY(source, network_id))`; `meta['contact_cursor']`; `meta['master_contacts_room']`; `ensure_contacts_room()`; `mirror_contacts()`.

**Behavior:** each reconcile pass, after the existing conversation reconcile:
1. **Resolve groupings first.** Read `com.jkali.contact_profiles`, index its `handleIds`, and for every handle call `set_person_id(source, network_id, handleOwner(profiles, source, network_id))` in `contacts.db`. This recomputes the derived `person_id` cache from the authoritative account-data (a re-link on the teammate's side thus flows to the master as a normal version bump), and `set_person_id` only bumps `version` when the link actually changed.
2. **Mirror shared rows.** Read `com.jkali.contact_share_policy`; for each `shared_since(cursor)` row whose `resolve_contact_share(source, policy).shared` is true, upsert a `com.jkali.contact` **state event** into the master contacts room (`state_key = sha1(source + '|' + network_id)`, content `{source, network_id, kind, display_name, person_id, person_display, deleted}` — `person_id`/`person_display` let the master group this handle with the person's mirror rooms, which already carry the same `person_id` via `com.jkali.profile`; both are null/omitted when unlinked), then advance `contact_cursor` to that row's version **only after HTTP 2xx**. A soft-deleted (`deleted=1`) shared row pushes a tombstone state event (`deleted:true`) so the master drops it. A row that resolves NOT shared is skipped and never leaves the machine. On `MasterUnreachable`, cursor unchanged; retried next pass.

The contacts room is created once (`ensure_contacts_room`), marked `com.jkali.contacts` state, PL pinned so the manager can only READ (no `com.jkali.contact` write power for `@manager`). Note: a person's `displayName` is a grouping label, not extra PII beyond the handle already shared under consent; `person_id` is an opaque profile id.

- [ ] **Step 1: Write the failing test** — a pure reconcile-cursor unit: given store rows at versions [1..5] and a cursor at 2, with policy sharing imessage, assert the planner yields exactly the rows with version >3..5 in order and that a not-shared policy yields none. (Mirror the shape of the existing `select_new_events` test in `reconcile.py`.)

- [ ] **Step 2: Run it to verify it fails.**
- [ ] **Step 3: Implement `contact_mirror` table, `ensure_contacts_room`, `mirror_contacts`, and the cursor-advance-only-on-2xx logic** in `uplink.py`; factor the pure "which versions to push" selection into `reconcile.py` so it is unit-testable.
- [ ] **Step 4: Run the reconcile unit test + the existing uplink tests to verify green.**

```bash
python3 tests/unit/uplink_reconcile.test.py
```

- [ ] **Step 5: Commit**

```bash
git add agents/uplink/uplink.py agents/uplink/reconcile.py tests/unit/uplink_reconcile.test.py
git commit -m "feat(uplink): mirror shared contacts up exactly-once (cursor advances only on master 2xx)"
```

---

## Task 7: Master contacts view + person-targeted proposal write

**Files:**
- Modify: `apps/master/main.js`, `apps/master/index.html`

**Interfaces:**
- Consumes: `com.jkali.contact` state events in each teammate's `com.jkali.contacts`-marked room (discovered the same way `MS.proposalsByUser` discovers proposals rooms).
- Produces: a Contacts list-mode (`renderContacts()`) that **groups by `person_id`** — a handle's `com.jkali.contact` events and that person's mirror rooms (already tagged `com.jkali.profile` = same `person_id`) render under one header, so "Alice — iMessage handle + WhatsApp conversation" shows as one person; `person_id: null` handles list ungrouped. A search box over `display_name`/`person_display`/`network_id`, and a "Propose message" composer whose submit calls the extended `submitProposal({ target_source, target_identifier, target_display, body })`. `submitProposal` gains an identifier branch: it writes a `com.jkali.proposal` with `{ target_source, target_identifier, target_display, body, created_by, origin_ts }` (NO `target_room`) into that teammate's proposals room. Existing room-targeted proposals are unchanged.

**Security:** the identifier is validated by SHAPE before write (`^\+[1-9]\d{6,14}$` OR strict email); the master still writes only `com.jkali.proposal` (never a message, never a start-chat). Queuing = submitting several; the teammate inbox already lists them all.

- [ ] **Step 1: Write the failing test** — `tests/unit/proposal_identifier.test.js`: a pure `buildIdentifierProposalContent({source, identifier, display, body})` helper (extracted from `submitProposal`) returns the exact content object, rejects a bad identifier by returning `null`, and never includes `target_room`.
- [ ] **Step 2: Run it to verify it fails.**
- [ ] **Step 3: Implement the contacts view + `buildIdentifierProposalContent` + the composer wiring in `main.js`/`index.html`** (textContent nodes; no CSP change; reuse `ROOM_SHAPE_RE`-style validation for the identifier).
- [ ] **Step 4: Run the test + `node --check` + CSP parity.**

```bash
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/proposal_identifier.test.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node --check apps/master/main.js
docker run --rm -v "$(pwd)":/w -w /w node:20-alpine node tests/unit/csp_parity.test.js
```

- [ ] **Step 5: Commit**

```bash
git add apps/master/main.js apps/master/index.html tests/unit/proposal_identifier.test.js
git commit -m "feat(master): searchable contacts view + person-targeted proposal (master still send-incapable)"
```

---

## Task 8: Teammate approve-walk — iMessage start-chat send leg

**Files:**
- Modify: `apps/user/proposals.js`

**Interfaces:**
- Consumes: the extended proposal content (Task 7); the existing verified iMessage mgmt-room path via `sendCmd('imessage', ...)` and the SC-7 client-side handle regexes.
- Produces: `parseProposal` returns `{kind:'room', targetRoom, body,...}` OR `{kind:'identifier', targetSource, targetIdentifier, targetDisplay, body,...}`. `renderProposalDetail` shows, for identifier drafts, "To: <display> (<handle>) — starts a NEW iMessage chat" and a **verbatim** confirm before send. `sendProposal` branches: room → `sendConvoMessage(targetRoom, text)` (unchanged); identifier → after re-validating the handle client-side (E.164/email), `sendCmd('imessage', 'start-chat ' + handle + ' | ' + text)` through the C-1 verified mgmt room, then `markHandled`.

**Security:** the identifier send leg is the already-approved imsg-startchat-v1 capability — the daemon re-validates the handle and enforces SC-3 rate caps authoritatively; the client regex is only a UX pre-check. No auto-send; a human presses Send per draft (that IS the "yes, yes, yes" walk). Never logs handle/body.

- [ ] **Step 1: Write the failing test** — extend `tests/unit/proposal_identifier.test.js` (or a sibling) to unit-test the extracted pure `parseProposal` over both content shapes and to assert an identifier draft with a bad handle is refused (returns null / flagged), imported as a plain node function.
- [ ] **Step 2: Run it to verify it fails.**
- [ ] **Step 3: Implement the `parseProposal` two-shape return + the identifier render/confirm/send branch** in `proposals.js`.
- [ ] **Step 4: Run the test + `node --check apps/user/proposals.js`.**
- [ ] **Step 5: Commit**

```bash
git add apps/user/proposals.js tests/unit/proposal_identifier.test.js
git commit -m "feat(user): approve a person-targeted draft -> guarded iMessage start-chat (teammate sends, not master)"
```

---

## Task 9: End-to-end integration scenario + security review

**Files:**
- Modify: `tests/integration/harness.py` — add scenario `12_contact_share_and_propose`.

**Scenario (drives the real local+master homeserver pair the harness already stands up):**
1. Seed `contacts.db` with one contact whose handle is the tester's OWN iMessage self-number (so the eventual start-chat is self-directed, per imsg-startchat SC.A acceptance — no third party is messaged).
2. Link that iMessage handle into a `com.jkali.contact_profiles` profile that ALSO owns an existing mirrored room (reuse scenario 11's cross-platform profile). Set `com.jkali.contact_share_policy` to share imessage contacts; run one uplink reconcile; assert (a) a `com.jkali.contact` state event for that handle appears in the master contacts room carrying the SAME `person_id` as that profile's mirror room's `com.jkali.profile` stamp (cross-platform grouping is visible on the master), and (b) a NON-shared contact does NOT appear.
3. As the master, submit a person-targeted proposal (identifier = the self-number, body = `pmmng-test-<nonce>`); assert exactly one `com.jkali.proposal` with `target_identifier` and NO `target_room` lands in the teammate proposals room.
4. As the teammate, approve it; assert `start-chat` fired to self (the nonce reaches iMessage) and that the master never issued any `/send/m.room.message` or `start-chat` itself.

- [ ] **Step 1: Write scenario 12** following the existing scenario pattern (`5_share_all_standing_policy`, `10_proposal_down`, `11_profile_span_platforms`).
- [ ] **Step 2: Run it** (per `tests/CLAUDE.md` harness invocation); expected: all four assertions pass.
- [ ] **Step 3: Security review checkpoint (REQUIRED, before any merge).** Dispatch `pilotfish:security-reviewer` over the full diff with this focus: (a) contact PII never leaves the machine unless `resolve_contact_share` is true and the mirror advances only on 2xx; (b) `contacts.db` is 0600 and a failed import never wipes/leaks; (c) the master has no send path and cannot issue `start-chat` — the identifier is inert data it only writes into a proposal; (d) the teammate send leg cannot be driven except by an explicit human Send, re-validates the handle, and inherits SC-1..SC-6 (mgmt-room only, rate caps, no shell); (e) JS↔Python contact-consent parity holds; (f) no CSP/Trusted-Types regression in either app. Address findings before merge (P0/P1 block).
- [ ] **Step 4: Commit**

```bash
git add tests/integration/harness.py
git commit -m "test(integration): scenario 12 — share a contact, propose to it, teammate approves (self-directed)"
```

---

## Self-Review (author checklist — completed at write time)

- **Spec coverage:** import (T1–T2), durable/continuous store surviving disconnect (T1 empty-import-never-wipes + T6 cursor-advances-only-on-2xx), cross-platform identity model — one `person_id` over handles + rooms, source-of-truth in account-data so a rebuild can't lose a grouping (T1 person_id cache + preservation test, T5 handleIds+invariants, T6 resolve+mirror with person_id, T7 master groups by person_id, T9 assertion 2), contact-share consent as its own dimension (T3–T4), mirror-up (T6), master search (T7), queue = many proposals in the existing inbox (T7 write + T8 walk), person-targeted "message a specific person" (T7–T8), teammate-sends-not-master (Global Constraints + T7/T8 + T9 assertion 4). All requirements map to a task.
- **Mirrored across user and master:** the identity graph lives in `com.jkali.contact_profiles` (already mirrored) and is projected onto the master as `person_id` on both `com.jkali.profile` (rooms, existing) and `com.jkali.contact` (handles, T6) — so grouping is identical on both sides by construction.
- **Deferred beyond this slice (explicitly out of scope):** Google Contacts (gmessages) and WhatsApp/LinkedIn importers — replicate T1's store shape + T6's mirror once this slice is proven; the contact-linking UX (this slice ships the schema + invariants + mirror for cross-platform grouping, but the user-facing "link these two" UI is a later slice); a dedicated batch-compose UI on the master (this slice relies on submitting proposals individually into the existing inbox); throttle/anti-ban UX beyond the SC-3 daemon caps; auto-suggesting a room and a handle that share `(source, network_id)`.
- **Type consistency:** `upsert_contacts`/`shared_since`/`set_person_id`/`resolveContactShare`/`resolve_contact_share`/`handleOwner`/`linkHandle`/`unlinkHandle`/`buildIdentifierProposalContent`/`parseProposal` names and shapes are used identically across the tasks that produce and consume them.
- **Placeholder scan:** no TBD/"handle errors"/"similar to Task N" — each task carries its own concrete test and rules.
