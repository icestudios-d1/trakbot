# TrakBot

Self-hosted employee activity monitoring (an ActivTrak-style tool without the seat limit):
a lightweight **agent** on each company device reports the foreground app, window title and browser
URL plus idle state to a central **server**, which labels time as productive / unproductive / neutral,
raises alerts for prohibited activity, takes periodic screenshots, and serves a manager dashboard with
charts. No keystroke logging.

## Feature map vs. ActivTrak

| ActivTrak | TrakBot |
|---|---|
| App & website usage tracking | ✅ agent (Windows/macOS/Linux) + browser extension for URLs |
| Productivity labels (productive/unproductive/undefined) | ✅ rule engine (app / domain / title), re-labels history on edit |
| Idle/active time | ✅ idle detection (`--idle-after`) |
| Productivity %, top apps/sites, categories, hourly activity, trend chart | ✅ multi-page dashboard: Overview, Employees, Alerts, Screenshots, Rules |
| Per-employee drill-down / timeline | ✅ Employee page: pick start/end dates, trends, day-by-day table, CSV + print/PDF |
| Active vs passive usage (e.g. YouTube music behind Photoshop) | ✅ "Active vs background" page; needs browser extension v0.2+ (audible tabs) |
| Design-studio productivity rules | ✅ Photoshop/Illustrator/InDesign/After Effects/Figma/Blender… preset, uncategorised-app finder, rule tester |
| Alerts for risky activity | ✅ rules with `alert` flag (piracy, gambling, private browsing pre-seeded) |
| Screenshots | ✅ one per app per minute once it's held the foreground 60s+; gallery with range-select + delete |
| Windows rollout | ✅ one PowerShell command, no Python/git/npm needed on the target machine |
| Reports | ✅ in-app only, no email reports (by request) |
| Keystroke logging, workload/utilisation, teams/groups, SSO | ❌ not built (keylogging intentionally omitted) |

## Run

```bash
# server
pip install -r server/requirements.txt
export TRAKBOT_ADMIN_TOKEN=<long-random>  TRAKBOT_AGENT_KEY=<long-random>
cd server && uvicorn trakbot_server.app:app --host 0.0.0.0 --port 8000   # put behind HTTPS (nginx/caddy)
# open http://host:8000 -> sign-in page, paste the admin token

# agent, run directly (on each device; Linux also needs `xprop` and `xprintidle`)
cd agent && pip install -r requirements.txt
TRAKBOT_SERVER=https://trakbot.example.com TRAKBOT_AGENT_KEY=<key> python -m trakbot_agent
```
Load `browser-extension/` as an unpacked extension (or deploy by Chrome/Edge policy) to capture URLs.
Tests: `cd server && pytest`, `cd agent && pytest`.

### Windows rollout (one PowerShell command, no dev tools on the target machine)

Python/PyInstaller is only needed **once**, on a machine IT uses to build the agent:

```powershell
# one-time, on a Windows box with Python 3.11+ (IT's machine, not target employee machines)
git clone <this repo>; cd trakbot
.\windows\build.ps1
# copy dist\trakbot-agent.exe and dist\trakbot-extension.zip into the server's dist/ folder
#   (set TRAKBOT_DIST_DIR on the server, or just drop them in server/<cwd>/dist)
```

Then, on each target machine (elevated PowerShell, nothing but PowerShell itself required):

```powershell
$cfg = Invoke-RestMethod https://trakbot.example.com/install.ps1
& ([scriptblock]::Create($cfg)) -ServerUrl https://trakbot.example.com -AgentKey <agent key>
```

This installs the agent under `Program Files\TrakBot`, stores the server URL/API key in
`ProgramData\TrakBot\config.json` (ACL'd to SYSTEM/Administrators only), and registers a Scheduled Task
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
browser security boundary, not a TrakBot limitation, and we don't try to defeat it. Instead:
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

TrakBot records app name, window title, URL, idle state, and periodic screenshots. No keystroke logging.
Even with a clear employment-agreement notice and legal sign-off, many jurisdictions (GDPR/UK, several US
states, India's DPDP Act, etc.) also expect: a specific notice of what's collected and why, limited
retention (screenshots can be bulk-deleted by date range from the dashboard for this), restricted access to
the data (the admin token gates all of it), and sometimes works-council consent. Run it only on
company-owned devices, tell employees what is collected, and have counsel review the policy — you noted
legal has already cleared this and is updating the agreement to name TrakBot specifically, which is the
right order of operations.


### Dashboard pages

`/login` (admin token) · `/` Overview · `/employees` · `/employee?user=…` (date-range report) ·
`/overlap` (active vs background audio) · `/alerts` · `/screenshots` · `/rules`.

**Active vs background:** the agent normally only sees the foreground window, so music left playing in a
background tab would be invisible (or, in tools that credit the browser, look like heavy YouTube use).
The v0.2 browser extension reports which tabs are playing sound; the agent attaches those sites to each
event. *Active* = in front and not idle; *passive* = audible but not in front (or user idle). Productivity %
uses active time only. Reload the extension and update the agent to start capturing it; older history has no
background data. Desktop players (Spotify app) are only seen while in front.

**Existing installs:** the studio rule preset is added with **Rules → Add missing studio rules** (never edits
or removes your rules). Existing `youtube.com` rules stay as they are; edit them there if you want YouTube neutral.
