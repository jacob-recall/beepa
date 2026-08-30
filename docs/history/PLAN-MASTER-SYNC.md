> ARCHIVED (2026-08-30): historical planning doc, superseded — kept for reference only.

# PLAN-MASTER-SYNC — Master ↔ User Distributed Sync

Design spec. Status: **COMPLETE — V1, V1.5, V2, and unified contacts (§12
phases 1–5) are all implemented and verified** (2026-08-27; build finished
on branch `feat/master-sync`). Supersedes nothing; extends the existing
single-user hub stack described in PLAN.md / PLAN-HUB.md. See
PLAN-MASTER-SYNC-IMPL.md for the phase-by-phase implementation record and
the per-directory `CLAUDE.md` files (`shared/`, `apps/user/`, `apps/master/`,
`agents/uplink/`, `master/`, `tests/`) for what was actually built.

---

## 1. Purpose & goal

A team of 3–4 people each manage many conversations across many messaging
networks. We want one person (the **manager**) to see and hold in their head
every pending message across the whole team, by having a centralized read view
of everyone's conversations — with each teammate's explicit, per-conversation
consent, and without ever taking away a teammate's control of their own
accounts or their own sending.

This spec defines the relationship between the **user app** (each teammate's
own instance) and the **master app** (the manager's centralized view): what
each side does, how they connect, and how data is stored and kept consistent.

### Success criteria (v1)

- A teammate can mark conversations to share — individually, per-network
  ("all iMessage"), or globally ("Share All") — and see, at a glance, exactly
  what the manager can currently see.
- The manager, from an always-on central instance, can read every shared
  conversation from every teammate, in one unified UI, sorted by recency,
  searchable, live-updating as teammates come online.
- Nothing a teammate has not shared ever leaves their machine.
- The manager can never send to the outside world and never holds any
  teammate's account credentials or session tokens.
- A teammate can revoke any share; the manager's copy is then deleted.

---

## 2. Scope

**In scope (v1):**
- One-way replication of *selected* conversations from each user up to the master.
- The master owns a **durable copy** of shared conversations.
- A **read-only** master UI.
- Layered per-conversation / per-source / global consent.

**Deferred (named here so the design leaves room for them):**
- **v2 — Proposal channel** (master → user): the manager composes a proposed
  message or a reusable "class of message"; it syncs down; the teammate
  approves/edits and **sends it themselves** from their own account. The master
  never sends externally.
- **v1.5 — Media re-upload** (v1 shows media as placeholder labels).
- **v1.5 — Ghost-user attribution** on the master (v1 attributes via event
  metadata rendered by the shared renderer).
- **Final feature — Unified contacts:** a contact-profile object that unifies
  one person across platforms (their iMessage + X + … under one profile, with
  all their conversations grouped), becoming the unit of sharing and of future
  routing decisions ("where do we send this person's next message"). Built last
  (see §12); lowest priority but foundational for later management features.
- **Distribution build** — de-Docker / native binaries / installer (PLAN, later).

**Non-goals:**
- The master is never a sending authority for external networks.
- No peer-to-peer connectivity between teammates.
- No Matrix E2E encryption of mirror rooms in v1 (see §9).

---

## 3. Architecture

### 3.1 Mechanism — "mirror-up" over an outbound Matrix client

Each teammate runs their existing local stack (bridges + local Synapse +
iMessage daemon + web UI). For each conversation the teammate shares, a new
background component — the **uplink agent** — **copies that conversation's
messages up** into a room on the **master homeserver**, acting as an ordinary
authenticated Matrix client over HTTPS. All connections are **outbound from the
teammate to the master**.

Rejected alternatives:
- **Matrix federation** (each side a peer homeserver): requires the teammate's
  laptop to be reachable *inbound*, which NAT/sleep defeats. Breaks
  outbound-only hub-and-spoke.
- **Custom non-Matrix protocol:** discards rooms/events/Matrix-client UI and
  forces rebuilding the master's read UI from scratch, breaking the shared-code
  goal.

### 3.2 Topology — hub-and-spoke, always-on master

```
 Teammate A machine                         Master (always-on, stable host)
 ┌──────────────────────────────┐          ┌─────────────────────────────────┐
 │ bridges → LOCAL Synapse       │          │  MASTER Synapse (durable copy)  │
 │ iMessage daemon               │  HTTPS   │   space:Alice  space:Bob  …     │
 │ user web UI                   │ outbound │     mirror rooms (copies)       │
 │ UPLINK AGENT ─────────────────┼─────────▶│   @manager reads (read-only)    │
 └──────────────────────────────┘ (client) │  master web UI (read-only app)  │
 Teammate B machine — same ───────────────▶ └─────────────────────────────────┘
```

The master is always on, at a stable hostname with a valid TLS cert. Each
teammate's uplink makes only outbound connections, so no teammate needs to be
reachable from outside; NAT/firewalls are irrelevant. "Significant online
overlap" among teammates simply widens sync windows — it is not required.

### 3.3 Structural symmetry with the user app

The user app groups conversations by **source** (WhatsApp / iMessage / …). The
master app groups them by **user** (Alice / Bob / …), with a source badge per
row. The master app is structurally the user app with the top grouping swapped
`source → user`, which is why the shared UI core (see §10) makes the master app
largely a re-skin.

---

## 4. Consent model — layered, most-specific-wins

Three levels; each conversation's effective shared-state resolves from the most
specific level that is set:

| Level | States | Default |
|---|---|---|
| Per-conversation | `share` · `private` · *inherit* | inherit |
| Per-source ("all iMessage", "all LinkedIn"…) | `share-all` · `private-all` · *inherit* | inherit |
| Global ("Share All") | `share-all` · `private` | private |

**Resolution for a conversation:**
1. If its per-conversation override is set → use it.
2. Else if its source policy is set → use it.
3. Else if global is `share-all` → shared.
4. Else → **private** (safe default).

This covers every requested case:
- *Share all my iMessage* → source[iMessage] = `share-all`.
- *Global Share All* → global = `share-all`.
- *Share everything except one thread* → global `share-all` + that conversation `private`.
- *All iMessage but not LinkedIn* → source[iMessage] `share-all`, source[LinkedIn] `private-all`.
- *Default private, just these 3 chats* → flip 3 rows to `share`.

### 4.1 Share-All is a standing policy

A `share-all` at global or source level is a **standing rule that also covers
conversations that arrive later** (a new iMessage thread auto-shares while
"all iMessage" is on). This is the point of Share-All. Because it is
trust-sensitive, two guards apply (§4.2).

### 4.2 Trust guards

1. **Consent summary panel** — the teammate can open, at any time, a truthful
   summary: *"The manager can currently see: all iMessage, 3 LinkedIn chats,
   nothing else."*
2. **Auto-share visibility** — when a standing policy auto-shares a
   newly-arrived conversation, it is surfaced in that panel and is
   one-click excludable, so nothing silently becomes visible without the
   teammate being able to notice and reverse it.

---

## 5. User-app functionality (what to add)

### 5.1 Contacts / conversations surface with search + share controls
- Reuse existing search (Home/per-source search already built).
- **Per-row share toggle** (tri-state: share / private / inherit).
- **Per-source "Share all <source>" switch** on each source's view.
- **Global "Share All" switch** at the top level.
- Each row shows its *effective* state **and why**: "shared (all iMessage)" vs
  "shared (explicit)" vs "private (excluded)" vs "private".
- **Consent summary panel** (§4.2).

### 5.2 Share-state storage (reuse existing account-data pattern)
- Global + per-source policy: one user account-data event
  `com.jkali.share_policy`, e.g.
  `{ "global": "share-all"|"private", "sources": { "imessage": "share-all"|"private-all"|"inherit", … } }`.
- Per-conversation override: room account-data `com.jkali.share_override`
  = `"share"`|`"private"` (absent = inherit).
- No new storage system; same mechanism already used for `m.lowpriority` and
  self-identities.

### 5.3 Enrollment
- One-time: the teammate's instance stores a credential for its master account
  (`@alice:master`) — homeserver URL + access token — issued by the manager
  at onboarding. Stored locally, mode 600. v1 uses a manually-provisioned
  token; a smoother enrollment-code exchange is v1.5.

### 5.4 The uplink agent (the main new component)
A headless background service in the teammate's local stack (sibling to the
iMessage daemon; must run with the browser closed). Responsibilities:

1. **Resolve** each conversation's effective shared-state from the three
   consent levels (§4).
2. **Reconcile** on any change:
   - newly-effective-shared room → create its mirror room on the master,
     invite `@manager` (read-only), backfill history, begin tailing;
   - newly-effective-not-shared room → **delete** its master mirror room;
   - re-evaluate new conversations as they arrive against standing policy.
3. **Tail** each shared room via `/sync` on the **local** homeserver; forward
   new messages, edits, and redactions to the corresponding mirror room on the
   **master**. (Reactions best-effort / v1.5.)
4. **Track watermarks & idempotency** (§8.2) so reconnects resume exactly
   without gaps or duplicates.

---

## 6. Master-side functionality

### 6.1 Master homeserver (always-on)
- One Matrix homeserver = the durable copy. Registration disabled; only the
  TLS'd client-server API exposed.

### 6.2 Provisioning
- The manager creates one account per teammate (`@alice:master`, …) and issues
  each an enrollment credential. Each token is **scoped to that teammate** —
  Alice's token can only write Alice's rooms.

### 6.3 Data organization & access control
- Each teammate's mirror rooms live under a **per-user space** (`space:Alice`).
- `@manager` is auto-invited (by the uplink) into every mirror room at
  **read-only** power level (see §8.3). Manager can read, never send (v1).

### 6.4 Master web app
- A read-only Matrix client against the master homeserver, built from `shared/`:
  - cross-user unified recent feed (reuse Home feed),
  - per-user spaces (reuse per-source view, grouped by user),
  - search across everyone (reuse search),
  - **no composer at all** — read-only is absent code, not a hidden button.
  - each row shows whose account + which platform.

---

## 7. Connection mechanics

- **Transport:** HTTPS (Matrix client-server API), outbound teammate → master.
  Master has a stable hostname + valid TLS cert (Let's Encrypt).
- **Auth:** each uplink authenticates as its own scoped master account token.
- **Sync loop (eventual consistency):** while online, the uplink (1) `/sync`s
  the **local** homeserver for new events in shared rooms, (2) sends them to the
  mirror rooms on the **master**, (3) advances the per-room watermark only after
  the master confirms receipt. Offline → buffer locally, resume from watermark
  on reconnect.
- **Direction:** v1 is strictly one-way (data up). v2 proposals arrive by the
  uplink also `/sync`ing the master (as its own account) for proposal events and
  surfacing them locally — still teammate-initiated outbound, preserving the
  outbound-only property.

---

## 8. Data model & storage

### 8.1 Where data lives
- **Teammate side:** local Synapse DB holds primary conversations (from
  bridges) — unchanged. Share policy in account-data. Uplink watermarks +
  event-map in a small local SQLite file (mode 600).
- **Master side:** the master Synapse DB holds all mirror rooms + events — the
  durable aggregate. (Media store used from v1.5.)

### 8.2 Mirror-room content & idempotency
- **Mirror room:** created per shared conversation; named with the
  conversation's display name; tagged `com.jkali.source: "<source>"` at
  creation (so the master app shows the platform badge); added to `space:<User>`.
- **Mirrored events:** the uplink posts `m.room.message` to the mirror room,
  copying `body` + `msgtype` and preserving metadata the shared renderer reads:
  - `com.jkali.from_me` (bool) — for right/left alignment,
  - `com.jkali.origin_ts` (original message time) — **the master app sorts and
    displays by this**, not `origin_server_ts` (a normal client cannot backdate
    server timestamps, so historical backfill would otherwise cluster at
    sync-time),
  - original sender display name (attribution).
- **Media (v1):** posted as its msgtype with a placeholder body ("Photo",
  "Video", …) and `com.jkali.media_placeholder: true`; the existing whitelist
  renderer already shows a static media label and never the filename. v1.5
  re-uploads media to the master's media store and includes a real `mxc`.
- **Idempotency:** local SQLite tables —
  `mirror_rooms(local_room_id, master_room_id, source, last_synced_pos)` and
  `event_map(local_event_id, master_event_id)`. New local events past
  `last_synced_pos` are posted, mapped, then the position advances on master
  `200 OK`. On restart, `event_map` prevents re-posting. (Same proven approach
  as the iMessage daemon's set-based txn/event map.)
- **Edits/redactions:** propagated via `event_map` (post an edit/redaction
  targeting the mapped master event).

### 8.3 Read-only enforcement (defense in depth)
- Mirror-room power levels: `@alice` = 100 (uplink must write), `events_default`
  = 50, `@manager` = 0 → manager cannot send.
- Plus: the master app ships **no composer** (build-time separation).

### 8.4 Backfill
- On first share of a conversation, backfill the last N messages (default 500,
  configurable) in order, then tail.

---

## 9. Security model

- The master holds **copies** — sensitive, but not the keys to the kingdom.
  **No bridge sessions, no account credentials, no external send-capability ever
  exist on the master.** A master compromise leaks readable message history
  only; it cannot send as anyone or steal accounts.
- Protections: TLS in transit; **full-disk encryption at rest** on the master;
  per-user scoped tokens; registration disabled; only the CS API exposed;
  rate limiting; standard secrets hygiene (600 / 700).
- **No Matrix E2EE on mirror rooms in v1** — it complicates both the mirror and
  the manager's read path; deferred. v1 relies on TLS + at-rest encryption +
  access control.
- **Revocation:** when a conversation's effective state flips to not-shared (via
  any consent level), the uplink **deletes** the master mirror room — revoke
  removes the durable copy, not just future updates.

### Homeserver flexibility (no lock-in)
Both sides talk only via the standard Matrix client-server API, so the master
homeserver can later be swapped for a lighter single-binary homeserver without
touching application code. Docker is retained for development; the distribution
build is deferred (see §12).

---

## 10. Code organization (single source of truth)

```
messaging-hub/
  shared/    ui core (renderer, convo row, chat view, search), matrix client,
             sync primitives, message/consent model     ← single source of truth
  apps/
    user/    + contacts list, layered share controls, consent panel,
             composer, per-source pages
    master/  + per-user grouping, cross-user feed; NO composer (read-only build)
  agents/
    imessage/  (existing)
    uplink/    NEW — the mirror-up daemon
  bridges/   user-stack compose  +  a separate master-stack compose (always-on box)
```

- Design/UI changes are made once in `shared/` and inherited by both apps.
- The "few exceptions" are the app-specific views under `apps/user/` and
  `apps/master/`.
- Build-time separation is the belt-and-suspenders trust boundary; the real
  authorization boundary is the sync/consent layer plus master-side power levels.

---

## 11. Error handling & edge cases

- **Offline catch-up:** watermark resume; nothing advances until the master
  confirms receipt.
- **No write conflicts:** one-way; mirror rooms are written only by the
  teammate's uplink, manager is read-only → no conflict resolution needed.
- **Ordering:** events forwarded in local stream order; the master app orders by
  `com.jkali.origin_ts`.
- **Duplicates:** prevented by `event_map`.
- **Master unreachable:** uplink buffers locally and retries with backoff;
  watermark not advanced until delivered.
- **Revocation race:** delete mirror room is idempotent; if a forward is
  in-flight during revoke, the room delete supersedes it.

---

## 12. Phasing (maps to the roadmap)

1. **User-app completion** — contacts list + search + layered share controls +
   consent panel + share-state account-data. (No master yet; flags just get
   stored and shown.)
2. **Uplink + Master v1** — master homeserver + provisioning + uplink agent
   (resolve/reconcile/backfill/tail/revoke) + read-only master app. End-to-end
   one-way sync working.
3. **Validation** — onboard 1–2 teammates white-glove on the Docker build;
   learn the real install/consent friction.
4. **v2** — proposal channel (propose → approve → send), media re-upload,
   ghost-user attribution, smoother enrollment.
5. **Unified contacts (final feature)** — a contact-profile object unifying one
   person across platforms; multiple conversations grouped under one profile;
   the profile becomes the unit of sharing and of future routing decisions.
6. **Distribution** — de-Docker / native binaries / installer / lean homeserver.

This overnight build targets phases 1–5 (V1 → V1.5 → V2 → unified contacts) in
that order; distribution (phase 6) is explicitly out of scope for this run.

---

## 13. Testing strategy

- **Unit:** consent resolver (all precedence combinations); uplink
  watermark/idempotency; event-map dedup.
- **Integration (Docker stack, two homeservers):**
  - share a conversation → mirror room appears on master with correct history,
    order, alignment, source badge;
  - new local message → appears on master;
  - offline → send → online → catch-up with no gaps/dupes;
  - "Share all iMessage" → all current + a newly-arrived iMessage room mirror up;
    per-conversation `private` exception stays out;
  - unshare (each level) → master copy deleted;
  - manager attempts to send → rejected (power level) and impossible (no composer).

---

## 14. Open decisions — confirmed defaults

Confirmed with the user during design:
- One-way data plane; master owns durable copy. ✔
- v1 read-only; proposals are v2. ✔
- Always-on master; hub-and-spoke; outbound-only. ✔
- Layered consent (per-conversation + per-source + global Share-All),
  most-specific-wins; Share-All is a standing policy with trust guards. ✔
- Attribution via metadata in v1 (ghosts later). ✔
- Media placeholder in v1 (re-upload v1.5). ✔
- Revocation deletes the master copy. ✔
- No E2EE in v1. ✔
- Uplink runs as a local headless daemon. ✔
- Docker retained for development; no homeserver lock-in. ✔
