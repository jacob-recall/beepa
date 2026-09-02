# Plan: per-contact share overrides for the contacts mirror

**ID:** per-contact-share
**Date:** 2026-09-02
**Status:** r4 — plan-verifier rounds complete (closing round's single
blocker — over-cap wedge/recovery — was a mechanical completion applied per
review-cap policy); security review integrated with dispositions.
APPROVED FOR IMPLEMENTATION (user feature approval 2026-09-02).

## Outcome

A teammate can share or withhold **individual contacts**, layered on the
existing per-source contact policy. Today the contacts mirror is per-source
only (`resolve_contact_share(source, policy)`): enabling iMessage shares
every iMessage-source contact (e.g. Datadog Alerting alongside Jacob K).
After this: a per-contact override wins over source/global policy;
most-specific-wins, absent = inherit from source/global (the contact
dimension deliberately KEEPS its standing policies — the explicit-only
decision applied to conversations, not contacts).

## Design

### C1. Model (`shared/model/consent.js` + byte-parity `agents/uplink/consent.py`)

- New account-data type `com.jkali.contact_overrides` (global user
  account-data): `{ "overrides": { "<source>|<network_id>": "share" |
  "private" } }`.
- **Key spec (F5/F6):** a valid key contains at least one `|`; the segment
  before the FIRST `|` must match `_SOURCE_KEY_RE` / `SOURCE_KEY_RE`; the
  remainder (which may itself contain `|` — the importer's email charset
  admits it) must be non-empty and is taken verbatim. Any code reversing a
  key splits ONCE on the first `|`, never `split('|')`. JS uses the
  `plainObject` + `hasOwnProperty` discipline of `sourceRule` and builds
  output on a null-prototype object; Python parity via `_plain`. Entry cap
  1024: every WRITE path refuses before it would cross the cap, with a
  visible refusal naming the cap (same visible-refusal pattern as F7);
  a STORED map over the cap reads as read-failure (see C2) except that the
  destructive-only writes — single-key removal and clear-all — remain
  permitted in that state so recovery stays in-app (pushes stay skipped
  until the map is back under cap). Malformed KEYS are dropped (inherit). A present-but-non-dict `overrides` FIELD is a
  READ-FAILURE, not `{}` — so a partially corrupt event cannot silently
  drop `'private'` denies (F5).
- `resolve_contact_share(source, policy, override=None)` gains the optional
  override argument: `'share'` → shared (reason "this contact"),
  `'private'` → not shared (reason "this contact private"), else fall through
  to today's source/global precedence unchanged. New
  `normalize_contact_overrides(raw)`. Byte-parity in both languages;
  contact-consent unit tests extended case-for-case in both.

### C2. Uplink (`agents/uplink/uplink.py` + `agents/uplink/reconcile.py`)

- `read_contact_overrides()`: 404/absent → `{}` (no overrides). ANY other
  error (HTTP non-404, URLError/timeout, malformed body) → the pass SKIPS the
  push leg entirely while tombstones still run — mirroring the existing
  `read_contact_profiles`/`mirror_contacts` fail-closed pattern (a transient
  read failure must never make a `'private'`-overridden contact fall back to
  a `share-all` source and push its PII). Acceptance: a unit test where the
  overrides read raises non-404 yields pushed==0 that pass (tombstones still
  applied); a 404 yields today's per-source behavior.
- `plan_contact_mirror(rows, mirrored, policy, sources, overrides)` —
  overrides is a REQUIRED POSITIONAL parameter (an unconverted call site is
  a TypeError, never a silent widening — F4). The override consult sits
  strictly AFTER the known-source allowlist and `deleted` checks (an
  override can never resurrect an unknown source or soft-deleted row), and
  the override is a BOOLEAN GATE ONLY: every field of pushed content comes
  from the store row, never from the override key (F4 invariant, pinned by
  test). `'share'` in a private source pushes just that row; revocation
  tombstones via the unchanged live-shared diff.
- **Tombstone-leg cache (P3):** the last successfully-read overrides map is
  cached in state.db and used FOR THE TOMBSTONE LEG ONLY during read
  failures (pushes stay skipped), so a transient error neither leaks nor
  flaps tombstone/re-push churn.
- **In-pass window (F10):** the push leg re-reads the overrides map every 50
  pushes and drops rows whose override flipped to `'private'` mid-pass; the
  residual sub-chunk window is documented as accepted.
- **Logging (F9):** the existing no-PII-in-logs invariant explicitly covers
  override keys (they ARE phone numbers/emails) in the uplink, the UI, and
  the helper's `_diag`. Counts only.
- **Alignment (P3):** `read_contact_policy` gains the same 404-vs-other-error
  split while this code is open (currently a 500 collapses to global-private
  and storms tombstones).

### C3. apps/user contacts UI — control unit is PER-HANDLE

- The override unit is a HANDLE (`source|network_id`), surfaced two ways:
  1. **Imported contacts list (covers store-only rows like Datadog):** the
     loopback session-connect helper gains **`POST /contacts/list`** behind
     the existing `_authorized()` gate (NOT a GET — the helper's GETs are
     deliberately ungated liveness-only, and adding GET would also widen the
     shared CORS methods header; F1). Absent `Origin` remains a refusal;
     a `Host` allowlist (`127.0.0.1:<port>`/`localhost:<port>`) is added as
     anti-rebinding defense-in-depth. Parameterless (no query — `_diag`
     logs `self.path`); rows filtered to known sources; response capped
     (2000 rows). The **local-process residual** (any process speaking the
     loopback protocol can read the imported address book — a broader read
     than `/enrich/numbers`) is ACCEPTED and documented in
     `session-connect/CLAUDE.md` beside the existing residuals block.
     The JS validates every row (shape, source ∈ known ids, handle shape)
     before building override keys, mirroring enrich.js's validation, and
     ALWAYS renders the network_id beside the display name (homoglyph/dupe
     names — P3). The panel's per-handle Share/Private/inherit control
     writes `com.jkali.contact_overrides`.
  2. **Profile fan-out:** a profile-level control writes ONE override key per
     linked handle — but only for handles that pass `SOURCE_KEY_RE` +
     known-source gates AND reconcile against `/contacts/list`; unmatched
     handles are refused VISIBLY ("2 of 3 handles applied; 1 is not in your
     imported contacts") — a `'private'` that silently never applies is a
     leak the user believes closed, and a `'share'` on a not-yet-imported
     handle would be a dormant grant (F7). The panel surfaces the total
     active-override count including unmatched keys, with a clear-all.
     Acceptance: fan-out on a two-handle profile produces both keys and both
     mirrored rows tombstone next pass.
- **Write discipline (F3):** every overrides (and profiles) write is a MERGE
  over a fresh read, never a blind PUT of a cached map; the read
  distinguishes 404 (→ empty) from any other failure (→ controls DISABLED,
  no write permitted this session — EXCEPT, for the specific over-cap
  stored-map state, single-key removal and clear-all, which are the in-app
  recovery path). `readProfiles` is fixed the same way in
  this slice (its current catch-all → empty + blind PUT can destroy the
  whole profile store on a blip). Unit test: read raises 500 → zero PUTs.
- **Consent-write invariant (F8), acceptance criterion:** no consent control
  may swallow a write error; a failed write is surfaced and the control
  renders last-known-good state — never the requested state. The two
  identified silent catches (buildTriStateSlider's handler and the global
  contacts switch) are fixed accordingly; the C3 silent-write bug is
  root-caused with a DevTools network trace as the discriminator.
- **Retraction honesty (F2):** per-contact revoke copy states: "turning a
  contact off stops sharing it and removes it from your manager's list; it
  cannot un-send what was already mirrored" (tombstones are state events —
  prior content stays retrievable to joined members). The existing consent
  copy claiming removal "removes the contacts already shared" is corrected.
  NEWLY created contacts rooms set `history_visibility: joined` as partial
  future mitigation (existing rooms unchanged, documented).
- Copy: "overrides the <source> setting for this contact only".
- **Bug fix in the same slice:** the existing share affordance the user
  clicked earlier wrote NOTHING to account-data (both
  `com.jkali.contact_profiles` and the policy were 404 afterward). Find the
  control, determine why its write never lands (silent catch? wrong path?
  dead handler after the S2 rework?), and either fix it or replace it with
  the new per-contact control. The failure mode must become visible (surface
  write errors; no silent catch).

### C4. Tests

- `tests/unit/contact_consent_py.test.py` + `tests/unit/contact_consent.test.js`
  (actual file names): override precedence (override > source > global),
  unknown values → inherit, malformed keys dropped, byte-parity.
- `tests/unit/uplink_reconcile.test.py` (or the contact-mirror test file):
  override-share in private source pushes one row; override-private in
  share-all source tombstones the mirrored row; absent overrides behave
  exactly as today (regression).
- Cap behavior (closing-review blocker): a fan-out that would exceed the
  entry cap performs ZERO PUTs and renders a visible refusal naming the
  cap; a clear-all issued from an over-cap stored map succeeds and returns
  controls to normal (next uplink pass resumes pushes). Both unit-tested.
- Conformance extension is MANDATORY, at the three call sites: the
  `resolve_contact_share` case in `tests/conformance/consent_eval.mjs`
  (~line 33), the vector generator in
  `tests/conformance/consent_conformance.py` (~line 322) and its dispatcher
  (~line 360) — vectors gain an override field (share/private/unknown/absent)
  with a nonzero override-bearing count — plus a new
  `normalize_contact_overrides` vector kind. Acceptance: the conformance run
  passes AND reports override-bearing `resolve_contact_share` vectors.

## Non-goals

- No change to conversation consent, the direct-send gates, or the
  contacts importer.
- No per-contact 'direct' concept (contacts are data, not conversations).

## Rollback

Old code ignores the new account-data type, with a stated ASYMMETRY:
`'share'` overrides narrow safely on revert (the contact stops mirroring and
is tombstoned by source policy), but a `'private'` override inside a
`share-all` source RE-WIDENS on revert — old code re-resolves the withheld
contact as shared and re-pushes its PII. Operator step before any revert:
flip the affected source(s) to `private-all` and let one tombstone pass run;
re-enable per-source sharing only after re-curating. Single commit.

## Security review — findings and dispositions

Reviewer: pilotfish:security-reviewer, 2026-09-02 (read-only, pre-implementation).

| # | Sev | Finding (abridged) | Disposition |
|---|-----|--------------------|-------------|
| F1 | P1 | GET /contacts/list would be ungated (helper GETs are liveness-only; gates are CSRF controls, not authn); DNS-rebinding angle; broader PII than /enrich/numbers precedent | Mitigated-by-C3.1 (POST behind _authorized(), Origin-required, Host allowlist, parameterless, capped, source-filtered, JS validation); local-process residual accepted + documented |
| F2 | P1 | Tombstones do not retract mirrored PII (state history remains readable); shipped copy claims otherwise | Mitigated-by-C3 copy correction + stated invariant; history_visibility: joined on new contacts rooms; residual accepted for existing room |
| F3 | P1 | Fail-to-empty read + blind PUT silently erases 'private' denies (pattern exists in readProfiles today) | Mitigated-by-C3 write discipline (404-vs-error split, merge-over-fresh-read, controls disabled on failed read; readProfiles fixed too; 500→zero-PUTs test) |
| F4 | P2 | Optional overrides parameter → silent source-only fallback; override could become a content source | Mitigated-by-C2 (required positional; ordering pinned; gate-only invariant + test) |
| F5 | P2 | Unrecognized override VALUE falls through to inherit → loses a deny in share-all source; key/body malformation semantics unspecified | Mitigated-by-C1 key/value spec (non-dict overrides field = read-failure; entry cap; own-property discipline) + conformance vectors |
| F6 | P2 | '|' is legal inside network_id (importer + UI regexes admit it) | Mitigated-by-C1 (first-| split only; SOURCE_KEY_RE prefix makes keys injective) |
| F7 | P2 | Fan-out can mint never-applying keys: silent failed 'private' = believed-closed leak; unmatched 'share' = dormant grant firing on next import | Mitigated-by-C3.2 (gated keys, visible reconcile report, active-override count + clear-all) |
| F8 | P2 | Silent-catch sites hide consent-write failures (likely the reported bug); user shown requested state not real state | Mitigated-by-C3 consent-write invariant as acceptance criterion; root-cause with network trace |
| F9 | P2 | Override keys are PII; logging them leaks the address book into logs/ | Mitigated-by-C2 logging invariant extension (uplink, UI, helper _diag) |
| F10 | P2 | Push leg samples consent once per pass (minutes on first backfill); no per-push freshness | Mitigated-by-C2 re-read every 50 pushes; residual sub-chunk window accepted + documented |
| P3s | P3 | Tombstone flap on read failure; read_contact_policy error asymmetry; homoglyph display names; serial-server response size; CORS methods widening | All adopted: tombstone-leg cache, policy-read alignment, network_id always rendered, response cap, POST (no CORS change) |

Bounding note (hostile local account-data writer): adds no new capability —
that writer already controls the per-source policy and conversation
overrides; the override is a per-row boolean gate and never a content
source. Marginal risk is visibility (dormant grants), addressed by F7's
surfacing.

## Risks

- Widening path: an override `'share'` shares one contact's PII from an
  otherwise-private source — that is the feature, teammate-explicit, same
  consent surface as everything else.
- Key ambiguity: composite key must match `handle_owner`'s exactly, or an
  override silently fails to apply (inherit — fails safe); pinned by a
  parity test using the same key-builder.
