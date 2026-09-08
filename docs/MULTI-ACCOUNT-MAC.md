# Two iMessage accounts on one Mac

Status: implementation branch; native two-session delivery and Docker runtime
acceptance are still required. Preparing an installation is not evidence of a
working account connection.

## Scope and topology

Run a native iMessage daemon and uplink inside each account's macOS GUI session.
Each daemon has its own local Synapse/PostgreSQL hub and browser origin. Both
uplinks enroll as different teammates into one external Beepa master. Apple
credentials remain in macOS; the master receives only explicitly shared chats.

```
Elliot's Messages → Elliot's native bridge → Elliot's local hub → Elliot's uplink ─┐
                                                                                ├→ external master
David's Messages  → David's native bridge  → David's local hub  → David's uplink ──┘
```

This change adds a fresh-install `imessage` profile. It starts only PostgreSQL,
Synapse, the user interface, native iMessage, and uplink. It does not install the
five other network bridges, browser-cookie helpers or Contacts importer. The
existing full-network installation path stays available. Converting an existing
installation, multiple native accounts inside ONE macOS user, or putting both
bridges into one Synapse are outside this change.

The master already accepts separate teammate enrollments. There is no master
protocol, consent, external-send retry, or iMessage ghost-namespace migration.
Using `:localhost` identifiers on two isolated hubs does not require federation.

## Changes

- `multi_account.py prepare`: persists profile, owner UID, assigned slot, ports,
  unique Compose project, retained paths and optional Messages handle. Refuses
  slot/profile/account changes and adoption of existing full hubs. Generates a
  private `.env`; repeated preparation preserves credentials and installation ID.
- `docker-compose.imessage.yml`: minimal stack with separate project volumes and
  configurable host ports. Container-internal Synapse remains on port 8008.
- `hub/render-hub.sh`: reads retained endpoints during installation and update;
  renders matching native daemon/appservice ports and registers only iMessage
  for this profile. Each installation has independent appservice tokens.
- `beepa_update.py`: selects the profile's Compose file and mounts that
  installation's state. Content-addressed generated entry point and CSP let the
  browser reach only its own hub. Preparing an update does not overwrite the
  artifacts mounted by the current release. Existing default apps use their
  existing entry point.
- `shared/installation.js` and user UI: iMessage-only connection card; enrollment
  uses the CLI, while status and Disconnect remain in the UI. No scanning another
  user's helper ports or sending browser requests to foreign cookie helpers.
- `setup.sh`: rerunning it on a prepared iMessage profile invokes that profile's
  installer. Native jobs use their owning GUI domain; identical launchd labels
  across different UIDs are valid. Update/agent operations reject the wrong UID.
- `multi_account.py link`: reads the installation's local credentials as data,
  validates local identity/URL, asks for the one-time code without shell history,
  and invokes the existing enrollment implementation. No tokens in documentation.

## Proposed assignment for this Mac

| Setting | Elliot | David |
|---|---|---|
| macOS short name | `elliot-msgr-spoof` | `david-msgr-spoof` |
| Slot | 1 | 2 |
| Local Synapse | `127.0.0.1:8108` | `127.0.0.1:8208` |
| Local app | `127.0.0.1:8111` | `127.0.0.1:8211` |
| Native appservice | `127.0.0.1:29351` | `127.0.0.1:29352` |
| Project | `beepa-imessage-<UID>-1` | `beepa-imessage-<UID>-2` |
| Master teammate | separate Elliot identity | separate David identity |

Ports are machine-wide, even when Docker engines are separate. Slots are an
operator assignment, not automatic cross-user discovery. An occupied port fails
installation; it never chooses the next port or adopts a foreign listener.

Each user needs their own checkout under their home. The manifest and state are
user-owned, with data in `~/Library/Application Support/Beepa/<install_id>` and
logs under `~/Library/Logs/Beepa/<install_id>`. Never copy a prepared checkout,
`.env`, tokens, database or `.beepa-install.json` to the other user.

## Runtime prerequisite: resolve before claiming live support

Docker is not installed on this Mac at initial inspection. Docker Desktop uses
per-user sockets and a per-user backend. Merely running setup under the second
login does not establish a usable engine for that account. The installer expects
`docker info` to work in each owning session and the selected runtime to mount
that user's state and route `host.docker.internal` back to the Mac's native
appservice. It does not install/share Docker sockets or change their permissions.

Use independently working per-user local engines only after verifying concurrent
operation, host callback routing and bind mounts. Do not assume two Docker Desktop
instances work concurrently. If using a single service-owned engine instead, a
separate privileged provisioning/deployment arrangement is required; this branch
does not implement it. Do not solve this by making another user's socket or
message data world-readable. Remote Docker contexts are not supported by these
local bind mounts and loopback endpoints.

Docker reference: https://docs.docker.com/desktop/setup/install/mac-permission-requirements/

## Prepare and install

In EACH user's own Terminal, clone the implementation branch into a fresh path:

```sh
mkdir -p ~/Projects
cd ~/Projects
git clone --branch feat/macos-multi-account-imessage https://github.com/jacob-recall/beepa.git
cd beepa
```

For Elliot (use slot 2 instead in David's session):

```sh
python3 multi_account.py prepare --slot 1
python3 multi_account.py status
```

Preparation creates private configuration only. It neither starts services nor
reads Messages. Once Messages is signed into the intended Apple account, provide
the phone/email selected for that account; this is an identity setting, not an
Apple sign-in mechanism:

```sh
python3 multi_account.py prepare --slot 1 --handle 'YOUR_IMESSAGE_PHONE_OR_EMAIL'
python3 multi_account.py install
```

Install requires a working local Docker runtime. It downloads hash-pinned host
dependencies and the repository's signature-verified iMessage CLI, provisions the
local Matrix account, installs user launch agents and the Beepa launcher. No
master is enrolled and no conversation is shared by installation.

In each macOS session grant the installed CLI Full Disk Access and Accessibility,
and approve Messages Automation when prompted. Use the exact retained CLI path
reported in that user's `.beepa-install.json`. Sign into Messages and finish Apple
prompts as the account owner. No Apple password should be sent to the agent.

Keep both GUI sessions logged in with Fast User Switching and keep the Mac awake
for live operation. After reboot, log into both sessions and start their runtime.
Do not run native agents with sudo or as system LaunchDaemons.

## External master and enrollment

Choose the master host and configure its private Tailscale gateway using
[Master operations](MASTER-OPERATIONS.md) and the existing master setup. No master
hostname is inferred from a local account or a Git remote. Create one teammate
and one code for each account, then in the corresponding local checkout run:

```sh
python3 multi_account.py link 'https://YOUR-MASTER-ENROLLMENT-URL'
```

Enter the one-time code at the hidden prompt. In the local UI, explicitly select
Private, Share or Direct per conversation. Start validation with Share. Direct
remains subject to all existing local authority and freshness checks.

## Operations

Use the manifest-aware command rather than raw `docker compose`, which defaults
to the legacy full stack:

```sh
python3 install_config.py compose -- ps
python3 install_config.py compose -- logs --tail 100 synapse
python3 install_config.py compose -- stop
python3 install_config.py compose -- up -d
./setup.sh
```

`stop` above affects containers only. Stop native agents from the owning session:

```sh
launchctl bootout "gui/$(id -u)/org.beepa.imessage-daemon"
launchctl bootout "gui/$(id -u)/org.beepa.uplink"
```

Normal update uses `./update.sh`, then `./update.sh --apply`, retaining profile,
ports, credentials and native executable. Never run `down -v` against live data.

## Acceptance gates and limits

Automated checks use synthetic identities, private temporary state and fake
signing keys. They cover disjoint projects/ports, state mounts, browser routing,
owner/slot rejection, port collisions, matching real-rendered appservice tokens
and endpoints, and update artifact retention. They cannot establish native
permission grants or real provider delivery.

Before enabling unattended Direct sends, verify all of the following on this Mac:

1. Both engines/stacks remain running after switching users. Each Synapse can
   reach only its own configured appservice; both private state mounts work.
2. Receive an identifiable message on each Apple account with its GUI foregrounded
   and switched away. Confirm the correct local hub and master teammate receive it.
3. With explicit permission for test messages, send through each account to a
   designated test recipient with each foreground/background combination, then
   locked/unlocked. Include a contact present in both accounts.
4. Confirm the external sender identity and that a reply in account A never leaves
   account B. Test Share review and, separately, Direct after explicit selection.
5. Restart and update A while B remains live. Reboot, log into both sessions and
   verify catch-up without duplicate external sends.

The pinned sender uses Messages UI/accessibility automation. Concurrent login
sessions do NOT establish that both background sessions can send reliably. If
that gate fails, this profile cannot promise simultaneous unattended sending;
use separate Macs or a separately evaluated backend. This change does not
silently switch the foreground user, retry ambiguous sends, or bypass macOS.

Apple session reference: https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPMultipleUsers/Concepts/FastUserSwitching.html
Native engine: https://github.com/beeper/platform-imessage

## Information/actions needed from the owner

- Confirm the external master host/URL, or identify where it should be deployed.
- Confirm which Messages phone/email belongs to each local macOS user.
- Complete Messages sign-in and macOS permission dialogs in both GUI sessions.
- Establish the local container runtime for both sessions (see prerequisite).
- Provide separate enrollment codes locally when the master is ready.
- Name a test recipient and authorize the native send acceptance tests.

No passwords or message contents are needed for code review or preparation.

## Validation recorded on this branch

The complete discovered unit suite and all 114,235 consent-conformance vectors
passed on macOS with Python 3.9.6 and Node 22.14.0. The suite required loopback
socket access. Standalone Docker Compose 2.35.1 also validated both effective
projects, including published ports, private state mounts and distinct database
volumes; this check is included in CI. Container-running integration and real iMessage tests have not run because
there is no configured Docker runtime or enrolled master on this device.

Localhost binding is not authentication between macOS users. The existing app
serves a bootstrap token on loopback, so this profile assumes trusted local users
and processes. Separate ports prevent accidental account routing; they do not
provide a hostile-user security boundary. Adding authenticated local bootstrap
would be a separate change before using this on an untrusted multi-user Mac.
