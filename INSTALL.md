# Installing Beepa (teammate onboarding, steps 1-3)

One command:

```sh
./install.sh
```

Safe to re-run. It walks the three onboarding steps below in order, checks
what it can automate, and prints the exact manual command for anything it
can't finish for you.

## What you need first

- **Docker Desktop**, installed and running.
- **macOS**, if you want the iMessage bridge (every other network — WhatsApp,
  Google Messages, Instagram, LinkedIn, X — works on any OS Docker runs on).
- A local Matrix account on this stack (`@you:localhost`) — provisioned once
  by whoever set up your Postgres/`.env`. `install.sh` doesn't create this;
  it's a precondition, same as it is for `agents/uplink/link.sh` today.
- If you're joining a manager's org (step 3): a one-time **enrollment code**
  from your manager (`master/enroll.py mint <you>`, or the manager console's
  "add teammate" action). Optional — you can connect networks first and
  enroll later.

## Step 1 — stand up the local hub

`install.sh` calls `./setup.sh`, which runs:

```sh
docker compose --profile bridge --profile client up -d
```

This brings up: Synapse (your private homeserver, `127.0.0.1:8008`), the five
mautrix bridges (WhatsApp, Instagram, LinkedIn, X, Google Messages — no host
ports, compose-network only), Postgres, and the `views` static server for the
web apps (`127.0.0.1:8011`). Idempotent — `docker compose up -d` is a no-op if
already running.

## Step 2 — connect each network account

Same `setup.sh` call installs and loads two loopback helper services as
launchd agents, so each network's login is a **single click in the browser,
no terminal**:

| Helper | Port | Covers |
|---|---|---|
| `gmessages-connect` | 8020 | Google Messages |
| `session-connect` | 8021 | Instagram, LinkedIn, X |

Open the app and use the Connect buttons:

```
http://127.0.0.1:8011/apps/user/index.html
```

**WhatsApp is the one exception** — it's QR-based, not one-click: send
`login qr` to `@whatsappbot:localhost` (or use the app's Connections card),
then on your phone: WhatsApp → Settings → Linked devices → Link a device →
scan.

**iMessage** runs as its own launchd appservice daemon
(`imessage/daemon.py`, macOS only, `com.jkali.imessage-daemon`). The same
`setup.sh` call loads it **when its two prerequisites exist**, and otherwise
prints exactly which is missing and moves on:

- `imessage/daemon.json` — copy `imessage/daemon.json.example`, fill
  `as_token`/`hs_token` from `synapse/imessage-registration.yaml`, your
  `user_id`, `self_handle`, and the absolute `cli_path`; `chmod 600` it.
- `imessage/bin/imessage-cli` — the pinned Beeper platform-imessage CLI
  build (see README.md "iMessage bridge"; macOS Full Disk Access is granted
  to this binary specifically).

Re-run `setup.sh` after providing them and the daemon is loaded.

**Contacts (macOS)** — the same `setup.sh` call also loads
`com.jkali.contacts-import`, an hourly launchd job that reads your
Contacts.app address book (names, phone numbers, emails) into a local,
mode-600 store (`agents/contacts/contacts.db`). Two things to know:

- On its first run macOS shows a standard "**osascript** wants access to
  your Contacts" prompt. That prompt *is* the consent for reading the address
  book — click **Allow**. If you miss or deny it, turn it on under System
  Settings → Privacy & Security → Contacts → osascript, then run
  `python3 agents/contacts/import_macos.py` once by hand (a background job
  can't answer the prompt itself).
- **Nothing leaves your Mac by default.** Imported contacts are shared with
  your manager only for the sources you switch on in the app's contact-share
  panel (a separate opt-in from sharing conversations, default private);
  switching a source on backfills its existing contacts, switching it off
  removes them from the manager's view.

## Step 3 — enroll the uplink to the master

Optional, and separate from steps 1-2 — this is what lets your manager see
conversations you explicitly share. `install.sh` prompts for:

- the master's enrollment URL (e.g. `https://master.example`, or
  `http://127.0.0.1:8019` for a local test master)
- the one-time enrollment code your manager gave you

and then runs the real enrollment mechanism, unchanged:

```sh
agents/uplink/link.sh <enroll-url> <code>
```

which redeems the code (`agents/uplink/enroll_client.py` against
`master/enroll.py serve`), writes your scoped master credentials to
`agents/uplink/uplink.env.local` (mode 600, gitignored), and installs +
loads the `com.jkali.uplink` launchd daemon. If you skip this in
`install.sh` (no code yet), run the command above later — it's the same
script a manual enrollment uses.

If you're re-running `install.sh` and `agents/uplink/uplink.env.local`
already exists, step 3 is skipped (you're already enrolled). To re-enroll
against a different master, delete that file first.

**What actually leaves your machine:** nothing, until you explicitly share a
conversation in the app (per-conversation, per-contact, or Share-All). The
uplink only mirrors what consent allows, outbound, into your own
per-teammate room on the master. It cannot send anywhere on your behalf.

## Verifying you're set up

- `http://127.0.0.1:8011/apps/user/index.html` loads and shows your
  connected networks in the Connections card.
- If enrolled: ask your manager to confirm your rooms/space appear in the
  manager console (`apps/master`) once you've shared at least one
  conversation.
- Logs: `agents/uplink/logs/uplink.log`, `session-connect/logs/connect.log`,
  `gmessages-connect/logs/connect.log`, `agents/contacts/logs/import.log`
  (one `added=… updated=… soft_deleted=…` line per hourly import — counts
  only, never a name or number).

## Uninstall / stop

```sh
launchctl unload ~/Library/LaunchAgents/com.jkali.session-connect.plist
launchctl unload ~/Library/LaunchAgents/com.jkali.gmessages-connect.plist
launchctl unload ~/Library/LaunchAgents/com.jkali.uplink.plist
launchctl unload ~/Library/LaunchAgents/com.jkali.contacts-import.plist
docker compose down          # stops the stack; add -v ONLY if you want to
                              # delete all bridged history and sessions —
                              # see README.md "Read before deleting anything"
```
