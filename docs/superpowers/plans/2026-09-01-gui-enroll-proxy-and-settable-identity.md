# GUI Enroll-Proxy + Settable Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a teammate pick and enroll to any (incl. remote/Tailscale) master entirely from the user-app GUI without a CSP change, and make the local hub identity a settable/changeable value instead of the hardcoded `jkali`.

**Architecture:** Two independent features. (A) **Settable identity** — the local Matrix account's localpart + display name become install-time config (default derived from the Mac), and the mxid the six bridges grant permissions to is parameterized in the tracked templates. (B) **GUI enroll-proxy** — a new loopback endpoint on the already-running session-connect helper performs the master `/enroll/exchange` server-side (dodging the loopback-only CSP), `orglink.js` calls it instead of fetching the remote master directly, and `setup.sh` pre-installs the idle uplink daemon so a GUI Connect both enrolls and starts mirroring.

**Tech Stack:** bash + Python 3.9 stdlib (`urllib`, `sqlite3`), vanilla ES-module JS (no bundler), Synapse + mautrix bridge YAML, launchd.

**Spec:** This plan is its own spec (internal tool, direct user request 2026-09-01). Binding decisions captured in Global Constraints.

## Global Constraints

- **CSP is not widened.** `apps/user/index.html` and `views/nginx.conf` `connect-src` stay byte-identical and loopback-only (`tests/unit/csp_parity.test.js` must stay green). Feature B works *because* it reuses the already-allowed 8021–8025 helper range.
- **The new enroll-exchange endpoint inherits session-connect's F1/F6 posture exactly:** loopback bind only; every POST gated by Origin ∈ {`http://127.0.0.1:8011`,`http://localhost:8011`} + `Content-Type: application/json` + `X-Beepa-Connect: 1` before any side effect; the enrollment code, the master URL query, and the returned credentials are **never logged** (log_message stays a no-op; `_diag` carries only method/path/origin/status). It returns only the five known credential fields, never a raw upstream body.
- **Bridge auth must not break.** `@jkali:localhost` appears in 7 tracked templates (6 bridge `permissions:` blocks + the gmessages appservice user-regex in 2 files). All must resolve to the *same* chosen mxid, or bridges 401 / stop puppeting. `hub/render-hub.sh --verify` must still round-trip byte-for-byte after parameterization.
- **`consent.py` ↔ `consent.js` byte-parity is untouched by this plan** (no consent logic changes here).
- **The master-visible identity is unchanged** — it is chosen by the manager at enrollment (`@worktrial:master`), independent of the local identity this plan makes settable.
- **Fresh-install semantics.** The identity change is an install-time choice (bridges bind to the mxid; changing it later means re-login). Existing installs keep their current mxid unless reset.

---

### Task A1: Identity as install-time config

**Files:**
- Modify: `setup.sh` (the `.env` mint step — add identity resolution)
- Modify: `hub/provision-user.sh:18` (already reads `LOCAL_LOCALPART`; add display-name plumbing)
- Reference: `.gitignore` (`/.env` already ignored — display name + localpart persist there)

**Interfaces:**
- Produces: `.env` gains `LOCAL_LOCALPART=<slug>` and `LOCAL_DISPLAYNAME=<name>`. `render-hub.sh` (Task A2) reads `LOCAL_LOCALPART` to derive `LOCAL_MXID=@<localpart>:localhost`. `provision-user.sh` reads both.
- Default derivation: localpart = `id -un` lowercased, non-`[a-z0-9._=/-]` → `-`, collapsed; display name = `id -F` (falls back to the localpart). Both overridable by pre-set env or an interactive prompt when `setup.sh` runs on a TTY and the values are unset.

- [ ] **Step 1:** In `setup.sh`, before rendering, resolve identity: if `LOCAL_LOCALPART` unset, slugify `id -un`; if `LOCAL_DISPLAYNAME` unset, use `id -F` (fallback to the localpart). On a TTY, echo the resolved values and allow the user to accept or type replacements. Write both into `.env` alongside `POSTGRES_PASSWORD`/`HOST_UID`/`HOST_GID` (only if not already present — never rotate an existing install's identity).
- [ ] **Step 2:** Export `LOCAL_LOCALPART` and `LOCAL_DISPLAYNAME` into the environment for the `render-hub.sh` and `provision-user.sh` calls that follow in `setup.sh`.
- [ ] **Step 3 (verify):** `bash -n setup.sh`; run `setup.sh`'s identity block in isolation with `LOCAL_LOCALPART`/`LOCAL_DISPLAYNAME` preset and confirm it does not overwrite them; unset and confirm the Mac-derived defaults appear.
- [ ] **Step 4:** Commit.

---

### Task A2: Parameterize the bridge-permission mxid in templates

**Files:**
- Modify (7): `hub/templates/meta/config.yaml.tmpl`, `hub/templates/twitter/config.yaml.tmpl`, `hub/templates/gmessages/config.yaml.tmpl`, `hub/templates/gmessages/registration.yaml.tmpl`, `hub/templates/synapse/gmessages-registration.yaml.tmpl`, `hub/templates/linkedin/config.yaml.tmpl`, `hub/templates/whatsapp/config.yaml.tmpl`
- Modify: `hub/render-hub.sh` (derive + substitute `LOCAL_MXID`)
- Modify: `hub/make-templates.sh` / `hub/_make_templates.py` (scrub the live mxid → `${LOCAL_MXID}` when re-freezing, so re-running make-templates does not undo the parameterization)

**Interfaces:**
- Consumes: `LOCAL_LOCALPART` from `.env` (Task A1).
- Produces: rendered configs contain the real `@<localpart>:localhost`. The gmessages regex line becomes `- regex: ^@<localpart>:localhost$`.

- [ ] **Step 1:** Replace the literal `@jkali:localhost` with `${LOCAL_MXID}` in all 7 template files (permission-map keys and the two `regex:` lines). Leave every mautrix-native `${...}` untouched.
- [ ] **Step 2:** In `render-hub.sh`, after loading `.env`, derive `LOCAL_MXID="@${LOCAL_LOCALPART:-jkali}:localhost"` and pass it into the `_render_subst.py` var list (like `DB_PASSWORD` — a plain value, **not** a minted secret, so it is not added to `.hub-secrets.local`). Add `LOCAL_MXID` to the `_render_subst.py` allowlist for each template render call.
- [ ] **Step 3:** In `_make_templates.py`, add a literal→placeholder rule mapping the live rendered mxid (`@<localpart>:localhost`, read from `.env` or the rendered synapse config) back to `${LOCAL_MXID}` in those 7 output templates.
- [ ] **Step 4 (verify — load-bearing):** With `.env` containing `LOCAL_LOCALPART=jkali`, run `hub/render-hub.sh --verify` and confirm a byte-clean round-trip against the working tree (proves the parameterization is faithful for the current identity). Then render with a *different* localpart into a scratch `OUT_ROOT` and grep the 7 outputs to confirm the new mxid appears everywhere `@jkali:localhost` used to.
- [ ] **Step 5:** Commit.

---

### Task A3: Set the Matrix display name at provisioning

**Files:**
- Modify: `hub/provision-user.sh` (after login/token, before writing session files)

**Interfaces:**
- Consumes: `LOCAL_DISPLAYNAME` (Task A1), the freshly obtained access token.

- [ ] **Step 1:** After the token is obtained, if `LOCAL_DISPLAYNAME` is non-empty, `PUT /_matrix/client/v3/profile/@<lp>:localhost/displayname` with `{"displayname": "<LOCAL_DISPLAYNAME>"}` using the token (python `urllib`, same injection-safe pattern as the existing login block). Best-effort: a failure logs a warning, never fails provisioning.
- [ ] **Step 2 (verify):** After a provision run, `GET .../displayname` and confirm it echoes the configured name.
- [ ] **Step 3:** Commit.

---

### Task B1: Loopback enroll-exchange endpoint (SECURITY-SENSITIVE)

**Files:**
- Modify: `session-connect/connect_server.py` (add the route to `do_POST`, behind the existing `_authorized()` gate)
- Test: `tests/unit/` — a new guard test mirroring the existing session-connect guard tests

**Interfaces:**
- Produces: `POST /enroll/exchange` on the session-connect loopback base. Request `{"master_url": "<https url>", "code": "<one-time code>"}`. Behaviour: validate `master_url` scheme (`https`, or `http` only for a `127.0.0.1`/`localhost` host to preserve local-master testing); POST `{"code": code}` to `<master_url>/enroll/exchange` server-side via `urllib` with a short timeout and default TLS verification; on upstream 200, return exactly `{master_hs_url, master_user, master_token, manager_mxid, master_space}` extracted from the upstream JSON; on any failure return a fixed generic `{status:"failed"}` with an appropriate 4xx/502 and log nothing sensitive.

- [ ] **Step 1 (write failing guard test):** New `tests/unit/enroll_proxy_guard.test.py` (pure, no network) asserting: no Origin → 403; wrong Origin → 403; missing `X-Beepa-Connect` → 403; wrong `Content-Type` → 403; and that the handler is registered. Model it on the existing session-connect guard tests. Run: expect FAIL (route absent).
- [ ] **Step 2:** Implement the route in `do_POST` **after** `_authorized()`. SSRF containment: reject a `master_url` whose scheme is not https unless the host is loopback; never echo the upstream body; map every error to the generic failure. Keep `log_message` a no-op and add nothing sensitive to `_diag`.
- [ ] **Step 3 (verify):** Guard test passes. Manually: `curl -si -X POST <base>/enroll/exchange` → 403; with full F1 headers + a bogus master_url → generic failure, and confirm `session-connect/logs/` contains no code/URL/token.
- [ ] **Step 4:** Update `session-connect/CLAUDE.md` Endpoints + Security-invariants sections to document the new endpoint under the same F1/F6 posture.
- [ ] **Step 5:** Commit.

---

### Task B2: Route orglink.js through the loopback proxy

**Files:**
- Modify: `apps/user/orglink.js` (the `connectBtn` handler, lines ~114-152)
- Reference: `shared/ui/connections.js:325` (`sessionConnectBase`), `apps/user/enrich.js` (identical copy + `SESSION_CONNECT_HEADERS`)

**Interfaces:**
- Consumes: `sessionConnectBase()` + the F1 headers. Produces: the same account-data write to `com.jkali.master_link` as today (unchanged).

- [ ] **Step 1:** Import `sessionConnectBase` (and reuse the F1 header constant) from the shared module. In the handler, replace the direct `fetch(base + '/enroll/exchange', …)` with `fetch(await sessionConnectBase() + '/enroll/exchange', { method:'POST', headers: SESSION_CONNECT_HEADERS, body: JSON.stringify({ master_url: base, code }) })`. Keep the existing `!res.ok` / creds-shape / account-data-write / warn paths.
- [ ] **Step 2:** Update the card's helper note: the terminal-fallback line stays (for a machine with no helper running), but the primary path is now GUI-only. Keep `textContent`-only, no CSP change.
- [ ] **Step 3 (verify):** Serve the app, open Settings → Connect to organization, and confirm the request goes to `127.0.0.1:<helper port>` (allowed by CSP) — no CSP violation in the console. (Full happy-path enroll is exercised live in the fresh install.)
- [ ] **Step 4:** Commit.

---

### Task B3: Pre-install the idle uplink daemon at setup

**Files:**
- Modify: `setup.sh` (its `install_agent` sequence — add `com.jkali.uplink`)
- Reference: `agents/uplink/com.jkali.uplink.plist`, `agents/uplink/run-uplink.sh`, `uplink.py:1401-1407` (idles without creds)

**Interfaces:**
- Consumes: nothing new. Produces: `com.jkali.uplink` loaded and idling; it begins mirroring within one loop after the GUI writes `com.jkali.master_link`.

- [ ] **Step 1:** Add `com.jkali.uplink` to the launchd jobs `setup.sh` sed-renders + loads (same path-rewrite it already does for the other agents). Confirm `run-uplink.sh` sources `agents/uplink/uplink.env.local` *if present* and otherwise relies on the account-data path — the daemon must start cleanly with neither.
- [ ] **Step 2:** Ensure `reset.sh` already unloads `com.jkali.uplink` (it does — verify the label is in its loop) so the fresh-install cycle is symmetric.
- [ ] **Step 3 (verify):** `launchctl load` the job with no creds present; `tail agents/uplink/logs/uplink.log` shows the "not connected — connect from the user app" idle line and no crash loop.
- [ ] **Step 4:** Commit.

---

### Task B4: Full-suite gate + security review

- [ ] **Step 1:** Run `tests/run.sh` (unit + conformance) and confirm green, including `csp_parity.test.js` (unchanged) and the new `enroll_proxy_guard.test.py`.
- [ ] **Step 2:** Dispatch a security review of `session-connect/connect_server.py`'s new endpoint (SSRF containment, F1/F6 adherence, no credential logging) before it goes live.
- [ ] **Step 3:** Address any findings; re-run the suite.

---

## Fresh install (user-in-the-loop — STOP before running)

Destructive and partly interactive; do **not** run autonomously. After the code lands and the suite is green, walk the user through: `./reset.sh` (typed `reset`) → `./master-setup.sh` is already up (skip) → `./install.sh` (Docker, render with the new identity, provision, one-click Connects, GUI enroll to the master). WhatsApp QR + macOS TCC prompts require the user at the keyboard.

## Self-Review

- **Coverage:** Ask 1 → B1/B2/B3 (proxy + orglink + idle daemon, no CSP change). Ask 2 → A1/A2/A3 (settable localpart + display name + parameterized bridge auth). ✓
- **Type consistency:** exchange response keys (`master_hs_url,master_user,master_token,manager_mxid,master_space`) match across B1 (return), B2 (account-data write), and `orglink.js`'s existing reader. ✓
- **Placeholder scan:** none — every task names exact files/lines and the concrete change. ✓
- **Risk:** the only security-sensitive new surface is B1; it is gated identically to the existing helper and reviewed in B4. Bridge-auth faithfulness is proved by A2 Step 4's `--verify` round-trip. ✓
