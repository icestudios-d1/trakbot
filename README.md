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
| Productivity %, top apps/sites, categories, hourly activity, trend chart | ✅ dashboard + `/api/summary`, `/api/trend` |
| Per-employee drill-down / timeline | ✅ `/api/timeline` |
| Alerts for risky activity | ✅ rules with `alert` flag (piracy, gambling, private browsing pre-seeded) |
| Screenshots | ✅ one per app per minute once it's held the foreground 60s+; gallery with range-select + delete |
| Windows rollout | ✅ one PowerShell command, no Python/git/npm needed on the target machine |
| Reports | ✅ in-app only, no email reports (by request) |
| Keystroke logging, workload/utilisation, teams/groups, SSO | ❌ not built (keylogging intentionally omitted) |

## Architecture

| Piece | Where it runs |
|---|---|
| API + dashboard + installer scripts | **Cloudflare Worker** (`server/`, TypeScript + Hono) |
| Events, rules, alerts, screenshot metadata | **Cloudflare D1** (SQLite) |
| Screenshot images | **ImageKit** (private files, signed-URL access only; Worker holds the key) |
| Agent (per device) | Python, unchanged: samples the foreground window, reports to the Worker |

Screenshots never touch D1/Workers storage: the agent posts them to the Worker, which uploads to ImageKit as
*private* files and keeps only the file id in D1. The dashboard views images through the Worker (admin-authenticated,
60-second signed URL), and a daily Cron Trigger deletes screenshots older than `SCREENSHOT_RETENTION_DAYS` (default 90).

## Deploy

```bash
cd server && npm install
npx wrangler login
npx wrangler d1 create cyril                 # paste the printed database_id into wrangler.toml
npm run db:init                              # creates tables in the remote D1
# wrangler.toml: set IMAGEKIT_URL_ENDPOINT (ImageKit dashboard > Developer options) and DEFAULT_TZ_OFFSET_MIN
npx wrangler secret put CYRIL_ADMIN_TOKEN    # long random; what managers paste into the dashboard
npx wrangler secret put CYRIL_AGENT_KEY      # long random; goes on every device
npx wrangler secret put IMAGEKIT_PRIVATE_KEY # ImageKit dashboard > Developer options > Private key
npx wrangler deploy
```

Local dev: `npm run db:init:local && npx wrangler dev` (put the three secrets in `server/.dev.vars`, gitignored).
Tests: `cd server && npm test` (Workers runtime via Miniflare, ImageKit mocked), `cd agent && pytest`.

### Plan limits that matter (checked Oct 2026 against search results of vendor pages; re-check before relying on them)
- **ImageKit Free:** 3 GB DAM storage (hard cap, uploads blocked when full) and 20 GB/month delivery bandwidth
  (delivery stops when exhausted). Some aggregator sites still list 25 GB / 5 GB, so confirm on imagekit.io/plans.
- **Screenshot volume vs 3 GB:** at the default cadence (one ~60-150 KB JPEG per minute of active use) one employee
  produces roughly 30-70 MB per working day, i.e. **~2-4 GB over 3 months**. The free tier therefore holds about *one*
  full-time employee for 90 days. For a team, either lower the cadence (`--screenshot-every 600` = 1 per 10 min),
  shorten `SCREENSHOT_RETENTION_DAYS`, or move to a paid ImageKit plan.
- **Workers Free** allows 10 ms CPU per request; dashboard reports over many users/days can exceed that.
  Workers Paid ($5/mo) is recommended for real use. D1 Free has daily row read/write caps (enforced on
  the Free plan from 1 Sep 2026).
- **Agent installer hosting:** `cyril-agent.exe` is served from `server/public/dist` as a Worker static asset
  (25 MiB per-file cap). If your PyInstaller build is larger, put it in R2 and change the download URL in `install.ps1`.

### Windows rollout (one PowerShell command, no dev tools on the target machine)

Python/PyInstaller is only needed **once**, on a Windows machine IT uses to build the agent:

```powershell
git clone <this repo>; cd cyril
.\windows\build.ps1      # writes server\public\dist\cyril-agent.exe + cyril-extension.zip
cd server; npx wrangler deploy
```

Then, on each target machine (elevated PowerShell, nothing but PowerShell itself required):

```powershell
$cfg = Invoke-RestMethod https://cyril.<your-subdomain>.workers.dev/install.ps1
& ([scriptblock]::Create($cfg)) -ServerUrl https://cyril.<your-subdomain>.workers.dev -AgentKey <agent key>
```

Run the agent directly on Linux/macOS (Linux also needs `xprop` and `xprintidle`):
`cd agent && pip install -r requirements.txt && CYRIL_SERVER=https://... CYRIL_AGENT_KEY=... python -m cyril_agent`.
Load `browser-extension/` as an unpacked extension (or deploy by Chrome/Edge policy) to capture URLs.

The installer puts the agent under `Program Files\Cyril`, stores the server URL/API key in
`ProgramData\Cyril\config.json` (ACL'd to SYSTEM/Administrators only), and registers a Scheduled Task
that runs it at boot as `SYSTEM` with auto-restart — so a non-admin employee can't stop or uninstall it,
but it's not hidden: it shows up in Task Manager and installed-software listings under its real name, same
as any other IT-managed agent (see "What we deliberately didn't build" below). Uninstall with
`uninstall.ps1` from the same server.

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
process from Task Manager, faking its name, or blocking it from "installed apps." Legitimate, disclosed
monitoring software (this one included) runs as a visible, clearly-named background service — what it
hardens is *tamper resistance* (running as SYSTEM so a standard user can't kill or remove it), not
*invisibility to the person it's installed on*. Techniques that hide an agent from the OS's own inspection
tools are what separate disclosed workplace monitoring from spyware, regardless of employer authorization,
so that's out of scope here.

## Scope & compliance

Cyril records app name, window title, URL, idle state, and periodic screenshots. No keystroke logging.
Even with a clear employment-agreement notice and legal sign-off, many jurisdictions (GDPR/UK, several US
states, India's DPDP Act, etc.) also expect: a specific notice of what's collected and why, limited
retention (screenshots can be bulk-deleted by date range from the dashboard for this), restricted access to
the data (the admin token gates all of it), and sometimes works-council consent. Run it only on
company-owned devices, tell employees what is collected, and have counsel review the policy — you noted
legal has already cleared this and is updating the agreement to name Cyril specifically, which is the
right order of operations.
