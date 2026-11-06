# Cyril — Production Note

How the whole system is put together, how it gets from a `git push` to every employee machine, and how to run it.
Written from the code in this repository (branch state of November 2026). Where something depends on how *your* office is
set up (reverse proxy, firewall, backups) and the repository cannot know it, the text says **Confirm** so someone can
fill it in.

Repository: `icestudios-d1/trakbot` · Public address used in examples: `https://cyril.icestudios.in`

---

## 1. What runs where

```
 EMPLOYEE PCs (Windows / Mac)                         OFFICE SERVER (Docker Compose)
 ┌──────────────────────────┐   HTTPS (JSON)    ┌───────────────────────────────────────────┐
 │ cyril agent (per user    │ ───────────────▶  │ reverse proxy / tunnel (TLS)  [Confirm]   │
 │ session)                 │  /api/ingest      │        │                                  │
 │  • foreground app+title  │  /api/screenshot  │        ▼                                  │
 │  • browser URL           │  /api/agent/*     │ ┌─ server (FastAPI, port 8000) ─────────┐ │
 │  • idle state            │ ◀───────────────  │ │ API, dashboard pages, installers,     │ │
 │  • screenshots (JPEG)    │  poll: commands,  │ │ agent update manifest                 │ │
 │  • local spool on disk   │  intervals        │ │  SQLite  (volume cyril_db)            │ │
 └──────────────────────────┘                   │ │  ./dist  (agent builds, bind mount)   │ │
        ▲  installed by one-liner                │ └───────────┬───────────────────────────┘ │
        │  (served by the server)                │             │ S3 API (internal network)   │
        │                                        │ ┌───────────▼───────────────────────────┐ │
 ADMIN BROWSER ── HTTPS ─────────────────────▶   │ │ SeaweedFS (volume cyril_screenshots)  │ │
   dashboard (sign in with admin token)          │ │ screenshots + thumbnails, no port out │ │
                                                 │ └───────────────────────────────────────┘ │
                                                 └───────────────────────────────────────────┘

 GITHUB  (icestudios-d1/trakbot)
   push to main ─▶ Actions "agents" ─▶ builds Windows .exe + 2 Mac binaries ─▶ release "agent-latest"
   office server: update.ps1 ─▶ git pull + gh release download ─▶ server/dist ─▶ PCs update themselves
```

| Piece | Tech | Where | State kept in |
|---|---|---|---|
| Server (API, dashboard, installers, update manifest) | Python 3.12, FastAPI, Uvicorn | container `server`, port 8000 | SQLite file `/data/cyril.db` (volume `cyril_db`) |
| Screenshot store | SeaweedFS (S3 gateway) | container `seaweedfs`, **no published port** | volume `cyril_screenshots` |
| Agent build outputs | files | `server/dist/` on the host, mounted at `/dist` | host folder (not in git) |
| Windows agent | Python → single `cyril.exe` (PyInstaller) | each PC, `C:\Program Files\Cyril` | `C:\ProgramData\Cyril` |
| Mac agent | Python → single binary per CPU (PyInstaller) | each Mac, `/Library/Application Support/Cyril` | same folder, `data/` |
| Browser extension (optional) | MV3 extension | unpacked from `cyril-extension.zip` | none |

Design intent: **self-hosted, no cloud dependency.** If the server is down the agents keep sampling, queue everything on disk and
send the backlog when it is back (events: `spool.jsonl`, up to ~50 MB; screenshots: `shots-pending`, up to ~300 MB / 7 days).

### Repository layout

| Path | What it is |
|---|---|
| `server/cyril_server/app.py` | all API endpoints, auth, page serving, installer endpoints |
| `server/cyril_server/pages/*.html` | dashboard pages (served **only** to signed-in browsers) |
| `server/cyril_server/static/app.js, app.css` | shared client code and styles (public; contain no data) |
| `server/cyril_server/{classify,reports,names,db,objstore}.py` | rule engine, report maths, display names, SQLite schema, S3 client |
| `server/cyril_server/winscripts/`, `macscripts/` | `install` / `uninstall` scripts, templated with server address and key |
| `server/docker-compose.yml`, `Dockerfile`, `seaweedfs/entrypoint.sh` | the deployment |
| `agent/cyril_agent/` | the agent (sampling, spool, screenshots, self-update, platform code) |
| `windows/build.ps1`, `mac/build.sh` | agent build scripts (used by CI and by hand) |
| `.github/workflows/agents.yml` | CI that builds and publishes the agents |
| `update.ps1` | one-command update of the office server |
| `browser-extension/` | optional URL reporter (the Windows agent normally reads the address bar itself) |

---

## 2. Server deployment

### 2.1 First install (office server, Docker)

Prerequisites: Docker with Compose, Git, and (for agent updates) the GitHub CLI `gh` signed in once (`gh auth login`).

```bash
git clone https://github.com/icestudios-d1/trakbot.git && cd trakbot/server
cp .env.example .env        # then edit .env (below)
docker compose up -d --build
curl http://localhost:8000/api/health      # {"ok":true,...,"build":"xxxxxxxx"}
```

### 2.2 Configuration (`server/.env`, git-ignored)

| Variable | Required | Meaning |
|---|---|---|
| `CYRIL_ADMIN_TOKEN` | yes | The dashboard password. One shared token for all admins. Use a long random value. |
| `CYRIL_AGENT_KEY` | yes | Shared key every agent sends (`X-Api-Key`). Lets an agent *submit* reports only, not read anything. It is inside the install one-liner and in each PC's `config.json`. |
| `CYRIL_S3_ACCESS_KEY` | yes | SeaweedFS login id. **Letters and digits only** (no `/`, space, comma) — otherwise screenshot storage fails with a 502. |
| `CYRIL_S3_SECRET_KEY` | yes | SeaweedFS secret; any characters. |
| `CYRIL_PUBLIC_URL` | recommended | Address agents are told to use, e.g. `https://cyril.icestudios.in`. If empty the server uses whatever address the browser used. |
| `CYRIL_WORK_START` / `CYRIL_WORK_END` | no | Hours of the dashboard's **Work** view (default `8` and `22`, viewer's local time). |
| `CYRIL_REQUIRE_SEALED` | no | `1` = refuse uploads that are not sealed (old agents). Turn on once *Install agents* shows 🔒 sealed on every PC. |
| `CYRIL_PAYLOAD_KEY` | no | Base64 of the server's 32-byte private payload key. Normally leave empty: the server creates `payload.key` next to the database on first start. |
| `CYRIL_OMS_ORIGINS` | no | Comma-separated web origins allowed to read the agent's **OMS Ping** (§7), e.g. `https://oms.icestudios.in`. Reaches agents through their 10-second poll. |
| `CYRIL_CACHE_TTL` | no | Seconds the Overview/Employee report is cached (default `30`; `0` disables). |

Inside the container, compose also sets `CYRIL_DB=/data/cyril.db`, `CYRIL_DIST_DIR=/dist`, `CYRIL_S3_ENDPOINT=seaweedfs:8333`, `CYRIL_S3_BUCKET=cyril-screenshots`.
After changing `.env`: `docker compose up -d --force-recreate` (both containers must read the same values).

### 2.3 Storage, ports, volumes

- Port **8000** is published by the `server` container. SeaweedFS publishes **nothing**; only the server can reach it, so screenshots are only ever seen through the server's authenticated endpoints.
- Volumes: `cyril_db` (SQLite), `cyril_screenshots` (SeaweedFS data). Bind mount `./dist:/dist` (agent builds).
- SeaweedFS runs `weed server -s3` with `-volume.max=1000` and 1 GB volumes (see `seaweedfs/entrypoint.sh`); it needs free disk to allocate volumes.
- Screenshot objects are stored as `<login>/<timestamp>_<device>.jpg`; since this release a small thumbnail is stored next to each as `thumbs/<same key>`.

### 2.4 TLS and the public address

The server speaks plain HTTP on 8000 and expects TLS to be terminated in front of it (reverse proxy such as Caddy/nginx, or a tunnel/CDN).
The code comments assume Cloudflare is in front (it mentions Cloudflare caching `.exe` files and blocking Python's default User-Agent), and the
public name is `cyril.icestudios.in`. **Confirm** the exact proxy/tunnel, who owns the certificate, and that the proxy forwards
`X-Forwarded-Proto` / `X-Forwarded-Host` (the server uses them to build the install one-liner and to set the `Secure` flag on the sign-in cookie).
The *Install agents* page shows "(not HTTPS)" next to the server address when agents would use plain HTTP.

### 2.5 Updating the server

```powershell
.\update.ps1            # on the server PC, in the repo (see §4)
```
or by hand: `git pull origin main && cd server && docker compose up -d --build`.
Rebuild is required whenever `requirements.txt` changes (this release adds **Pillow** for thumbnails). The dashboard shows `build xxxxxxxx` next to *Sign out*; it must equal
`curl https://<server>/api/health`. After an update browsers revalidate pages automatically (pages are `no-store`, static files `no-cache`).

### 2.6 Backups and retention (**Confirm**)

Nothing in the repository backs anything up. At minimum back up the two Docker volumes (SQLite file and SeaweedFS data), `server/.env`, and **`payload.key`** (it lives in the `cyril_db` volume, next to the database; without it nothing still queued on PCs can be opened).
Retention: screenshots can be bulk-deleted by date range from the dashboard; **events, titles, URLs and alerts have no automatic retention limit.**
Encrypt the server's disk (BitLocker/LUKS): the database and screenshots are not encrypted at rest by the application.

---

## 3. How the dashboard is protected

- **Sign-in.** The admin enters `CYRIL_ADMIN_TOKEN` on `/login`. The server then sets an **HttpOnly, SameSite=Lax** cookie holding a keyed hash of the token (never the token); `Secure` when served over HTTPS. "Keep me signed in" makes it last 30 days, otherwise it is a session cookie. *Sign out* clears it.
- **Pages are gated on the server.** Every dashboard page request without that cookie gets a `302` to `/login?next=…` before any HTML is sent. Page files are not in the public static folder. Only `/login`, `/static/app.js`, `/static/app.css` are public.
- **API.** Every `/api/*` data endpoint requires `Authorization: Bearer <admin token>`. The cookie is accepted for `GET` only (so `<img>` tags can load screenshots and the browser can cache them); it can never change data.
- **Agents** authenticate with `X-Api-Key` and can only call ingest/screenshot/status/poll/update-log endpoints.
- Single shared admin token: there are no per-person accounts or audit trail of who looked at what. **Confirm** whether that is acceptable for your policy.

---

## 4. GitHub → agents → employee machines (the artefact pipeline)

### 4.1 The CI workflow (`.github/workflows/agents.yml`, name `agents`)

**When it runs:** on a push to `main` that touches `agent/**`, `browser-extension/**`, `windows/build.ps1`, `mac/build.sh` or the workflow file; or manually (*Actions → agents → Run workflow*).
Server-only changes (dashboard, API) do **not** rebuild agents. Work on a feature branch builds nothing until it is merged to `main`.

**Jobs**

| Job | Runner | What it does | Output (workflow artifact) |
|---|---|---|---|
| `windows` | `windows-latest`, Python 3.12 | `./windows/build.ps1 -OutDir out`: installs PyInstaller + deps, builds `cyril.exe` (`--onefile --noconsole`, bundles `uiautomation`, `comtypes`, `certifi`), **runs `cyril.exe --selftest` and refuses to publish if it fails**, zips the browser extension, writes `manifest.json` (SHA-256, `build` = short git SHA, `selftest: true`) | `win` → `cyril.exe`, `cyril-extension.zip`, `manifest.json` |
| `mac` | `macos-15` (Apple Silicon, arm64) | `OUT_DIR=out ./mac/build.sh`: venv, PyInstaller one-file build, self-test, writes `manifest-mac-arm64.json` | `mac-arm64` → `cyril-mac-arm64`, `manifest-mac-arm64.json` |
| `mac-intel` | `macos-15-intel` (x86_64) | same script | `mac-x86_64` → `cyril-mac-x86_64`, `manifest-mac-x86_64.json` |
| `publish` | `ubuntu-latest`, needs **`windows` + `mac` only** | downloads those artifacts, carries over the Intel files from the previous release, **deletes** the release `agent-latest` (and its tag), **re-creates** it at the commit SHA, using the built-in `GITHUB_TOKEN` (`permissions: contents: write`) | GitHub Release **`agent-latest`** ("Agents (latest)") |
| `publish-intel` | `ubuntu-latest`, needs `mac-intel` + `publish` | adds the fresh Intel binary and manifest to the release (`gh release upload --clobber`) | same release |

The Intel build is deliberately **not** waited for: GitHub has few Intel Mac runners and a job can sit queued for a long time. Windows and Apple Silicon publish at once; Intel Macs keep the previously published Intel build until the new one arrives. A `concurrency` group stops two runs from recreating the release at the same time.

**What is in the release (7 files):** `cyril.exe`, `cyril-extension.zip`, `manifest.json`, `cyril-mac-arm64`, `manifest-mac-arm64.json`, `cyril-mac-x86_64`, `manifest-mac-x86_64.json`.

Things to know:
- `agent-latest` is a **rolling** release: only the newest build exists. There is no history to roll back to; to go back, revert the commit on `main` (CI rebuilds) or re-run the workflow on an older commit, and keep a copy of a known-good `server/dist/`.
- The release is deleted and recreated, so for a short moment during `publish` there is no release; do not run `update.ps1` until the workflow shows green.
- No secrets are needed in GitHub: the workflow uses only `GITHUB_TOKEN`. Binaries are **not code-signed**; Windows SmartScreen/AV and macOS Gatekeeper behaviour on unsigned binaries is something to watch (**Confirm** whether you plan to sign).
- `manifest.json`'s `version` is whatever `-Version` was passed to `build.ps1` (CI passes none, so it says `0.1.0`); it is cosmetic. What drives updates is the **build id** (short git SHA) / file hash, and the installers compare SHA-256.

### 4.2 Getting the artefacts onto the server — `update.ps1`

On the server PC, in the repo, once CI is green:

```powershell
.\update.ps1                 # git pull origin main → gh release download agent-latest → docker compose up -d --build
.\update.ps1 -SkipPull       # use the code as it is
.\update.ps1 -LocalBuild     # build the Windows agent on this PC instead of downloading (no Mac binaries then)
.\update.ps1 -ForceAgent     # rebuild the Windows agent locally even if unchanged
.\update.ps1 -DryRun         # say what it would do
```
It downloads the release into `server\dist` (`gh release download agent-latest --dir server\dist --clobber`), rebuilds/restarts the server container, waits for `/api/health` and prints the live build id.
`server/dist` is **bind-mounted**, so new files are served immediately — the agent builds themselves need no server restart.
Without `gh` (or not signed in) it falls back to a local Windows build, and the Mac agents then cannot be refreshed.

### 4.3 From the server to each machine

**New machine** — the *Install agents* page gives a one-liner with the server address and agent key filled in:

| OS | Command (admin/sudo) | Silent form |
|---|---|---|
| Windows | `& ([scriptblock]::Create((irm 'https://<server>/install.ps1?key=<AGENT_KEY>')))` | add `-Silent` |
| Mac | `curl -fsSL 'https://<server>/install.sh?key=<AGENT_KEY>' \| sudo bash` | `… \| sudo bash -s -- --silent` |

What the installer does (Windows; the Mac one mirrors it): checks the server, reads `/api/agent/version` (SHA-256), downloads `/dist/cyril.exe?h=<hash>` to a temp name, **verifies the checksum**, **runs `--selftest` on the new file before replacing anything**, only then stops the old agent and swaps the file; unpacks the extension; writes `C:\ProgramData\Cyril\config.json` (server, key, optional user) with admin-only write permission and a user-writable `data\` folder; registers scheduled tasks; starts the agent.

**Windows scheduled tasks**

| Task | Runs as | When | Purpose |
|---|---|---|---|
| Cyril Agent | logged-in user (`Users` group, limited) | at every logon, plus re-launch every 5 min if closed | must run in the user's desktop session (SYSTEM/session 0 cannot see windows or the screen); runs `cyril.exe --config … --update-check-every 0` |
| Cyril Updater | SYSTEM | daily ~03:00 (+≤30 min random), 5 min after every start, retries 3×/10 min | re-runs the installer in `-Unattended` mode; does nothing unless a new build is published |
| Cyril Recovery Check | SYSTEM | every 15 min | reads `data\health.json`; if uploads have failed for over an hour, re-runs the installer to fetch a fix (at most once per 30 min) |

**Mac:** installs to `/Library/Application Support/Cyril`, a **LaunchAgent** (`in.icestudios.cyril`, runs in the logged-in user's session, restarted by launchd) and a root **LaunchDaemon** (`in.icestudios.cyril.updater`, re-runs the installer daily and at start). The person at the Mac must allow **Screen Recording** once, and **Automation** per browser to let the agent read page addresses; no installer can grant these (an MDM privacy profile can pre-approve them).

**Updates:** the agent's own updater is disabled (`--update-check-every 0`); updating is done by the SYSTEM/root updater job re-running the installer, which downloads only when the published hash differs. So a published build reaches every PC within about a day (or on next start / resume), or immediately with `Start-ScheduledTask 'Cyril Updater'` (Windows, elevated) / `sudo launchctl kickstart -k system/in.icestudios.cyril.updater` (Mac). The *Agent build* column on *Install agents* shows `✓ current`, `⟳ update pending` or `⟳ old agent`.

**Uninstall:** `& ([scriptblock]::Create((irm 'https://<server>/uninstall.ps1')))` (admin) / `curl -fsSL '…/uninstall.sh' | sudo bash`; each lists what it removed. Server-side history stays until you delete the employee (*Employees → Delete* also removes screenshot files).

**What if the installer window is closed early?** The agent is already installed and started *before* the installer starts waiting for the first report (`Start-ScheduledTask` runs first; the agent is owned by Task Scheduler, not by your window). Closing during *Waiting for Server Confirmation* only loses the success message. Closing during the download leaves the old agent in place (the new file is a temp file until verified); the daily updater retries. `-Silent` skips the wait entirely.

### 4.4 Release checklist (agent change)

1. Change code under `agent/` (bump `VERSION` in `agent/cyril_agent/update.py` if you track versions), run `cd agent && pytest`.
2. Merge to `main`. Watch *Actions → agents* until all three jobs and `publish` are green (builds take a few minutes).
3. On the server PC: `.\update.ps1`. Confirm *Install agents* shows the new **build** under "Agent program" and `Mac agent` published for both CPUs.
4. Push to one test PC: `Start-ScheduledTask 'Cyril Updater'`; confirm its row turns `✓ current`.
5. The rest follow overnight. Check *Install agents* the next morning for `update pending` rows (PCs that were off).

### 4.5 Release checklist (server/dashboard-only change)

1. Run `cd server && pytest`. 2. Merge to `main`. 3. On the server PC: `git pull origin main && cd server && docker compose up -d --build` (or `.\update.ps1 -SkipPull` after pulling). 4. Verify `/api/health` build id matches the dashboard footer; hard-refresh once.

---

## 5. Operating it

| Task | How |
|---|---|
| Health | `GET /api/health`; *Install agents* → "Ready to install?" checks and the *Machines reporting* table (last report, agent build, web-address reading, screenshot status, queued items) |
| Storage problems | *Install agents → Screenshot storage* writes/reads/deletes a test object and prints the likely fix |
| Logs (server) | `docker compose logs server --tail 50`, `docker compose logs seaweedfs --tail 50` |
| Logs (PC) | `C:\ProgramData\Cyril\data\agent.log`, `crash.log`; Mac: `/Library/Application Support/Cyril/data/` |
| Rules | *Productivity rules*; editing a rule re-labels history immediately (classification happens at query time) |
| Groups | *Employees → Manage groups*; groups marked *excluded* stay out of regular reports; pick a group on Overview/Employees/Web activity to view it |
| Work vs All | switch in the top bar; *Work* trims every report to 8 AM–10 PM your time |
| Screenshot frequency | *Screenshots* page (applies to PCs within ~a minute) |
| Remove an employee | *Employees → Delete* (type the login). Uninstall the agent first or the person reappears |
| Silent roll-out | Install page → *Silent install* |

**Performance notes (this release).** Reports classify once per distinct program/site/title, the previous-period comparison computes only two headline numbers, `events`/`screenshots`/`alerts` have a `ts` index, and the team report is cached 30 s. On a synthetic 1.1 M-event database the 7-day Overview went from ~14 s to ~2 s (30-day: 29 s → ~6 s). If it grows past a few million events, the next step is pre-aggregating per-day totals in SQLite.

---

## 6. Security and privacy position (read this before going wider)

**What is protected.** Dashboard and data behind the admin token; screenshots only through the authenticated server; agents can only submit; SeaweedFS has no exposed port; agent downloads are checksum-verified and self-tested before install; every update is logged (`update_log`, local `~/.cyril-update.log`).

**Encryption — how payloads are protected (sealed uploads)**

The server owns an X25519 key pair (`payload.key`). The installer writes only the **public** key into each PC's admin-only `config.json`.
The agent seals every event, and every screenshot's image / window title / app name, to that public key (fresh ephemeral key per item,
HKDF-SHA256, AES-256-GCM; `agent/cyril_agent/seal.py`) **before** it touches the disk or the network. A public key can encrypt but cannot
decrypt, so the agent itself cannot read what it queued. Result:

| Where | Status |
|---|---|
| Queue on the PC (`spool.jsonl`, `shots-pending\*.json`) | **Ciphertext.** An employee (even a local admin) sees only who/when plus encrypted blobs. Nothing readable is stored on the PC. |
| On the wire | Sealed items inside TLS. A proxy or a user-installed root certificate shows ciphertext, not apps, titles, URLs or pictures. Still readable: login name, device id, and a screenshot's capture time. |
| Server | Opens each item on arrival (private key never leaves `payload.key` / `CYRIL_PAYLOAD_KEY`) and stores it as before. The database and SeaweedFS are **not encrypted at rest** by the application; use full-disk encryption on the server. |
| Old agents | Keep sending plain until updated (daily ~03:00). *Install agents* shows 🔒 sealed / ⚠ not sealed yet per PC. Then set `CYRIL_REQUIRE_SEALED=1`. |
| Browser | The admin token sits in `localStorage`/`sessionStorage` (by the "keep me signed in" choice) plus the HttpOnly cookie. |

Limits to be honest about: the plaintext necessarily exists in the agent's memory for a moment before sealing, so someone with local admin
rights who attaches a debugger to the process, or replaces the binary, can still capture it; a local admin can also replace the public key in
`config.json` (this only affects what is queued *afterwards*, and the server will fail to open it, which shows up as 400s). The agent key
is still readable from `config.json` (it allows *submitting* only). Sealing removes the practical routes (reading the queue, reading the
traffic); the stronger step is a SYSTEM service owning the agent's work (item 3 below).

**What would actually raise the bar** (not built; in order of value for effort):
1. Always `https://`, with HSTS on the proxy; refuse plain HTTP outside a closed LAN.
2. Standard employees without local admin (removes debugger/replace-the-binary tricks and tamper with the tasks).
3. **Split the agent**: a small SYSTEM service owns the agent key and the upload; the user-session part only captures and hands over through a named pipe. Then even the key and the process memory are out of the employee's reach.
4. Per-device keys (revocable) instead of one shared agent key.
5. Certificate pinning in the agent (only matters now for hiding who-reports-when metadata from a user-installed proxy).
What cannot be hidden: the employee can always see their own screen, and a determined local administrator can observe anything a process on their machine does. Cyril deliberately does not hide itself (visible in Task Manager and Task Scheduler); see README "What we deliberately didn't build".

**Other items to be aware of**
- The agent's local listener `127.0.0.1:7077` **no longer accepts reports from web pages**: `POST /` is refused unless it comes from a browser extension (or has no Origin), must be `application/json`, and the Host must be `127.0.0.1`/`localhost` (DNS-rebinding guard); it sends no `Access-Control-Allow-Origin`. A website can therefore no longer feed the agent a made-up URL. (Another program running on the PC as the same user still can; that is inside the trust boundary.)
- `seaweedfs` image is `chrislusf/seaweedfs:latest` — pin a tag before production (comment in `docker-compose.yml`).
- Unsigned agent binaries (see §4.1). Single admin token. No retention on events/URLs/titles. Legal/consent: the README's *Scope & compliance* section applies; keep the employee notice and agreement in step with what is collected.

---

## 7. OMS Ping — letting the sign-in site check that Cyril is on the PC

The agent already listens on `127.0.0.1:7077`. It now answers **`GET http://127.0.0.1:7077/oms/ping`**:

```js
// on the OMS sign-in page
const r = await fetch('http://127.0.0.1:7077/oms/ping?nonce=' + crypto.randomUUID(), {cache: 'no-store'});
const p = await r.json();     // {ok:true, app:"cyril", ping:"pong", version, build, platform, device, user, sealed, time, nonce}
```
If the fetch throws (connection refused) Cyril is not running on that computer.

- **Who can read it:** only origins listed in the server setting `CYRIL_OMS_ORIGINS` (comma separated, e.g. `https://oms.icestudios.in`). Agents pick the list up from their 10-second poll (or `"oms_origins": [...]` in `config.json`). Any other website gets a minimal `{ok:true}` with no CORS header, so the browser hides it. A request with no `Origin` (a local tool such as `curl`) gets the full answer.
- **Private Network Access:** Chrome/Edge first send an `OPTIONS` preflight when a public HTTPS page calls a loopback address; the agent answers it with `Access-Control-Allow-Origin: <that origin>` and `Access-Control-Allow-Private-Network: true` for allowed origins only.
- **Safety checks:** Host must be `127.0.0.1`/`localhost` (DNS-rebinding guard); responses are `no-store`; the optional `nonce` is echoed (max 64 chars) so the page can tell a fresh answer from a cached one.
- **Browsers:** Chrome, Edge and Firefox allow an HTTPS page to call loopback; **Safari blocks it** (mixed content), so Safari users need the browser-extension route (a content script on the OMS origin that reports `{cyril:true}` to the page) if you must support them.
- **It is a convenience check, not proof.** A page can only trust what the browser tells it; a user could run a fake listener on port 7077. If OMS must *enforce* it, add a server-checked handshake: OMS gives the page a one-time nonce → page calls `/oms/ping?nonce=…` → OMS's back end asks the Cyril server (new endpoint) whether that device reported that nonce recently. Not built; say if you want it.
- **Rollout:** needs the new agent build (GitHub Actions on merge to `main`, then `update.ps1`, PCs update ~03:00) and the server setting `CYRIL_OMS_ORIGINS`. Tests: `agent/tests/test_local_door.py`.

---

## 8. Known gaps / to confirm

- [ ] Reverse proxy / tunnel, TLS owner, forwarded headers (§2.4).
- [ ] Backups of `cyril_db`, `cyril_screenshots`, `.env` (§2.6) and restore test.
- [ ] Retention policy for events/URLs/titles (none today) and screenshots.
- [ ] Code signing for `cyril.exe` / Mac binaries; SmartScreen/Gatekeeper/AV exclusions.
- [ ] Pin the SeaweedFS image tag.
- [ ] Individual admin accounts and an access log if policy needs one.
- [ ] Set `CYRIL_OMS_ORIGINS`, then `CYRIL_REQUIRE_SEALED=1` once every PC shows 🔒 sealed.
- [ ] An MDM profile to pre-approve Screen Recording/Automation on Macs.
- [ ] Decide on the remaining hardening steps in §6 (no local admin for staff; SYSTEM-service split).
