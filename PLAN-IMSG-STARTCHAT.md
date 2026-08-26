# Amendment: enable iMessage "start a new chat" (guarded) — imsg-startchat-v1

User approved (2026-08-26) enabling iMessage start-chat from the Directory,
knowingly widening the daemon's capability from "reply only in existing chats"
to "can initiate contact with a user-typed handle." This is the deferred D-1 /
M-18 amendment. Security-flagged → pilotfish:security-executor (Opus) builds;
Fable verifies. All Phase 1/2 M-*/N-*/B-*/R-* controls remain.

## Capability being added (and its bound)
The iMessage daemon gains ONE new management-room command, `start-chat`, that
creates a new iMessage chat to a user-typed handle. It does NOT let the daemon
derive destinations from bridged content (M-2 still governs: portal-room
messages are relay-only; only the mgmt room, only @jkali, can start a chat).

## Engine reality (verified in Phase 1)
`imessage-cli create-chat <recipient> --message "<text>"` REQUIRES a first
message — Messages.app cannot create a thread with no content. So "creates the
chat, sends no content" is not achievable; instead the FIRST MESSAGE is
user-typed in the mgmt-room command (destination AND content both user-
provided). This stays within the guardrail intent (nothing is derived from
bridged/remote content) — confirm in-slice that create-chat needs a message;
if a no-content form exists, prefer it.

## Constraints (governing; from the D-1 disposition + engine reality)
- **SC-1** `start-chat` executes ONLY in the persisted iMessage mgmt room
  (meta.mgmt_room), sender == @jkali:localhost, msgtype m.text, no
  m.relates_to, origin_server_ts within 120s — identical gating to
  status/setup (B-1/B-2/B-3). It is NOT reachable from any portal room.
- **SC-2** Command form: `start-chat <handle> | <first message>` (or a
  two-field hub form). The HANDLE is validated STRICTLY against
  `^\+[1-9]\d{6,14}$` (E.164 phone) OR a strict email
  `^[^\s@]+@[^\s@]+\.[^\s@]+$`; anything else → rejected, no engine call.
  The first-message text is treated as untrusted user text: clean_text +
  length clamp (reuse MAX_TEXT), never shell-interpolated.
- **SC-3** Rate/volume caps SEPARATE from the message rate limiter and the
  open limiter: ≤3 start-chats/hour AND ≤10/day; dedup an identical
  (handle) within 60s. Over cap → dropped + logged (body-free), bot replies
  "rate limited, try later."
- **SC-4** Engine call is list-argv, shell=False: `[CLI,"--no-events",
  "create-chat", handle, "--message", text]` (handle already regex-validated;
  no `--` needed per the Phase-1 finding, but handle/text are separate argv
  so no injection). No Matrix-derived value reaches a shell.
- **SC-5** Logging M-12: never log the handle or message; log
  outcome + a sha8 of the handle + counts only.
- **SC-6** Bot reply into the mgmt room: success ("started chat with <the
  handle the USER typed, echoed verbatim>") or a validation/rate error. The
  new portal then appears via the normal poll → iMessage space (no special
  path). The reply echoes only the user's own typed handle, never anything
  bridged.
- **SC-7** Hub side: flip iMessage `canStartChat` to true; the Directory
  iMessage control becomes a two-field form (handle + first message), both
  user-typed; on submit, strict client-side handle validation (same regexes)
  THEN `sendCmd('imessage', 'start-chat ' + handle + ' | ' + message)` through
  the C-1 unconditionally-verified mgmt-room path. Client validation is a UX
  pre-check; the daemon re-validates authoritatively (SC-2). A confirmation
  modal shows the exact handle + message verbatim before send.

## Slices
- **SC.A (daemon, Opus)** — add the `start-chat` command per SC-1..SC-6.
  *Acceptance*: in the mgmt room as @jkali, `start-chat +1<self-number> |
  pmmng-test-<nonce>` creates a chat and the nonce reaches iMessage (M-15:
  self only, ≤ caps); an invalid handle (`start-chat notaphone | x`) →
  rejected, zero engine calls; a `start-chat` from a PORTAL room or a GHOST →
  zero execution; the 4th start-chat within an hour → rate-limited, zero
  engine call; logs contain no handle/message text.
- **SC.B (hub, Opus)** — SC-7. *Acceptance*: iMessage Directory control is
  enabled as a two-field form; a bad handle is refused client-side with no
  send; a good handle shows the verbatim-confirm modal then drives the
  iMessage mgmt room (C-1 verified); node --check passes; 0 HTML-string sinks;
  hub CSP byte-identical.
- **SC.C (verify, Fable)** — confirm SC.A negative controls (invalid handle,
  portal/ghost, rate cap all zero-engine), one positive self-chat create,
  log hygiene, and SC.B static/code checks; hub headers unchanged.

## Rollback
Daemon: remove the `start-chat` command handler + caps table, restart launchd.
Hub: set iMessage canStartChat back to false, revert the Directory form.
No registration/homeserver change.

## Stops
Engine-visible create-chat tests target ONLY the user's own number/Apple ID
(M-15). Never create a chat to any other handle during testing. Any slice
failing after 2 fix attempts → stop and report.

## Security review dispositions (2026-08-26) — GOVERNING
Approved intent; 5 mitigations required in-slice. No P0/P1.
- **SC-P1 (P2) parser**: do NOT lowercase whole body or startswith-prefix.
  `raw=body.strip(); parts=raw.split(None,1); word=parts[0].lower();
  rest=parts[1] if len==2 else ""`. status/setup/help dispatch ONLY when
  `word in {...} and rest==""` (reject "status foo"). start-chat ONLY when
  `word=="start-chat" and rest!=""`. Never lowercase `rest`. Apply msgtype/
  no-relates_to/120s-freshness gate BEFORE dispatch for ALL commands. Log
  only `word`, never `rest`.
- **SC-P2 (P2) argv-option guard**: after validation add explicit
  `if handle.startswith("-"): reject` (email local-part could be
  `--message@...`). PHONE_RE uses ASCII digits: `^\+[1-9][0-9]{6,14}$`
  (re.ASCII / [0-9], to match the hub's JS \d). Pass the message as ONE argv
  token `"--message=" + text` (not two tokens) to avoid a leading-dash
  message binding oddly. Do NOT use a `--` terminator (would break --message).
- **SC-P3 (P3) split**: `handle_part, sep, message = rest.partition("|")`;
  `sep==""` → reject. `handle=handle_part.strip()` then regex-validate;
  `message=clean_text(message).strip()`; empty → reject; clamp via clean_text.
- **SC-P4 (accept w/ condition)**: M-2 holds structurally (handle_command
  reached only when room==mgmt_room and chat_for_room is None; mgmt room never
  carries uk.half-shot.bridge) — no bridged→create-chat path. Residual named:
  a compromised Synapse can forge an @jkali mgmt event → SC-P5 caps bound it.
- **SC-P5 (P2) persist caps**: SQLite `startchat_log(ts REAL, handle_hash
  TEXT)` (prune like `txns`). Enforce ≤3/3600s AND ≤10/86400s GLOBALLY across
  all handles (not per-handle — prevents fan-out); 60s dedup PER-handle by
  handle_hash. In-memory would reset on launchd restart — must be SQLite.
  At-cap attacker gain (state in plan): ≤10/day new outbound chats from the
  user's real iMessage identity to attacker-chosen handles w/ attacker first
  content — bounded; the persistent counter holds the bound.
- **SC-P6 (P3) echo**: wrap the SC-6 success reply in clean_text() before
  send_text (EMAIL_RE admits bidi/zero-width; clean_text strips them). Hub
  already safe (sanitize/textContent).
- **SC-P7 (accept)**: client-validate + daemon-authoritative is correct;
  confirm modal built with el()/textContent; have client validHandle also
  reject control/bidi so confirm-equals-send. Flip imessage canStartChat true
  only in SC.B.
- **SC-P8 (accept)**: do NOT add a ledger call for the create-chat first
  message (chat_id doesn't exist pre-send; surfaces once via normal poll).

Revised acceptance: SC.A adds — "status foo" and "start-chatx ..." → zero
exec; a `--message@x.com`-style handle → rejected (leading-dash guard); rate
counter survives a daemon restart (persisted rows); log has no handle/message.

### Execution outcome (2026-08-26)
Built by pilotfish:security-executor (Opus): daemon `start-chat` command
(cmd_start_chat, startchat_gate, startchat_log table, engine_create_chat) +
hub two-field Directory form (canStartChat=true, validHandle rejects
control/bidi/leading-dash, confirm modal, sendCmd C-1 path). Verified by Fable:
- Negative controls ALL zero-engine/zero-row: invalid handle, --message@ email
  (leading-dash guard), no-`|`-separator, portal room, ghost sender, and
  `status foo` (SC-P1 exact-match) — 6/6, startchat_log stayed 0, invalid
  handles replied "Invalid number or email."
- Rate cap unit-tested against the real startchat_gate: 4th/hour → deny,
  11th/day → deny, per-handle 60s dedup → deny, fresh-under-caps → allow.
- Log hygiene: no handle/message text in logs (sha8 + counts only).
- Reply-accuracy bug found + fixed by Opus: reply was unconditional; now
  reflects engine result ("Started chat…" vs "Couldn't start that chat…");
  failed attempts still consume the budget (anti-retry).
- py_compile + node --check pass; 0 HTML sinks; hub CSP byte-identical;
  canStartChat true for iMessage.
Caveat: create-chat to the user's OWN Apple ID (the only M-15-allowed test
target) is nondeterministic because that chat already exists — the happy path
for a genuinely NEW contact (engine returns 0, creates chat + first message)
is exercised by the user, not automatable under M-15. Browser-visual (the
two-field form + confirm modal) is user-checklist (Chrome ext unavailable).
