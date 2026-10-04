# F0 propagation baseline — evaluation, 2026-10-03

Operator account only (`@jkali:localhost`, iMessage handle in `imessage/daemon.json`).
No teammate account, conversation or setting was touched.

## What was measured

`tests/integration/probe_propagation.py` sends one canary iMessage from the
operator to their own handle and times each hop. Two legs, run back to back
after the fixes below were deployed to the local daemons.

| Hop | Leg | Measured | Budget |
|---|---|---|---|
| App → local Synapse accepts the event | matrix | 0.05 s | — |
| Local Synapse → iMessage daemon → Messages.app engine accepts | matrix | 3.03 s | 30 s |
| Local Synapse → uplink → master mirror (stamped `com.jkali.hops`) | matrix | 0.1 s + 0.1 s | 90 s |
| Messages.app (CLI send) → daemon poll → local Synapse | native | 1.89 s after a 12 s CLI launch | 60 s |
| Native-sent message renders as **sent** (`@imessagebot` + `com.jkali.from_me: true`) | native | yes | must |
| Native echo → uplink → master mirror, `from_me` preserved | native | 0.53 s | 90 s |

Both legs: all hops within budget. The 12 s on the native leg is the CLI
process start, not Beepa.

## What was found before the fixes

1. **Uplink delivery was blocked for five days by one deleted message.**
   `pending_events` held 66 events for the mirror of one Google Messages
   conversation. The oldest was a redacted `m.room.message` (empty content).
   The master refused it with 4xx on every pass; `deliver_pending` is ordered
   and re-raised, so the stage backed off (≈50–60 s) and the 65 events behind
   it never moved. The health line only said `delivery failed (HTTPError)`.
   This is the "master lags by minutes" report.
2. **iMessage daemon health read "144 refused sends" with zero failed sends.**
   Management-room commands (`status`), timestamp-correction state events and
   non-owner events ended in the outbound journal as
   `refused/ignored_or_invalid`. The six real sends (2026-09-04) were all
   `confirmed`; no real send had been attempted since.
3. **Only six iMessage chats are mapped** because the CLI's normal inbox for
   this Messages instance contains exactly six chats. Not a discovery bug.
4. **Console startup synced ~1000 rooms in full (≈20 s, 12 MB) on every load
   and every 20 s poll.** Fixed earlier the same day with incremental sync
   plus an IndexedDB baseline (≈20 ms per poll).

## Fixes shipped

| Fix | Where | Test |
|---|---|---|
| Retire redacted/hollow events; record a definitive master 4xx on one event instead of aborting the stage | `agents/uplink/durable_sync.py` `deliver_pending` | `uplink_durable_sync.test.py::test_hollow_or_refused_event_never_blocks_the_queue` |
| `com.jkali.hops` stamp on every mirrored message (local, uplink, master times) | `agents/uplink/uplink.py` `_forward_message` | `uplink_superseded.test.py` (hops present, inbound copy replaced) |
| Non-send events filed as `ignored/not_a_send`, counted apart from refusals | `imessage/daemon.py` `process_outbound_event` | `imessage_durability.test.py::test_non_send_events_are_filed_as_ignored_not_refused` |
| Live probe with budgets | `tests/integration/probe_propagation.py` | the run above |

After restarting the uplink the queue went 66 → 14 within twenty seconds and
to the single retired redacted row within two minutes.

## Health surfaces added the same evening

- Manager console: per-teammate uplink health from `com.jkali.uplink_health`
  on the teammate's space ("synced 12s ago · 0 queued · 2 refused", red dot
  when stale/disconnected), and "mirror lag" in the conversation header from
  the hop stamps. First write failed with HTTP 400 because one field was a
  float — Matrix event content is canonical JSON — now cast to int.
- Teammate app: the iMessage daemon's `status` reply now carries last send,
  last inbound and chats mapped; the Settings card renders it as one line
  ("Sends: 7 confirmed · last send 2m ago · last inbound 1m ago · 6 chats").
  The uplink line gained "messages the organization server refused".

## Unattended invite acceptance — security review (2026-10-03)

Design reviewed: the uplink accepts bridge-created room invites through a
Python port of `apps/user/invites.js`, so new conversations appear while the
browser is closed. Verdict: proceed with fixes. The consent claim held:
joining is membership only, a joined room resolves `private` in both
resolvers, no mirror is created, and the Direct auto-send gate cannot target
it. Required and adopted:

| # | Requirement | Where |
|---|---|---|
| 1 | Run the gate off `reconcile()`'s full sync, not the incremental tail (an invite left behind by a cap or error would otherwise be lost); verify `invite_state` carries `m.room.create` | uplink `invites` stage |
| 2 | Replace the browser's per-session 200 cap with a persisted rolling hour cap; bounded, expiring memo for hard 4xx; never memoize 429 | `state.db` tables |
| 3 | Daemon joins only after a server-side ack `com.jkali.autojoin_ack = {ok:true}` written by the browser on an affirmative confirm (migrated once for installs that already confirmed); read fail-closed | browser + uplink |
| 4 | Log counts and room hashes only; never room names (contact names, the operator's own number) or sender mxids (ghosts embed phone numbers) | uplink |
| 5 | Recorded decision: a re-invited portal with a stale `share`/`direct` override is mirrored again, honoring the prior explicit choice; joining a source space can make shared-but-sourceless rooms mirror | documented + tested |
| 6 | JS/Python conformance harness for the invite gate, including Discord's child space name, wired into `tests/run.py` | `tests/conformance/invites_conformance.py` |
| 7 | The claim that a bridge session secret can never land in a portal rests on `isBotDmMgmt()`'s full-state check, not on join ordering; stale comment fixed; Discord portal marker verified | `apps/user` docs |

## Not yet done (F0 remainder)

- Repeat the probe on a schedule and keep a history, so a regression shows as
  a trend rather than a complaint.
- Teammates' machines (David, Elliot) still run the pre-fix uplink. Not
  touched per instruction.
