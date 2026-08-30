# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Build & Test

See `tests/CLAUDE.md` for how to run the unit tests and integration harness.

## Architecture Overview

See `docs/ARCHITECTURE.md` for the verified system architecture and data flow.

## Conventions & Patterns

See `docs/SYSTEM-DESIGN.md` for project conventions and design patterns.

## Master-sync architecture

The `feat/master-sync` build (PLAN-MASTER-SYNC.md / PLAN-MASTER-SYNC-IMPL.md)
adds a manager's centralized, read-only view across teammates' conversations
on top of the single-user hub. Status: **V1, V1.5, V2, and unified contacts
are complete and verified** — see the per-directory files below for what
lives where, security invariants, and how to run/test each piece:

- `shared/CLAUDE.md` — the single-source-of-truth ES-module core: DOM
  helpers + the render whitelist/from_me anti-spoof gate (`ui/render.js`),
  the guarded send path (`ui/chat.js`), the Matrix transport, and the
  consent (`model/consent.js`) + unified-contacts (`model/contacts.js`)
  models. Both apps below import from here; `shared/` never imports from
  either app.
- `apps/user/CLAUDE.md` — the teammate app: share controls, the consent
  summary panel, the contacts UI, and the proposal inbox, all built on
  `shared/`. `sendConvoMessage` (in `shared/ui/chat.js`) is the only path
  that sends into a conversation; the only other message writes are
  `sendCmd`/`sendSecretToMgmt` into verified bridge-management rooms.
- `apps/master/CLAUDE.md` — the manager's read-only console: no composer,
  no send call anywhere except one narrow, allowlisted proposal-write path.
  Deliberately avoids importing most of `shared/ui/` so the absence of a
  send path is *absent code*, not a hidden button.
- `agents/uplink/CLAUDE.md` — the outbound-only mirror-up daemon on each
  teammate's machine: resolves consent, mirrors shared conversations up,
  pulls proposals down into a dedicated local room, tracks watermark +
  event-map for exactly-once delivery. Its `consent.py` is a byte-parity
  Python port of `shared/model/consent.js` — the two must never drift.
- `master/CLAUDE.md` — the always-on master Synapse stack (separate
  compose project `matrix-master`, its own ports), provisioning, and the
  v1.5 enrollment-code flow.
- `tests/CLAUDE.md` — 19 unit tests, all wired into tests/run.sh (consent
  parity, uplink reconcile logic, and more), and the 12-scenario
  integration harness that drives two real homeservers end to end.

**Data flow:** each teammate's local Synapse (bridges + iMessage daemon)
stays the source of truth for their own conversations. A teammate's layered
consent (per-conversation > contact-profile > per-source > global,
most-specific-wins, default private) decides which conversations the
uplink mirrors, as an ordinary outbound Matrix client, into per-teammate
rooms on the always-on master homeserver. The master is a **copy**, never a
credential or sending capability — the manager reads it through
`apps/master/`, which cannot send, and may only leave a proposal that the
teammate reviews and sends themselves from their own account
(`apps/user/`'s proposal inbox, via the same guarded local send path).

**Security model, in one line per layer:** render whitelist + anti-spoof
from_me gate (shared UI) → consent resolver as the authorization boundary,
enforced identically in JS and Python (shared model + uplink) → mirror-room
power levels pinning the manager to read-only, set at room creation
(uplink) → no composer / no send code at all in the master app (build-time
separation) → secrets and state files at 600, master stack isolated from
the live `matrix-wa` hub on separate ports and volumes.
