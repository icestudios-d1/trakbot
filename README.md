# TrakBot

Self-hosted employee activity monitoring (an ActivTrak-style tool without the seat limit):
a lightweight **agent** on each company device reports the foreground app, window title and browser
URL plus idle state to a central **server**, which labels time as productive / unproductive / neutral,
raises alerts for prohibited activity, and serves a manager dashboard.

## Feature map vs. ActivTrak

| ActivTrak | TrakBot |
|---|---|
| App & website usage tracking | ✅ agent (Windows/macOS/Linux) + browser extension for URLs |
| Productivity labels (productive/unproductive/undefined) | ✅ rule engine (app / domain / title), re-labels history on edit |
| Idle/active time | ✅ idle detection (`--idle-after`) |
| Productivity %, top apps/sites, categories, hourly activity | ✅ dashboard + `/api/summary` |
| Per-employee drill-down / timeline | ✅ `/api/timeline` |
| Alerts for risky activity | ✅ rules with `alert` flag (piracy, gambling pre-seeded) |
| Screenshots, keystroke logging, workload/utilisation, teams/groups, SSO, email reports | ❌ not yet (roadmap) |

## Run

```bash
# server
pip install -r server/requirements.txt
export TRAKBOT_ADMIN_TOKEN=<long-random>  TRAKBOT_AGENT_KEY=<long-random>
cd server && uvicorn trakbot_server.app:app --host 0.0.0.0 --port 8000   # put behind HTTPS (nginx/caddy)
# open http://host:8000 and paste the admin token

# agent (on each device; Linux also needs `xprop` and `xprintidle`)
cd agent && pip install -r requirements.txt
TRAKBOT_SERVER=https://trakbot.example.com TRAKBOT_AGENT_KEY=<key> python -m trakbot_agent
```
Load `browser-extension/` as an unpacked extension (or deploy by Chrome/Edge policy) to capture URLs.
Tests: `cd server && pytest`, `cd agent && pytest`.

## Scope & compliance

TrakBot records only app name, window title, URL and idle state — no keystroke logging or screenshots, and
private/incognito tabs are never reported. Even where the employment agreement covers monitoring, many
jurisdictions (GDPR/UK, several US states, India's DPDP Act, etc.) also require a specific notice of what is
collected and why, limited retention, restricted access to the data, and sometimes works-council consent.
Run it only on company-owned devices, tell employees what is collected, and have counsel review the policy.
