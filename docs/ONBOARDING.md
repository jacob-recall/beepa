# Onboarding a New Device — Happy Path

Stand up a new teammate's local hub and connect it to the always-on **master**
over Tailscale, so their shared conversations mirror up to the manager's console.

> **This org's master:** `https://jacobs-macbook-pro.jaguar-pirate.ts.net`
> (Synapse on `:443`, enrollment on `:8443`). Update this line if the master moves.

**Time:** ~15 minutes, most of it the Docker image pull + the one-time iMessage
build. **You'll need two things from the manager:** an *enrollment URL* and a
*one-time code*.

---

## 0. Prerequisites

- **A Mac.** The connect helpers, the uplink, and iMessage all run as macOS
  launchd services — this doesn't run on Windows/Linux.
- **On the org's Tailscale tailnet.** The device reaches the master over
  Tailscale; if it's not on the tailnet, nothing will connect.
- **Docker Desktop.** If it's not installed, the installer offers to install it
  via Homebrew; if it *is* installed, just make sure it's running.
- **Git** (to clone) — or download the repo ZIP from GitHub.

---

## 1. Manager: issue the invite (on the master machine)

The manager provisions the new person's master account and mints a one-time
code. Pick a short name for the device/person (e.g. `alice`):

```bash
# on the master machine, in the repo:
TEAMMATES='jkali alice' master/provision.sh      # add existing names + the new one
python3 master/enroll.py mint alice              # prints the one-time code
```

Hand the teammate:
- **Enrollment URL:** `https://jacobs-macbook-pro.jaguar-pirate.ts.net:8443`
- **Code:** the string `mint` printed.

> Codes expire in **~10 minutes** and are single-use. Mint it right when the new
> device is about to enroll (Step 4). The master machine must stay running.

---

## 2. New device: join the tailnet and confirm the master is reachable

Install Tailscale, sign into the **same tailnet**, then verify:

```bash
tailscale up
curl https://jacobs-macbook-pro.jaguar-pirate.ts.net/health
```

A `200` / `{}` means you can reach the master. If it hangs or errors, you're not
on the tailnet yet — fix that before continuing.

---

## 3. New device: get the repo and make sure Docker is running

```bash
git clone https://github.com/jacob-recall/beepa.git
cd beepa
```
*(No git access on that machine? Use the GitHub page's **Code → Download ZIP**,
unzip, and `cd` in.)*

Make sure Docker Desktop is running. If it's installed but the `docker` command
isn't found, launch it and add its bundled CLI to your PATH:

```bash
open -a Docker
# accept the license, wait for the menu-bar whale to say "running", then:
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
docker info        # should print engine info
```

---

## 4. New device: run the installer

```bash
./install.sh
```

It will, in order:
1. **Preflight** (Docker, macOS, free ports).
2. **Render the hub config** from the repo's tracked templates (fresh secrets).
3. **Bring up the stack** — pulls the images (~2 GB the first time) and starts
   Synapse + the five bridges + the local web server.
4. **Provision your local account** (`@jkali:localhost`) automatically.
5. **Load the one-click Connect helpers** (Instagram / LinkedIn / X / Google
   Messages) and the contacts importer.
6. **Download the iMessage CLI** — Beeper's prebuilt, Developer-ID-signed
   binary (pinned version, checksum + signature verified; no Xcode or Swift
   needed, takes seconds). Its stable signature means the macOS permission
   grants you make later stick permanently.
7. **Enroll to the master.** At the prompt:
   - **Master enrollment URL:** `https://jacobs-macbook-pro.jaguar-pirate.ts.net:8443`
   - **Enrollment code:** the one from Step 1.

   The uplink links and starts running — it mirrors your **shared** conversations
   up to the master over Tailscale.

---

## 5. New device: open the app and connect your accounts

Open the teammate app — **no password, it logs in automatically:**

```
http://127.0.0.1:8011/apps/user/index.html
```

Then, using the **Connect** buttons in the app (no terminal):
- **WhatsApp** — scan the QR with your phone.
- **Instagram / LinkedIn / X** — one click each; sign in on the tab that opens.
- **Google Messages** — one click, then tap the matching emoji on your phone.

Everything starts **private**. Each conversation has an explicit level —
**Share** (mirrors up; manager suggestions land in your Proposals inbox for
review), **Direct** (mirrors up AND the manager's drafts send as you without
review — enable only via its confirm dialog, per conversation or per source
via the bulk action), or **Private**. There is no inherit/Share-All standing
policy; use the per-source bulk action to set many at once. Shared
conversations mirror up within ~10s. Contact sharing is separate: per-source
switches plus per-contact overrides in Sharing settings.

---

## 6. iMessage (optional, Mac only)

The CLI is already installed. To turn iMessage on:
1. Set your `self_handle` (your iMessage phone/email) in `imessage/daemon.json`.
2. Re-run `./setup.sh` to load the daemon.
3. Grants (one-time each; the daemon opens the right Settings pane for you
   when one is missing):
   - **Receiving:** add `imessage/bin/imessage-cli` to **Full Disk Access**.
   - **Sending:** add `imessage/bin/imessage-cli` to **Accessibility**, and
     click **Allow** on the "control Messages" Automation prompt at first send.
   If you ever replace the CLI binary, remove (−) and re-add (+) its rows —
   toggling a stale row does not re-key it.

---

## 7. Verify it worked

- **On the new device:** the app opened without a login screen, the networks you
  connected show as connected, and the uplink is running
  (`launchctl list | grep com.jkali.uplink`).
- **On the manager's console** (`http://127.0.0.1:8011/apps/master/index.html`,
  also passwordless): within ~30s of sharing a conversation, it appears under the
  new person's name — mirrored over Tailscale.

You can watch the moment it crosses the tailnet in `agents/uplink/logs/uplink.log`
(look for requests to `…ts.net`).

---

## Common happy-path snags

| Symptom | Fix |
|---|---|
| `brew install ... there is already an App at '/Applications/Docker.app'` | Docker Desktop is installed but its CLI isn't on PATH. Run the `open -a Docker` + `export PATH=…` from Step 3, then re-run `./install.sh`. |
| Enrollment says the code is used/expired | Have the manager `python3 master/enroll.py mint <name>` again; codes last ~10 min. |
| `curl …ts.net/health` hangs | The device isn't on the tailnet (or the master machine is off). |
| An image fails to resolve (`not found`) | The repo pins images; run `git pull` to pick up any repin, then re-run. |
| App still shows a login screen | A stale browser session — hard-reload, or open a fresh tab. |

---

## What just happened (one paragraph)

Each device runs its **own** local hub (Synapse + bridges) and its own web app at
`127.0.0.1` — those are always local, per machine. The only thing that crosses
Tailscale is the **uplink**, an outbound-only daemon that mirrors the
conversations you explicitly share up to the central master. The manager reads
that master through a **read-only** console and can never send from it. Nothing
you don't share ever leaves your machine.
