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
| Screenshots | ✅ adaptive: once a minute while the app in front keeps changing, once every 3 minutes while it stays the same (both set in the dashboard); **Screenshot now** on demand; gallery with range-select + delete |
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

### Screenshots: frequency, "Screenshot now", storage

- **Adaptive frequency.** One screenshot a minute while the program in front keeps changing (someone flipping between
  Edge and Photoshop), slowing to one every 3 minutes while the same program stays in front (a long Photoshop session).
  Nothing is captured while the user is idle. Both intervals are set on the **Screenshots** page (15 s to 5 min / 1 to
  15 min); PCs pick the change up within about a minute, no rebuild or reinstall.
- **Screenshot now.** On the Day viewer, an employee's report and the Employees list: asks that PC for a live screenshot
  and shows it in seconds, so you can tell "the feature is working" from "they just switch apps faster than the
  interval". Agents check in about every 10 seconds; if the PC is off, asleep or on an older agent you get a clear
  "no answer" after 90 seconds. The shot is saved like any other.
- **Nothing is lost while the server or storage is down.** Activity is queued on the PC and resent, and so are
  screenshots: the agent keeps them in `ProgramData\Cyril\data\shots-pending` (capped at ~300 MB / 7 days) and uploads
  them, with their original capture times, as soon as the server accepts them again. *Install agents* shows how many are
  waiting.
- **Old agents.** An agent built before a feature existed can't use it. *Install agents* marks it "old agent" and
  **Screenshot now** says so immediately instead of waiting; update it with `Start-ScheduledTask 'Cyril Updater'`
  (elevated) on that PC after publishing a new build.
- **Storage check.** *Install agents* shows whether the server can actually save screenshots (it writes, reads back and
  deletes a test image). If screenshots fail with a 502 the reason and the fix are shown there and on the machine's row.
  Common causes: **`CYRIL_S3_ACCESS_KEY` containing `/`, a space or a comma** (it is the storage login id and must be
  letters and digits only, e.g. `cyrilstorage`; the secret key may contain anything), the two containers reading different
  `.env` values, or SeaweedFS having no room to allocate volumes. After editing `server/.env` run
  `docker compose up -d --force-recreate`.

### Deleting an employee

**Employees -> Delete** (or on the employee's report) permanently removes everything recorded about that login:
activity, alerts, screenshots **including the image files in storage**, the display name, and the status of PCs nobody
else uses. It shows the counts first and asks you to type the login. For a returned rental: run the uninstall
one-liner on the PC, then delete the employee. If the PC is still reporting it will reappear, so the dialog warns when
it reported in the last 10 minutes. If a screenshot file can't be removed its row is kept and you are told, so
nothing is orphaned; run Delete again to retry.

### How rules treat browsers

Rules are tried in priority order, first match wins, with one exception for browsers. When the website is known, a rule
about the **browser** ("Edge = neutral") never decides: the site is judged by its own domain/title rules, and a site with
no rule is *Uncategorized* (it appears under **Needs a label** on the Rules page). The browser's rule is only the
fallback when the address can't be read. In that case the window-title rules go first, so the **Private/Incognito**
alert still fires even if you have a blanket browser rule. Rules for programs other than a browser are unaffected.

### Names, websites and the web report

Reports use plain names: **YouTube, Claude, Freepik, PowerShell, Photoshop**, never `youtube.com` or `Photoshop.exe`.
Time in a browser is counted under the **website** (so it is "YouTube", not "Edge"); when the address can't be read it
falls back to the browser's name. Unknown domains/programs are tidied automatically (`app.freepik.com` -> Freepik,
`Some_Tool.exe` -> Some Tool); common brands are in `server/cyril_server/names.py`. Rules still match the raw
app/domain, so nothing about productivity labels changes.

The **Web activity** page is the detailed report: every page with its **full URL** and title, per-site totals, search,
and CSV download. Private/incognito windows are not in it (their address is not recorded).

On Windows the agent reads the address of the foreground Chromium browser (Edge, Chrome, Brave, Opera, Vivaldi)
straight from the browser window through UI Automation, so **no browser extension is needed** (the extension remains an
optional fallback). Each agent also reports its own health, shown per machine on **Install agents**: whether web
addresses are readable and whether screenshots are getting through, with the last error.

### Reporting interval ("Group by")

The agent always samples every few seconds; **Group by** only changes how that data is *reported*. Pick **1, 5, 10,
15 or 30 minutes** (or **Exact**, the default) in the bar at the top of Overview, Employees, an employee's report and
the Day viewer. Time is cut into blocks aligned to the local clock (:00/:15/:30/:45 for 15 minutes) and each block is
credited to the app or site used **most** within it; a block that is mostly idle shows as idle. It is one server-side
setting, so every page and every admin sees the same granularity, and it applies everywhere: timeline, activity log,
productivity %, top apps, per-day totals and exports. An employee's report has a **Time slots** table and a **Download
slot report (CSV)** with one row per block (app, category, productivity, share of the block, what else ran in it).
Totals of tracked time are preserved; what changes is attribution (a 15-minute block that was 8 min Photoshop and 7 min
YouTube counts fully as Photoshop). API: `GET/PUT /api/settings` (`report_interval`), or `?slot=N` on a single request.

### Updating the server

```bash
git pull origin main
cd server && docker compose up -d --build
```
The dashboard shows `build xxxxxxxx` next to **Sign out**; it should match `curl http://<server>:8000/api/health`.
If the menu or pages look old after an update, your browser is showing a cached copy: hard-refresh once
(Ctrl+Shift+R). Newer builds tell browsers to revalidate, so this only happens once, when moving from an old build.

### Windows rollout (one PowerShell command, no dev tools on the target machine)

Python/PyInstaller is only needed **once**, on a machine IT uses to build the agent:

```powershell
git clone <this repo>; cd cyril
.\windows\build.ps1      # writes server\dist\cyril.exe + cyril-extension.zip + manifest.json
cd server; docker compose up -d --build
```

Then, on each target machine, open PowerShell **as administrator** and paste the one-liner from the dashboard's
**Install agents** page (it has the right server address and agent key filled in, plus a Copy button):

```powershell
& ([scriptblock]::Create((irm 'https://cyril.icestudios.in/install.ps1?key=<agent key>')))
```

It checks the server and the download, installs, starts the agent, then **waits for the machine's first report and
tells you the result**. If nothing arrives (or the agent can't see the desktop) it prints why: which session the
process runs in, the task result and the tail of the agent log. The machine also appears on the Install page.
Running it again updates in place (a failed update leaves the old agent running).
Add `-User "Priya"` to report under a name instead of the Windows login. Set `CYRIL_PUBLIC_URL` on the server if it sits behind a proxy and the detected address is wrong.

**Remove it:** same elevated PowerShell, `& ([scriptblock]::Create((irm 'https://cyril.icestudios.in/uninstall.ps1')))`.
It lists each thing it removes (scheduled task, process, `Program Files\Cyril`, `ProgramData\Cyril`, local spool
files; and it restores Incognito if an older installer had disabled it). History already on the server is untouched.

Run the agent directly on Linux/macOS (Linux also needs `xprop` and `xprintidle`):
`cd agent && pip install -r requirements.txt && CYRIL_SERVER=http://<office-server>:8000 CYRIL_AGENT_KEY=... python -m cyril_agent`.
Load `browser-extension/` as an unpacked extension (or deploy by Chrome/Edge policy) to capture URLs.

The installer puts the agent at `Program Files\Cyril\cyril.exe` and stores the server URL/agent key in
`ProgramData\Cyril\config.json` (only administrators can change it; the agent's own log and spool live in
`ProgramData\Cyril\data`). It registers two scheduled tasks:
- **Cyril Agent** runs `cyril.exe` **inside the logged-in user's session**: at every logon, and re-launched every
  5 minutes if it was closed. It has to run there: a service or boot-time `SYSTEM` task lives in Windows session 0,
  which has no desktop, so it cannot see windows, screenshots or input. Running as the user also means reports carry
  the real Windows login rather than `PCNAME$`.
- **Cyril Updater** runs as `SYSTEM` daily at about 03:00 (random delay up to 30 min) **and 5 minutes after every start**,
  on battery too, retrying 3 times 10 minutes apart if it fails, and re-runs the installer; nothing happens unless a new
  build is published to the server. It never wakes a sleeping PC: one that was off or asleep at 03:00 catches up when it
  next starts or resumes, so a new `windows\build.ps1` build reaches every PC within about a day.

It's not hidden: `cyril.exe` shows in Task Manager and both tasks show in `Get-ScheduledTask` under their real names.
Trade-off to know about: because the agent runs as the employee, an employee *can* end the process and can read
the agent key from the config file. Ending it is undone within 5 minutes by the re-launch; only administrators can
disable or delete the tasks. The key only allows submitting reports, not reading anything.

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
3. Private windows get **quicker screenshots** than normal ones (first shot after ~10 s, then every minute even if the window doesn't change).
4. Cyril does **not** disable Incognito: staff can use it (e.g. to open a site without leaving their normal session),
   it is simply monitored through 1-3. Earlier installer versions did disable it by browser policy; re-running the
   installer (or uninstalling) reverts that.

## What we deliberately didn't build

Keystroke logging was dropped per request. We also didn't build **stealth/anti-detection**: hiding the
process from Task Manager/Activity Monitor, faking its name, or using kernel-level/driver tricks to
conceal it from the OS's own process-inspection APIs. This holds even with disclosed, signed-off consent
(e.g. a TOS naming the agent and its persistence/auto-update behavior) — consent changes whether monitoring
is disclosed, not what a concealment *technique* is reusable for elsewhere. Legitimate, disclosed monitoring
software (this one included) runs as a visible, clearly-named background service — what it hardens is
*tamper resistance* (admin-only install/removal, auto-relaunch if closed, auto-updating without a popup), not *invisibility to the OS's own inspection tools*. Practically,
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
