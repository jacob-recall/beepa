# tests/live/ — opt-in LIVE send verification (NOT part of CI)

This directory is deliberately **separate** from `tests/run.sh` and
`tests/integration/`. Everything under `tests/unit/` and
`tests/integration/` is hermetic (synthetic homeservers, synthetic
contacts, no real bridge, no real message ever leaves the machine).
**`self_send_verify.py` is the opposite: it sends REAL messages through
REAL bridges to REAL accounts** — specifically, your own.

Nothing in this directory is invoked by `tests/run.sh`, by
`tests/integration/run.sh`, or by any CI/automated path. It only runs when
an operator runs it by hand, with explicit flags.

## What it does

`self_send_verify.py` drives the same management-room command path the
hub's Directory uses (`shared/ui/sources.js`'s `resolveMgmt`/`sendCmd`,
and for iMessage the guarded `start-chat` command from
`PLAN-IMSG-STARTCHAT.md`) to:

1. send a unique nonce message to a handle the operator explicitly names
   on the command line,
2. then poll that platform's portal room until the nonce round-trips back
   (proof the bridge actually delivered it), or time out.

It covers the three platforms where messaging yourself is possible:

- **iMessage** — `start-chat <handle> | <nonce>` in the verified iMessage
  management room (same mechanism as integration scenario 12's
  self-directed acceptance note; see `tests/integration/harness.py`'s
  `scenario_12_contact_share_and_propose` docstring).
- **WhatsApp** — `start-chat <handle>` in the WhatsApp bot DM, then a
  normal message into the resulting portal.
- **Google Messages** — same pattern as WhatsApp.

**LinkedIn / X / Instagram are NOT covered.** There is no "message
yourself" concept on those platforms — you cannot DM your own account. They
stay human-tested; see the checklist below.

## Guardrails (why this can't fire by accident)

- **Self-only, explicit handles required.** The operator must pass
  `--imessage`, `--whatsapp`, and/or `--gmessages` with an actual handle.
  With none given, it prints usage to stderr and exits **without touching
  the network at all**.
- **Explicit confirmation flag.** Even with handles given, it refuses to
  send unless `--i-am-sending-to-myself` is also passed. This is a
  deliberate, separate flag — there is no way to "accidentally" opt in by
  only supplying handles.
- **Handle validation.** Every handle must match the same strict E.164 (or
  email) shape the hub's own `validHandle()` requires
  (`shared/ui/sources.js`). Anything else is refused before any network
  call.
- **Obvious-fake smell test.** Handles that look like placeholder/movie
  numbers (all-same-digit, strictly sequential, the fictional 555-01xx
  range) are refused outright. This is a courtesy check on top of the
  confirmation flag, **not** a substitute for it — it cannot prove a
  number really is the operator's own, it can only catch the common
  "forgot to fill in a real number" mistake.
- **Per-run cap.** At most 3 platforms per invocation (there are only 3
  platforms this script knows about, so this is a hard ceiling, not a
  configurable escape hatch).
- **No surprising logging.** Only the operator's own explicitly-typed
  handle, the generated nonce, and pass/fail are printed — no message
  bodies beyond the nonce marker, no bridge-internal state, no tokens.
- **Management-room re-verification before every send**, mirroring the
  hub's own C-1 guard (`verifyMgmt`/`verifyImsgMgmt`): the resolved room is
  re-checked as a genuine bot-DM (or the iMessage marker room), never a
  portal, immediately before sending — so a stale or spoofed room id can't
  be used to send somewhere else.

## Running it

For an existing configured iMessage self-chat that is already Direct, verify
both paths without changing consent or authorizing a different master:

```sh
python3 tests/live/imessage_paths_verify.py --i-am-sending-to-myself
```

This sends one unique local message, waits for confirmed native acceptance and
an actual inbound self-ghost event, then repeats through a manager proposal.
The manager request must use HTTPS resolving to a Tailscale address. It stops
after a failure, never counts outgoing echoes as receiving, and checks that
the native executable's inode and hash stayed unchanged. `--paths local` or
`--paths master` selects one path; `--output` writes private diagnostic evidence.
It requires existing account credentials and a warmed-up, unsuspended Direct
binding. It does not create a portal, alter sharing or acknowledge suspension.

The September 4 release verification found and fixed a real late-inbound bug:
a self-message can arrive with a timestamp older than its outgoing echo,
leaving the chat marker unchanged. The bridge now periodically rescans tails
with bounded, fair scheduling. See the sanitized
[live verification record](../../docs/review-evidence/2026-09-04-live-validation.json).

Requires this install's own local hub credentials — the same
`LOCAL_HS_URL` / `LOCAL_USER` / `LOCAL_TOKEN` shape as
`agents/uplink/uplink.env.local` (see `agents/uplink/CLAUDE.md`), read from
that file by default or overridable with `--env-file` / matching
environment variables.

```bash
python3 tests/live/self_send_verify.py \
    --imessage +1YOURNUMBER \
    --whatsapp +1YOURNUMBER \
    --gmessages +1YOURNUMBER \
    --i-am-sending-to-myself
```

Only the platforms you pass a handle for are exercised. `--timeout` /
`--poll-interval` control how long it waits for the nonce to round-trip
(default: 60s / 2s). Exit code is non-zero if any requested platform
failed to confirm delivery.

`--help` never sends anything and never touches the network — safe to run
any time.

## Manual checklist — LinkedIn / X / Instagram

These bridges have no self-DM path, so verify them by hand instead:

- [ ] **LinkedIn**: from the hub's Directory, open an existing LinkedIn
      conversation with a real contact (or use `search`/`resolve-identifier`
      to confirm a known contact resolves), send a short test message, and
      confirm it appears as sent on linkedin.com in the same thread.
- [ ] **X (Twitter)**: same as above — send a short test DM from the hub to
      an existing X conversation, confirm it appears on x.com.
- [ ] **Instagram**: same as above — send a short test DM from the hub to
      an existing Instagram conversation, confirm it appears in the
      Instagram app/site.
- [ ] For all three: confirm the message you sent from the hub renders
      with the correct sender attribution (yours, not the bridge ghost) in
      the portal room, and that a reply from the other side arrives back
      in the same room.

These three are intentionally left out of any automated live-send tooling
— there is no "self" address to target, so any live automated test would
have to message a real third party, which this project will not do.
