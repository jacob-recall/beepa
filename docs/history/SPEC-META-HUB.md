> ARCHIVED (2026-08-30): historical planning doc, superseded — kept for reference only.

# HANDOFF SPEC: add Instagram as a hub SOURCE (meta bridge)

**Audience:** the agent that owns `hub/site/app.js` (Home feed / Directory / Connections).
**Author:** the meta-bridge agent (owns bridge infra; does NOT edit `hub/site/app.js`).
**Beads:** `pm_mng-ebn` (handoff). **Depends on:** the meta bridge being live
(`@instagrambot:localhost`, space `Instagram`).

The mautrix-meta Instagram bridge is deployed and registered with Synapse
exactly like the WhatsApp bridge: bot **`@instagrambot:localhost`**, appservice
namespaces `^@instagrambot:localhost$` / `^@instagram_.*:localhost$`, and it
creates a Matrix **space named `Instagram`** for the user's DM portals. Nothing
below adds a backend, port, secret, or CSP change — it is one `SOURCES` entry
plus a login-flow note. All existing hub security invariants (Trusted Types /
no HTML-string sinks, `sanitizeLine` on bridged strings, `ROOMID_RE ∩
/joined_rooms ∩ known-source-space` for navigation, `sendCmd` mgmt-room check,
D-3 read-path isolation, PLAN-HOME H-1..H-7 / HF-1..HF-9) apply unchanged.

## 1. Add one entry to the code-owned `SOURCES` array
Alongside the existing `whatsapp` / `imessage` entries:

```js
{ id: 'instagram', label: 'Instagram', kind: 'source',
  botMxid: '@instagrambot:localhost', spaceName: 'Instagram',
  canStartChat: true, icon: '📷',
  blurb: 'Bridge your Instagram DMs; log in with your browser session and chats appear in Element.' },
```

- `spaceName: 'Instagram'` MUST match the space the bridge creates (badge/feed
  membership is derived from source-space membership only — HF-4/HF-6, never
  from bridged content).
- `icon: '📷'` renders via the same CSS-pill / `textContent` path as the other
  sources (HF-7: no `<img>`, no `data:`/remote URL — CSP stays byte-identical).
  Pick a distinct color if the badge uses per-source colors.
- `canStartChat: true` enrolls Instagram in the Directory + start-chat surface
  (D-1) — the "reuse Directory" contact requirement. No Instagram-specific UI.

That single entry is enough for: the per-source sidebar list, the Directory
cross-source filter, and the Home unified feed (PLAN-HOME) to include Instagram
— **provided** those features enumerate `SOURCES` generically. If any of them
hardcodes the WhatsApp+iMessage spaces (e.g. a literal two-space list in the
Home feed's space-gathering or in `SOURCES_BY_SPACE`), add the `Instagram` space
there too, derived from this `SOURCES` entry (never from bridged data).

## 2. Instagram login flow differs from WhatsApp (NOT a QR)
The WhatsApp Connections card shows a QR. **Instagram login is cookie-based:**
the command is `login instagram`, after which the user pastes the **"Copy as
cURL" of a graphql XHR** from instagram.com (or the cookies
`sessionid, csrftoken, mid, ig_did, ds_user_id` as a JSON object) as the next
message. So the Instagram Connections card needs a **text/paste flow**, not a QR:
1. Button → `sendCmd('login instagram')` (through the existing `sendCmd` mgmt-room
   guard — C-1, unchanged).
2. A single-line/paste input whose value is sent as the next message to the bot.

### SECURITY (load-bearing — from the bridge security review, finding M1)
The pasted cURL/cookies are a **bearer credential** and become the *content of a
Matrix message*, so they rest in Synapse's events DB and the mgmt room. The card
MUST, on a successful login reply:
- **redact** (delete) the message that carried the cURL/cookies
  (`PUT /_matrix/client/v3/rooms/{roomId}/redact/{eventId}/{txn}`), and
- never echo the pasted secret back into the console/log, never `sanitizeLine`-
  render it into a visible transcript, never build a URL from it.
If auto-redaction isn't wired, the card must tell the user to delete that message
manually. Treat the paste value like a password field (no logging, no history).

## 3. Command surface (optional, mirrors WhatsApp's COMMAND_GROUPS)
If you expose per-source management buttons, meta's useful commands are:
`help`, `version`, `login instagram`, `list-logins`, `logout <login ID>`,
`sync` (bridge-native contact/DM sync — gentle; do NOT add any bulk
follower/following enumeration button, per the anti-ban posture). Portal-scoped
commands stay excluded as with WhatsApp.

## 4. Acceptance for this handoff
- Instagram appears as a sidebar source and in the Directory; a row/badge is
  labeled by the `Instagram` space, not by bridged content.
- Home feed includes Instagram conversations once the space has portals, merged
  and sorted with the others (bridged content still passes `sanitizeLine`, never
  reaches the command/console path — HF-1).
- The Instagram Connections flow sends `login instagram` + a pasted session and
  **redacts the secret-bearing message** on success (M1).
- `node --check hub/site/app.js` passes; 0 HTML-string sinks; hub CSP
  byte-identical; navigation still `ROOMID_RE ∩ /joined_rooms ∩ source-space`.

## What the bridge side already guarantees (so you don't have to)
- `@instagrambot:localhost` resolves (Synapse appservice registered), single-
  entry admin permissions (`@jkali:localhost`), provisioning disabled.
- Space `Instagram` is created by the bridge; portals are its children.
- Backfill is OFF and contact sync is gentle (anti-ban) — no hub action needed.

## 5. Cookie collection — why it MUST be user-paste, not auto-harvest
Verified 2026-08-26 with Claude-in-Chrome against instagram.com:
- Instagram's `sessionid` (and `ds_user_id`) are **HttpOnly** — invisible to page
  JS (`document.cookie` returns only `csrftoken`, `wd`).
- Claude-in-Chrome **blocks reading sensitive cookie keys** (`sessionid`/
  `csrftoken` return `[BLOCKED: Sensitive key]`) — an intentional anti-
  credential-exfiltration guard. The agent also must not, by policy, read/relay
  a session credential.
Therefore the "open Instagram → cookies collected" UX cannot mean *the agent or
the page silently harvests the cookie*. The safe equivalent (credential stays in
the user's hands, never through the agent): a **user-paste** flow. This is the
closest safe analogue to the gmessages quick-sign-in given Instagram's cookie
model (gmessages uses QR/Google sign-in and never needs a pasted session).

## 6. Instagram Connections card — connect flow (to implement in app.js)
Mirror the gmessages Connections card, but with a paste step:
1. **Connect** button → `sendCmd('login instagram')` (through the existing
   `sendCmd` mgmt-room guard, C-1 — unchanged).
2. Show a short guide + a **paste field** (treat like a password input:
   `type=password` or a textarea that is never echoed/logged): "In the Instagram
   tab, DevTools → Network → filter `graphql` → right-click a request → Copy as
   cURL → paste here." (Optionally the hub opens instagram.com in a new tab for
   them; the user logs in there themselves.)
3. On submit, send the pasted value as the next message to the mgmt room (same
   `sendCmd` path). Do NOT `sanitizeLine`-render it into the console, do NOT
   `logConsole` it, do NOT build a URL from it.
4. Poll the bot's replies (existing mgmt read path) for success/failure text;
   show a connected/failed pill.
5. **On success, REDACT the paste message** (security review M1). Capture the
   `event_id` returned by the send
   (`PUT /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txn}` → `event_id`),
   then `PUT /_matrix/client/v3/rooms/{roomId}/redact/{event_id}/{txn2}` with the
   user's token. If redaction fails, tell the user to delete the message
   manually. Never keep the paste value in JS state after submit.

## 7. Contact import (verified bridge-side; no hub work beyond the SOURCES entry)
- `sync_direct_chat_list: true` + `personal_filtering_spaces: true` are set: the
  user's Instagram **DM conversations** sync into the `Instagram` space as
  portals with ghost users, automatically, after login. The Directory picks them
  up via `canStartChat: true`.
- **Starting a NEW chat** is by username search, not a contact picker: the meta
  bridge resolves an Instagram identifier via its start-chat/search command.
  Wire the Directory's start-chat for `instagram` to that command (same
  capability gate the other sources use). There is intentionally **no follower/
  following import** (Instagram has no address-book contact list, and enumerating
  followers is an anti-ban no-go).
- No backfill of old message history (anti-ban); conversations bridge new
  messages from login onward.
