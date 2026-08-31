# Consent resolver: shared conformance harness + identical input canonicalisation

**Problem:** the consent resolver — the authorization boundary that decides
whether a conversation or an address-book source leaves a teammate's machine —
is implemented twice on purpose: `shared/model/consent.js` (what the UI shows)
and `agents/uplink/consent.py` (what the daemon enforces). Parity is asserted
only by two hand-mirrored curated test files. A missed case where Python says
"share" and the JS UI showed "private" would leak a private conversation to
the manager under a green build.

**Reachability baseline (security review, 2026-08-30):** in production the
resolvers never see malformed input — `policy` is always normalizer output
(`uplink.py:468`, `consent.js:169`), `sourceId` is one of six code-owned
strings (`uplink.py:547-556,600`; `apps/user/main.js:148`), profile maps are
`ROOMID_RE`-validated on both sides. So this change is **defense-in-depth
plus a permanent drift gate**, not an active-leak fix. It is still worth
doing because the next drift will be in exactly the shape nobody tested.

**Evidence (new harness `tests/conformance/consent_conformance.py`, 84,416
vectors = 60,416 exhaustive `resolve()` combinations + 24,000 seeded fuzz
across all 8 entry points; re-baselined after the >2^53-integer harness fix
below):** 14,379 vectors differ or crash in 858 distinct classes — **7 are
decision-level** (`DECISION DIFFERS`: the `shared` boolean differs), 14,192
are Python crashes where JS decides, 0 JS errors, the rest reason-string
drift. Decision-level divergences found:

1. `convo.sourceId = 5` with `sources: {"5": …}` — JS keys numbers as
   strings and honours the rule, Python does not.
2. `sources: {"__proto__": "share-all"}` — JS `normalizePolicy` drops it
   (assignment hits the prototype setter), Python keeps it; JS `resolve`
   on a JSON-parsed object then reads an own `__proto__` key — asymmetric
   both ways.
3. Array-shaped `sources` with `sourceId: "0"` — JS `typeof [] === 'object'`
   so `sources["0"]` matches, Python requires a dict (found by review; the
   generator never produced `"0"` — added).

Plus ~14k Python crashes (`AttributeError`/`TypeError`) on non-string labels,
non-object profiles, list `sourceId`s, junk `/sync` shapes, while JS decides;
and junk reason strings in JS (`"all "`, `"all [object Object]"`).

## Fix

### A. One explicit canonicalisation, implemented identically in both

Every input is type-gated **before** comparison, same rules and order in
both languages. Anything failing a gate is treated as *absent* — **absent is
safe only relative to the more-specific levels**: the per-source level
carries a deny (`private-all`), so a dropped malformed per-source rule can
fall through to `global: share-all`. That is the recorded decision (it is
what the enforcer, Python, already answers today, so this reconciles the UI
up to the enforcer, never the other way), and it is pinned by curated cases
in all four consent test files so nobody "fixes" it back silently:
`sourceId: 5` + `sources: {"5": "private-all"}` + `global: "share-all"` →
`shared: true` on both sides; same for the contact-share variant.

| Input | Counts only if | Otherwise |
|---|---|---|
| `convo` | plain object (dict / non-array object) | no `sourceId`, no label |
| `convo.sourceId` | non-empty string | absent → no per-source rule can match |
| `convo.sourceLabel` | non-empty string | fall back to `sourceId` (if it counts), else `'source'` |
| `override` | exactly `'share'` or `'private'` | inherit |
| `profile` | plain object | absent → fall through |
| `profile.share` | exactly `'share'` or `'private'` | fall through |
| `profile.displayName` | non-empty string | `'profile'` |
| `policy` | plain object | `{}` |
| `policy.global` | exactly `'share-all'` | private |
| `policy.sources` | plain object (not array) — in `resolve`/`resolveContactShare` too | `{}` |
| per-source key (normalizers **and** lookups, **both dimensions**: conversation `sources` and contact-share `sources`) | key matches `^[a-z][a-z0-9]{0,31}$` **and** is an own property (JS `Object.prototype.hasOwnProperty.call`, Python `in`) **and** value is exactly `'share-all'`/`'private-all'` | dropped / inherit |
| `source` (contact) | non-empty string matching the same key regex | absent |
| `resolveAll` `overrides` / `profiles` containers | JS: `instanceof Map` (exact, not duck-typed `.get`) → Map lookup; `typeof profiles === 'function'` → call; else plain object with own-property lookup. Python: dict with `in`/`get` | no override / no profile |
| `overridesFromSync` output keys | room id matches the **static, server-agnostic** literal `^![^:]+:[A-Za-z0-9.\-:]+$`, defined identically inside **both** consent modules (`CONSENT_ROOMID_RE`) — never the runtime-mutable `shared/matrix/client.js` `ROOMID_RE`, which `configureMatrixBase()` rebinds to a server name; `overridesFromSync` must not depend on that state. Accepts `!a:local` (curated cases and harness `ROOM_IDS` unchanged) | event skipped |
| `/sync` shapes | each level a plain object / array as expected | skipped |

A JSON object carrying an own `"get"` key is a plain object on both sides
under the `instanceof Map` rule (previously JS duck-typed it as a Map — a
harness-invisible divergence, now closed).

**Every regex gate is matched with end-of-string semantics on both sides.**
Python's `$` also matches before a single trailing `\n`; JS's (non-multiline)
`$` does not. So Python uses `re.fullmatch(...)` (never `.match` with `$`)
for both `CONSENT_ROOMID_RE` and the source-key regex; JS keeps `^…$`. The
harness pools carry newline-suffixed canaries (`"!a:local\n"` in `ROOM_IDS`,
`"imessage\n"` in `SOURCE_IDS`) so the equivalence is proven, not asserted —
reverting Python to `$`-anchored `.match` must produce a `DECISION DIFFERS`
vector on `overrides_from_sync`.

The source-key regex is shape-based (matches `imessage`, `whatsapp`,
`gmessages`, `instagram`, `linkedin`, `twitter`; a new bridge id needs no
resolver change) and must never be tightened to something a real id could
fail — per the deny-drop note, dropping a key drops a `private-all` too.
Because `normalizePolicy` also runs on **write** (`consent.js:178-180`,
`apps/user/consent.js:80`), a dropped key silently discards a user setting;
acceptable only because no real id is dropped and the UI re-reads the
normalized body. No `Object.defineProperty` anywhere. Reason strings are
unchanged for every valid input.

**Harness-invisible surface, recorded:** `resolveAll`'s JS `Map`/function
container forms cannot be expressed in JSON and are JS-only; production JS
uses the `Map` form (`apps/user/consent.js:34,356`). Coverage of that form
stays with the curated `consent.test.js` Map cases. The harness proves the
JSON-shaped domain, which is the real input path on both sides.

### B. The harness is a permanent, every-run gate

- `tests/conformance/consent_conformance.py` (generator + Python evaluation
  + diff) and `tests/conformance/consent_eval.mjs` (a pure dispatcher onto
  the real JS exports, run in the pinned `node:20-alpine` container with the
  repo mounted **read-only**). No policy logic in the harness.
- Deterministic (`CONSENT_SEED` default 20260830, `CONSENT_FUZZ_N` default
  3000 → ~84k vectors, ~2 s). Generator fixes from review: `9007199254740991`
  (2^53−1) replaces the >2^53 integer (JS float rounding made echoed convos
  a false difference — never float-normalise in `canon`); `"0"`/`"1"` added
  to `SOURCE_IDS`; `"__proto__"` added to `ROOM_IDS`; unicode/NFKC lookalike
  and NUL-suffixed tokens added as canaries against a future `.trim()` /
  `.lower()` / `normalize` on one side.
- Fails on **any** differing output **or any crash on either side**, and
  labels each class `DECISION DIFFERS` when the `shared` boolean differs
  (leak class) vs reason-only drift, so a red build cannot be waved away.
  While a vector crashes on both sides nothing is learned about the
  decision; zero-crash acceptance resolves that — never merge a
  "stop the exception" patch without re-running the full gate.
- Wired into `tests/run.sh` after `consent_py.test.py`; run at the default
  seed and one alternate seed for acceptance (proves not seed-tuned).

**Why conformance vectors rather than generating one from the other:** both
consumers need a native implementation (browser ES module vs stdlib Python
daemon) with no build step; a generator adds a third artifact and a toolchain
to the boundary. The harness gives agreement on every JSON input and makes
the next drift a red build with a reproducing vector.

**Boundary statements (to `shared/CLAUDE.md` + `agents/uplink/CLAUDE.md`):**
`effective_shared()` is the only value the uplink acts on (single call site
`uplink.py:604`); `reason` strings are UI-only and must never be parsed for
authorization.

## Files

- `shared/model/consent.js`, `agents/uplink/consent.py` — gates per the
  table, same order.
- `tests/conformance/consent_conformance.py`, `tests/conformance/consent_eval.mjs`.
- `tests/unit/consent.test.js`, `consent_py.test.py`, `contact_consent.test.js`,
  `contact_consent_py.test.py` — the pinning cases only; nothing else changes.
- `tests/run.sh`, `tests/CLAUDE.md`, `shared/CLAUDE.md`, `agents/uplink/CLAUDE.md`.

## Acceptance

`python3 tests/conformance/consent_conformance.py` → `OK … no crashes` at
the default seed and `CONSENT_SEED=7`; all four curated consent test files
pass with only the pinning cases added; `tests/run.sh` green; integration
scenarios 5 and 6 (share-all standing policy; revoke at each level) pass.

## Security review (pilotfish:security-reviewer, 2026-08-30) — NO P0/P1

| # | Finding | Disposition |
|---|---|---|
| 1 P2 | "absent" drops a per-source deny under global share-all | **FIX** — stated + pinned cases (both dimensions) |
| 2 P2 | `defineProperty`/`__proto__` reconciles toward permissive; unenforceable | **FIX** — shape regex key gate + own-property lookup |
| 3 P2 | `resolveAll` containers / `overridesFromSync` keys ungated; Map form harness-invisible | **FIX** — rows added; caveat recorded |
| 4 P2 | >2^53 integer = permanent false difference | **FIX** — 2^53−1; re-baseline |
| 5 P3 | generator gaps (`"0"`, `__proto__` room id, lookalikes) | **FIX** |
| 6 P3 | shared-crash defers a divergence | **ACCEPT** — stated |
| 7 P3 | evaluator carries no logic | **ACCEPT** |
| 8 P3 | `:ro` mount; run.sh wiring | **FIX** |
| 9 P2 | boundary statements + decision-vs-reason classification | **FIX** |

## Gates

Plan-verifier on this revision; fresh verifier after on the Acceptance above.
