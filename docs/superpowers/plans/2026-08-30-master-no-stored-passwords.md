> STATUS: DONE 2026-08-31. Implemented, live master migrated (all four
> teammates + manager on derived passwords; enrolled uplink token survived),
> enrollment 9/9, fresh verifier CONFIRMED all 8 gates; scratchpad backups
> deleted after verification. Deferred P4: derive_password's explicit key=
> parameter bypasses the length floor (unreachable today).

# Master: stop storing teammate/manager passwords in cleartext

**Problem (verified 2026-08-30):** `master/.provision-state.local` (mode 600,
gitignored) holds every provisioned master account's password in cleartext:
`MANAGER_PW='…'` (default literal `password`) and one `PW_<USER>='…'` per
teammate (`jkali`, `verifyx`, `alice`, `bob` on this host).

- Written by `master/provision.sh:164-168` and by `master/enroll.py`
  `add_teammate()` (~line 470).
- Read by `provision.sh:51` (`source "${STATE_FILE}"`) and by
  `enroll.py:_teammate_facts()` (line 146) → `_login()` inside `exchange()`,
  the only runtime consumer: redeeming an enrollment code password-logs-in
  **as the teammate** to mint the fresh scoped token their uplink uses.

The passwords exist for one reason — minting tokens later — and without an
admin account the only alternative is to be able to *reproduce* them.

## Design (revised after security review, see table at the end)

**Derive, don't store.** Every master-side account password is a
deterministic function of one master-held key, with domain separation:

```
teammate: urlsafe_b64( HMAC-SHA256( KEY, b"beepa-teammate-password-v1\0" + localpart ) )[:32]
manager : urlsafe_b64( HMAC-SHA256( KEY, b"beepa-manager-password-v1\0"  + "manager"  ) )[:32]
```

- `KEY` = the ASCII bytes of `TEAMMATE_PASSWORD_KEY` in
  `master/synapse/.secrets.local` (mode 600, gitignored). **Single writer:
  `master/setup.sh`** generates it with the other secrets (reused on re-run)
  and writes it inside the `{ … } > "${SECRETS}"` block. `provision.sh` and
  `enroll.py` **fail loudly** ("run master/setup.sh") if it is absent or
  shorter than 32 chars; nothing self-heals, nothing else writes that file.
  A missing key never falls through to `hmac.new(b"")`.
- Optional `TEAMMATE_PASSWORD_KEY_PREV` (same file): if present, the
  helper below tries the previous key's derivation as a fallback and
  rotates to the current one — rotation reuses the migration machinery.
  `setup.sh` reads and re-emits `_PREV` unchanged when present, so a re-run
  mid-rotation cannot drop it. **Precedence for these two keys (unlike the
  other secrets, which stay file-wins):** `setup.sh` captures
  `ENV_TEAMMATE_PASSWORD_KEY="${TEAMMATE_PASSWORD_KEY-}"` and
  `ENV_TEAMMATE_PASSWORD_KEY_PREV="${TEAMMATE_PASSWORD_KEY_PREV-}"` **and the
  set-ness** `ENV_PREV_SET="${TEAMMATE_PASSWORD_KEY_PREV+set}"` **before**
  `source "${SECRETS}"` (the `ENV_TEAMMATES` pattern at `provision.sh:39`;
  `source` clobbers the env variables, so set-ness must be captured first).
  The `_PREV` decision branches on `ENV_PREV_SET`: set + non-empty → emit
  the env value; set + empty → omit the line; unset → emit the file value if
  present, else omit. The current key: non-empty env → env; else file; else
  `gen_secret`. `TEAMMATE_PASSWORD_KEY` is never emitted empty. Acceptance:
  from a file containing a `_PREV` line, `TEAMMATE_PASSWORD_KEY_PREV=
  master/setup.sh` leaves no `TEAMMATE_PASSWORD_KEY_PREV=` line and the
  current key unchanged; a plain re-run leaves the key list byte-identical. **Rotation procedure** (documented in `master/CLAUDE.md`):
  `TEAMMATE_PASSWORD_KEY_PREV=<old> TEAMMATE_PASSWORD_KEY=<new> master/setup.sh`
  → `master/provision.sh` (every account logs in with `_PREV`, rotates to
  the new key, `migrated:true`) → remove `_PREV` by running `setup.sh` with
  `TEAMMATE_PASSWORD_KEY_PREV=` (empty) once every account reports
  `migrated:false`.
- **Single implementation** in `master/enroll.py`: `derive_password(kind,
  localpart)`; localpart must match `[a-z0-9]{1,64}` (rejected, never
  normalised); `manager` is reserved for the manager kind (provision.sh's
  roster refuses it).
- **No password ever crosses argv or a shell variable.** New subcommand
  `enroll.py provision-account <localpart> [--manager]` does, in Python:
  register **with the derived password** via the shared-secret HTTP flow
  (skip if exists) → try login with the derived password → else try
  `_PREV`-derived → else the legacy password (`PW_<U>` / `MANAGER_PW` from
  `.provision-state.local`, or `MANAGER_PW` env for the manager) → on a
  legacy/`_PREV` success **rotate** to the derived value via
  `POST /_matrix/client/v3/account/password` with the fresh bearer token and
  body `{"new_password": <derived>, "logout_devices": false, "auth": {…}}`
  (a storage-format migration, not a revocation — every already-enrolled
  uplink's token must keep working), full UIA (first POST without `auth` →
  401 `{session, flows}` → resubmit with `auth: {type: m.login.password,
  identifier: {type: m.id.user, user}, password: <the OLD password that
  just logged in>, session}`) → then drop that account's legacy key
  from the state file **immediately** (per-account write, crash-safe: the
  next run simply succeeds derived-first). Prints **only** JSON
  `{"mxid","token","migrated"}` to stdout; errors to stderr, non-zero exit.
  `provision.sh` captures that JSON (plain assignment, never `local x=$(…)`),
  validates `mxid`/`token` are non-empty, and proceeds to the space step
  exactly as today. `register_new_matrix_user`/`curl -d password` disappear
  from `provision.sh`.
- `enroll.py password <localpart>` (and `--manager`) stays as an
  operator-only convenience — the manager's console login is the derived
  value — documented as "prints a secret: never redirect to a file, never
  wire into `serve`"; `serve()` gains **no** route for it.
- `_teammate_facts()` returns `(mxid, space)`; `exchange()` logs in with
  `derive_password("teammate", teammate)`; `add_teammate()` registers with
  the derived password and writes only `SPACE_<U>` + `TEAMMATES`; it keeps
  refusing an account that exists but is not managed here.
- State file after migration: header + `TEAMMATES` + `SPACE_<U>` only
  (non-secret). `tokens.local` unchanged by this plan (bearer tokens used by
  the console tooling/harness; copies of it are copies of live credentials).

**Security properties, stated honestly:** the key grants exactly the
authority the stored passwords granted; `REGISTRATION_SHARED_SECRET` in the
same file already implied it (it can register admin accounts). Net: N+1
stored passwords → 0, one key in the file that is already the crown jewel.
Two properties the old scheme had that this one does not: a key read at
time T also compromises accounts created **after** T (forward compromise),
and rotation touches every account (mitigated by `_PREV`). Invariants: the
key is never copied anywhere the registration secret isn't (not
`tokens.local`, not `master/.env`, not the launchd plist); the file is inside
the Synapse bind mount (`/data`), same as the registration secret today.

**Non-goals:** removing bearer tokens from `tokens.local`; Synapse admin API;
changing the enrollment protocol, code store, or client.

## Files

- `master/enroll.py` — `derive_password`, `provision-account` + `password`
  subcommands, `_change_password` (UIA, `logout_devices:false`),
  `_remove_shell_vars`, `_teammate_facts`/`exchange`/`add_teammate` changes,
  docstring rewrite (no "reads teammate passwords").
- `master/provision.sh` — roster validation `[a-z0-9]{1,64}` + `manager`
  refused; per-account `provision-account` call; manager via
  `provision-account manager --manager` (env `MANAGER_PW` = legacy input
  only); never writes `MANAGER_PW`/`PW_*`; `register`/`login_token`/`gen_pw`
  removed.
- `master/setup.sh` — `TEAMMATE_PASSWORD_KEY` generated + written with the
  other three; double-run leaves it unchanged.
- `master/run-enroll.sh`, `master/CLAUDE.md` (lines ~41/56/91 rewritten,
  `password` carve-out, key-copy invariant, bind-mount note, rotation
  procedure), `tests/CLAUDE.md` (20 unit tests), `tests/run.sh` (+1).
- `tests/unit/enroll_password_derivation.test.py` — deterministic; differs
  per localpart, per kind (teammate `manager` ≠ manager), per key; 32
  url-safe chars; invalid localpart rejected; missing/short key raises
  (asserted on the exception); UIA password-change request shape carries
  `logout_devices: false` (stubbed transport).
- Live migration on this host: `master/setup.sh` (adds the key; no
  container action) → fresh backups of `.provision-state.local` +
  `tokens.local` to the scratchpad (600) → `TEAMMATES="jkali verifyx alice
  bob" master/provision.sh` (note: pins `MASTER_TEAMMATE_*` aliases to
  `jkali`) → assert **no secret-bearing assignment lines** remain:
  `grep -E "^(MANAGER_PW|PW_[A-Z0-9_]+)='" master/.provision-state.local
  master/tokens.local master/synapse/.secrets.local` exits 1 (source-code
  occurrences of the legacy-fallback identifiers in `provision.sh`/`enroll.py`
  are expected and out of scope of this check) → restart
  `com.jkali.master-enroll` (between the state-file rewrite and this restart
  the old `serve` process answers 502 on exchange — a documented window of
  seconds, no code is lost) → `python3 tests/integration/test_enroll.py`
  (mint → exchange → scoped token; cross-user 403; reuse/expired/invalid
  refused) → assert the **live** uplink's existing master token still
  authenticates (`whoami` with the token in `agents/uplink/uplink.env.local`
  / `com.jkali.master_link`) → **delete the scratchpad backups**.

## Security review (pilotfish:security-reviewer, 2026-08-30) — dispositions

| # | Finding | Disposition |
|---|---|---|
| F1 P1 | default `logout_devices:true` kills live uplink tokens | **FIX** — `logout_devices:false` with bearer; verifier checks an enrolled token survives |
| F2 P1 | `setup.sh` re-render drops an appended key | **FIX** — key written by `setup.sh` inside the secrets block |
| F3 P1 | two self-healing writers of `.secrets.local` | **FIX** — single writer; others fail loudly |
| F4 P1 | manager password never rotated | **FIX** — derived (own prefix), rotated in the same migration |
| F5 P2 | random-once manager → lockout path | **FIX** — derived, retrievable via `password --manager` |
| F6 P2 | derived password in `docker`/`curl` argv, stdout/history | **FIX** — Python `provision-account` helper; `password` carve-out documented; no HTTP route |
| F7 P2 | migration not crash-safe | **FIX** — derived-first, per-account legacy-key removal |
| F8 P2 | localpart canonicalisation | **FIX** — `[a-z0-9]{1,64}` validated in both, `manager` reserved |
| F9 P2 | shell capture masking | **FIX** — JSON handoff, plain assignment, validated |
| F10 P2 | scratchpad backups hold live tokens | **FIX** — explicit deletion step after verification |
| F11 P3 | derivation mechanics | **ACCEPT** — key = ASCII bytes, ≥32-char floor, missing → exception |
| F12 P3 | `tests/run.sh` entry; keep "not managed here" refusal | **FIX** |
| F13 P3 | `_PREV` rotation, logging, bind mount, stale docs | **FIX** (`_PREV` read path implemented; docs) |

## Gates

Secrets/authn change → plan-verifier on this revision, then fresh verifier
on: "no `MANAGER_PW='…'`/`PW_*='…'` assignment lines in
`master/.provision-state.local`, `master/tokens.local`,
`master/synapse/.secrets.local` (and a deliberately re-added `PW_TEST='x'`
line makes that grep exit 0, proving the check is live); `setup.sh` twice —
with `TEAMMATE_PASSWORD_KEY_PREV` present — leaves the key list (names and
values) byte-identical; enrollment mint→exchange→token works for an existing
and a newly added teammate; the pre-migration enrolled uplink token still
authenticates; the manager can password-login (CS API, the same call
`apps/master/index.html`'s form makes) with `enroll.py password --manager`'s
output; a second `provision.sh` run reports `migrated:false` for every
account."
