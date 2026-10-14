# Cyril

Self-hosted employee activity monitoring (an ActivTrak-style tool without the seat limit):
a lightweight **agent** on each company device reports the foreground app, window title and browser
URL plus idle state to a central **server**, which labels time as productive / unproductive / neutral,
raises alerts for prohibited activity, takes periodic screenshots, and serves a manager dashboard with
charts. No keystroke logging.

## Feature map vs. ActivTrak

| ActivTrak | Cyril |
|---|---|
| App & website usage tracking | ✅ agent (Windows/macOS/Linux) + browser extension for URLs |
| Productivity labels (productive/unproductive/undefined) | ✅ rule engine (app / domain / title), re-labels history on edit |
| Idle/active time | ✅ idle detection (`--idle-after`) |
| Productivity %, top apps/sites, categories, hourly activity, trend chart | ✅ multi-page dashboard: Overview, Employees, Alerts, Screenshots, Rules |
| Per-employee drill-down / timeline | ✅ Employee page: pick start/end dates, trends, day-by-day table, CSV + print/PDF |
| Day-by-day review | ✅ Day viewer: timeline of the day with clickable screenshots, activity log, filmstrip |
| Who is behind each login | ✅ Rename any employee login to the real person (shown everywhere; data stays keyed on the login) |
| Design-studio productivity rules | ✅ Photoshop/Illustrator/InDesign/After Effects/Figma/Blender… preset, uncategorised-app finder, rule tester |
| Alerts for risky activity | ✅ rules with `alert` flag (piracy, gambling, private browsing pre-seeded) |
| Screenshots | ✅ one per app every 3 min once it's held the foreground 60s+ (`--screenshot-every`); gallery with range-select + delete |
| Windows rollout | ✅ one PowerShell command, no Python/git/npm needed on the target machine |
| Reports | ✅ in-app only, no email reports (by request) |
| Keystroke logging, workload/utilisation, teams/groups, SSO | ❌ not built (keylogging intentionally omitted) |

## Architecture

Self-hosted on your own office server (Docker Compose), on purpose: if the server goes down, agents keep
sampling and spool everything locally (`agent/cyril_agent/core.py:Spool`), then flush the backlog as soon as
it's reachable again — no cloud dependency, no data lost to an outage.

| Piece | Where it runs |
|---|---|
| API + dashboard + installer scripts + self-update manifest | **FastAPI** (`server/cyril_server/`), in a Docker container |
| Events, rules, alerts, screenshot metadata, update log | **SQLite** (a file on a Docker volume) |
| Screenshot images | **SeaweedFS** (self-hosted, S3-compatible; replaces MinIO — see `server/docker-compose.yml`) |
| Agent (per device) | Python, built to a single `cyril.exe` via PyInstaller |

Screenshots never touch the app container's own disk: the agent posts them to the server, which stores them
in SeaweedFS (private — no port published to the host, reachable only from the `server` container) and keeps
just the object key in SQLite. The dashboard views images through the server (admin-token-authenticated
proxy endpoint), and a daily sweep (run it via cron/Task Scheduler hitting a maintenance script, or add an
APScheduler job) deletes screenshots older than retention.

## Deploy (office server, Docker)

```bash
cd server
cp .env.example .env            # fill in CYRIL_ADMIN_TOKEN, CYRIL_AGENT_KEY, CYRIL_S3_ACCESS_KEY/SECRET_KEY
docker compose up -d --build    # starts the server + bundled SeaweedFS
```

That's it — no cloud account, no external DNS required for a LAN-only deployment (point agents at the
office server's internal hostname/IP). Put it behind a reverse proxy with TLS (Caddy/nginx) if any device
reaches it off-LAN (VPN, remote staff).

Tests: `cd server && pytest` (SeaweedFS is faked in-process, no container needed to run them), `cd agent && pytest`.

### Agent updates (built once, applied automatically fleet-wide)

```powershell
# On IT's build machine only (Python/PyInstaller never touches employee machines):
.\windows\build.ps1 -Version 0.2.1
```

This writes `cyril.exe`, `cyril-extension.zip` and `manifest.json` (with a SHA-256 of the new build) into
`server/dist/`, which is bind-mounted straight into the running container — nothing to restart. Every
already-installed agent polls `/api/agent/version` hourly (`--update-check-every`, default 3600s); on seeing
a newer version it downloads the new build, verifies the checksum, and swaps itself in, restarting via the
same Scheduled Task that already auto-restarts it (see `agent/cyril_agent/update.py`). There's no popup —
same as how Chrome or any managed EDR patches itself — but every step is written to a local log
(`~/.cyril-update.log` by default) and reported to `/api/agent/update-log`, visible to admins at that
endpoint, so the rollout is fully auditable even though it isn't interruptive. A checksum mismatch aborts
the update rather than applying anything.

### Windows rollout (one PowerShell command, no dev tools on the target machine)

Python/PyInstaller is only needed **once**, on a machine IT uses to build the agent:

```powershell
git clone <this repo>; cd cyril
.\windows\build.ps1      # writes server\dist\cyril.exe + cyril-extension.zip + manifest.json
cd server; docker compose up -d --build
```

Then, on each target machine (elevated PowerShell, nothing but PowerShell itself required):

```powershell
$cfg = Invoke-RestMethod http://<office-server>:8000/install.ps1
& ([scriptblock]::Create($cfg)) -ServerUrl http://<office-server>:8000 -AgentKey <agent key>
```

Run the agent directly on Linux/macOS (Linux also needs `xprop` and `xprintidle`):
`cd agent && pip install -r requirements.txt && CYRIL_SERVER=http://<office-server>:8000 CYRIL_AGENT_KEY=... python -m cyril_agent`.
Load `browser-extension/` as an unpacked extension (or deploy by Chrome/Edge policy) to capture URLs.

The installer puts the agent at `Program Files\Cyril\cyril.exe`, stores the server URL/API key in
`ProgramData\Cyril\config.json` (ACL'd to SYSTEM/Administrators only), and registers a Scheduled Task named
"Cyril Agent" that runs it at boot as `SYSTEM` with auto-restart — so a non-admin employee can't stop or
uninstall it, but it's not hidden: it shows up in Task Manager as `cyril.exe` and in `Get-ScheduledTask`
under its real name, same as any other IT-managed agent (see "What we deliberately didn't build" below).
Uninstall with `uninstall.ps1` from the same server.

### How the agent knows which machine it's on

Each report carries a `device` ID that is **not** the hostname (hostnames get renamed, or duplicated across
cloned company images). It's a stable per-OS machine identifier read once at startup:
- **Windows:** the registry's `MachineGuid` (`HKLM\SOFTWARE\Microsoft\Cryptography`), set by Windows setup and
  constant for the life of that install.
- **macOS:** the hardware `IOPlatformUUID`.
- **Linux:** `/etc/machine-id`.
- Falls back to hostname only if none of those is readable.

The employee ("user") is a separate field — by default the OS login name, or set explicitly via
`config.json`/`--user` if IT wants to map it to an email or employee ID instead.

## Incognito / private browsing

Private-mode browser windows deliberately withhold the page URL from extensions and history — that's a
browser security boundary, not a Cyril limitation, and we don't try to defeat it. Instead:
1. The agent recognizes the window title pattern ("Incognito", "InPrivate", "Private Browsing") and logs it
   as a flagged **Private/Incognito Browsing** category/alert — time spent is tracked, the actual page title
   and URL are not (redacted before leaving the device).
2. **Screenshots are the real backstop.** They capture the screen itself, which private mode cannot hide,
   and fire automatically once a browser window's been open 60+ seconds.
3. `install.ps1` also sets the Chrome/Edge `IncognitoModeAvailability` enterprise policy to disabled by
   default — the simplest fix if you'd rather block incognito outright. Remove that block from the script
   if you want employees to be able to use it (then rely on 1+2 above for visibility into it).

## What we deliberately didn't build

Keystroke logging was dropped per request. We also didn't build **stealth/anti-detection**: hiding the
process from Task Manager/Activity Monitor, faking its name, or using kernel-level/driver tricks to
conceal it from the OS's own process-inspection APIs. This holds even with disclosed, signed-off consent
(e.g. a TOS naming the agent and its persistence/auto-update behavior) — consent changes whether monitoring
is disclosed, not what a concealment *technique* is reusable for elsewhere. Legitimate, disclosed monitoring
software (this one included) runs as a visible, clearly-named background service — what it hardens is
*tamper resistance* (running as SYSTEM so a standard user can't kill or remove it, auto-restarting if
killed, auto-updating without a popup), not *invisibility to the OS's own inspection tools*. Practically,
true process-hiding is also exactly what EDR/antivirus is built to flag and quarantine as malicious — it
would get `cyril.exe` killed on the machines it's meant to run on. Self-update (new in this version) is
silent in the sense of "no interruptive UI," never in the sense of "no record": every check and apply is
logged locally and reported to `/api/agent/update-log`.

## Scope & compliance

Cyril records app name, window title, URL, idle state, and periodic screenshots. No keystroke logging.
Even with a clear employment-agreement notice and legal sign-off, many jurisdictions (GDPR/UK, several US
states, India's DPDP Act, etc.) also expect: a specific notice of what's collected and why, limited
retention (screenshots can be bulk-deleted by date range from the dashboard for this), restricted access to
the data (the admin token gates all of it), and sometimes works-council consent. Run it only on
company-owned devices, tell employees what is collected, and have counsel review the policy — you noted
legal has already cleared this and is updating the agreement to name Cyril specifically, which is the
right order of operations.


### Dashboard pages

`/login` (admin token) · `/` Overview · `/employees` (rename logins) · `/employee?user=…` (date-range report) ·
`/day` (day viewer) · `/alerts` · `/screenshots` · `/rules`.

**Foreground only:** time is credited to the window in focus, so music or video left running in a background tab
while someone works in Photoshop counts as Photoshop, not as the player.

**Existing installs:** the studio rule preset is added with **Rules → Add missing studio rules** (never edits
or removes your rules). Existing `youtube.com` rules stay as they are; edit them there if you want YouTube neutral.
