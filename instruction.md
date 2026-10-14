# Cyril — Data-Masking Proposal for Legal Review

**Status:** PROPOSAL ONLY. Nothing described below has been built. This document describes what would change
and how, so Legal can approve, amend or reject each item before a professional developer implements it.
**Audience:** Legal / Privacy / HR. **Implementer (after approval):** a professional developer.

## 1. Purpose and boundaries

"Masking" here means **collecting less, or showing less, of what employees do** — to cut privacy risk and
data-protection exposure. It never means hiding Cyril from the employee. Out of scope for every item below
(and to be refused if ever requested):

- Hiding the agent from Task Manager, installed-apps lists, or renaming/disguising its process.
- Any change that makes monitoring less visible to the monitored person, or that conflicts with the employee notice/agreement.
- Keystroke logging (not built, stays unbuilt).

## 2. What Cyril collects today (baseline Legal is approving against)

| Data | Where captured | Where stored | Notes |
|---|---|---|---|
| Employee login name, device ID (machine GUID) | Agent | Cloudflare D1 | On every record |
| Foreground app name | Agent | D1 `events` | Every ~5s, merged into intervals |
| **Window title** (full text) | Agent | D1 `events`, D1 `alerts` | Titles often contain names, email subjects, document names, account numbers |
| **Full browser URL** incl. path, query string, fragment | Browser extension → agent | D1 `events`, `alerts` | Query strings can carry tokens, search terms, IDs |
| Idle/active state | Agent | D1 | |
| **Screenshots of the entire screen, all monitors** | Agent (~1/min while an app is foreground ≥60s) | Image in **ImageKit** (private); metadata + file id in D1 | Captures whatever is visible: personal email, banking, chats, passwords being shown |
| Private/incognito windows | Agent | D1 | Only the *fact* is stored; title/URL redacted on-device. Screenshots still occur |

Existing retention: screenshots auto-deleted after 90 days (configurable), manual bulk delete available.
**Events, titles, URLs and alerts currently have no retention limit.**

Third parties that process this data: **Cloudflare** (Workers, D1) and **ImageKit** (screenshots).
Legal should confirm DPAs, sub-processor lists, data-residency/transfer position (esp. for GDPR/UK, India DPDP)
for both before go-live — this is independent of masking but affects which masking is mandatory.

## 3. Proposed masking features

Each item lists: **what changes → how → data before/after → risks / trade-offs → decisions needed from Legal.**
Suggested priority: A = recommend building, B = recommend if Legal wants it, C = optional.

### M1 (A) — URL minimisation before it leaves the device
- **What:** store only the **domain** (e.g. `bank.example.com`) by default; drop path, query string and fragment.
- **How:** in the agent (`agent/cyril_agent/main.py`, where `url` is assembled) and again in the server ingest
  handler (`server/src/index.ts`) as defence-in-depth. A server setting chooses the level: `domain-only` (default) / `domain+path` / `full`.
- **Before → after:** `https://mail.example.com/inbox?q=salary&token=abc#msg42` → `mail.example.com`.
- **Trade-off:** productivity rules are domain-based already, so classification is unaffected; managers lose the ability to see *which page* within a domain.
- **Legal decides:** default level; whether any role may ever see full URLs.

### M2 (A) — Sensitive-site / sensitive-app exclusion list ("never record detail")
- **What:** for a maintained list of sensitive categories (online banking, health portals, government/tax, personal/webmail, password managers, union/legal-advice sites, HR self-service, job sites?), record **time only** (category + duration), with **no title, no URL path, and no screenshot**.
- **How:** a rules list synced from the server to the agent at start-up and hourly (new endpoint `GET /api/agent-policy`). The agent checks the foreground app/domain/title against it *before* creating the event or capturing a screenshot. Rules reuse the existing rule syntax (app / domain / title-regex). Changes to the list are versioned and logged.
- **Before → after:** event `{app: chrome, title: "Statement – HDFC NetBanking", url: ".../statements"}` + screenshot → `{app: chrome, title: "(sensitive)", url: "hdfc…" → "Banking"}`, no screenshot.
- **Risks:** list is never complete (false negatives); over-broad list weakens legitimate monitoring. Needs an owner and a review cadence. Employee-requested additions? (policy question)
- **Legal decides:** who owns the list; initial categories; whether employees can see the list; whether "job sites" belongs on it.

### M3 (A) — Screenshot minimisation
- **What (3 independent options):**
  1. **Active window only** instead of the whole desktop/all monitors (today all monitors are captured, which routinely includes unrelated windows and notifications).
  2. **Skip** screenshots when M2 matches, when the screen is locked, and during any defined personal-time window (see M7).
  3. **On-device blur/redaction**: downscale + blur regions known to be sensitive (password fields, notification toasts, windows matching M2), or an overall "low-detail" mode (e.g. 640px wide, heavier blur) so the image shows *what kind* of activity, not readable content.
- **How:** change `agent/cyril_agent/shots.py` (capture region, optional blur via Pillow) and the throttle logic in `main.py`. Capture mode becomes a server-pushed setting (`full-screen` / `active-window` / `blurred`).
- **Trade-off:** blurred/low-detail shots weaken their value as evidence for disciplinary matters (flagged-content alerts). Legal should decide whether evidential value or minimisation wins, possibly per alert category (e.g. full detail only *after* an alert fires).
- **Side benefit:** smaller images → fits the ImageKit free tier (3 GB) for more employees.
- **Legal decides:** capture mode default; whether alert-triggered captures may be higher-detail.

### M4 (A) — Automatic PII scrubbing of titles and URLs
- **What:** pattern-based redaction of obvious personal/secret data in window titles and URLs: email addresses, phone numbers, payment-card numbers (Luhn-checked), national IDs (e.g. Aadhaar, PAN, SSN formats), IBAN/account numbers, API keys / bearer tokens / long hex strings.
- **How:** a redaction function in the agent (primary, so raw data never leaves the machine) and in server ingest (backstop). Replaced with tags such as `[email]`, `[card]`. Patterns are configuration, versioned, tested against a fixture set.
- **Trade-off:** regex redaction is best-effort, not a guarantee; some false positives and false negatives. Must not be described to employees or regulators as "anonymisation".
- **Legal decides:** which identifier types are mandatory; jurisdiction-specific formats to include.

### M5 (B) — Pseudonymised dashboard with audited "unmask"
- **What:** dashboards show **employee codes** (`EMP-0412`) or initials by default. Revealing the real name, opening a person's timeline/titles, or viewing screenshots requires a recorded **reason** (and optionally approval by a second person, e.g. HR + manager).
- **How:** add a mapping table `user → pseudonym` (D1); the API returns pseudonyms unless the request carries an authorised "reveal" grant. Add an append-only `access_log` table (who, when, which employee, which screen, reason). Screenshot image requests are logged individually.
- **Prerequisite:** today there is **one shared admin token** for everyone. This feature needs per-person logins and roles (e.g. Cloudflare Access / SSO) — a separate piece of work the developer must scope.
- **Legal decides:** who may unmask; whether two-person approval is required; log retention; whether employees can request their own access log (DSAR support).

### M6 (B) — Tiered retention ("age-out masking")
- **What:** keep detail short, aggregates long. Example: full titles/URLs and alerts' text for **30 days**, then reduce to app + domain + category; aggregate time-use for **12 months**; screenshots **≤90 days** (already enforced). Alert records retained longer only when a case is open (legal hold flag).
- **How:** extend the existing daily Cron Trigger (`sweepOldScreenshots` in `server/src/index.ts`) with SQL jobs that null out `title` / `url` path on old rows and delete old alerts. Add a `legal_hold` flag that exempts a user's data from deletion.
- **Legal decides:** each retention period per data type and jurisdiction; legal-hold process; whether deletion must be provable (deletion log).

### M7 (B) — Working-hours and break boundaries
- **What:** record only inside defined working hours per employee/timezone, with a policy on lunch/breaks and a visible "paused" state on the agent.
- **How:** schedule delivered via the same agent-policy endpoint as M2; the agent stops sampling and screenshots outside the window. (Any employee-facing pause control and its visibility to managers is a policy question; it must remain visible, not silent.)
- **Legal decides:** hours policy; whether employees may pause (and whether pauses are reported); treatment of on-call and remote work from home.

### M8 (C) — Alert minimisation
- **What:** alert rows store **category + time + app/domain** only; the title/URL/screenshot that triggered it is fetched through the audited path of M5 rather than copied into the alert.
- **How:** change the `alerts` insert in ingest and the alerts table in `server/src/index.ts` / `schema.sql`; link alerts to the underlying event id.
- **Legal decides:** whether alerts must be self-contained evidence (argues against this item).

### M9 (C) — Exclude specific people or groups
- **What:** per-user exemptions or reduced collection tiers (e.g. HR, legal, works-council representatives, employees on protected leave / under accommodation).
- **How:** agent-policy endpoint returns per-user overrides (collect nothing / time-only / full). Requires an authoritative roster source.
- **Legal decides:** which roles are exempt; works-council / union requirements.

## 4. Cross-cutting requirements for the implementer
1. **Mask at the source.** Where possible, redaction runs in the agent so sensitive text never travels or reaches Cloudflare/ImageKit. Server-side masking is a backstop only.
2. **Changes to masking policy are versioned and logged** (who, when, old → new).
3. **Existing data:** masking applies going forward. A backfill/purge job for already-stored data (titles, URLs, screenshots) is a separate item for Legal to decide on.
4. **Documentation:** employee notice, privacy policy and DPIA/PIA must be updated to describe the final masked state — and must not overstate it (best-effort ≠ guaranteed).
5. **Testing:** fixture-based tests for each pattern/rule; a review of real screenshots by a privacy reviewer before rollout.
6. **No stealth:** the implementer must not add any option that hides the agent or its presence from the device user.

## 5. Open questions for Legal (beyond individual items)
- Lawful basis / consent model per jurisdiction in which employees sit, and whether works-council consultation is needed.
- Is screenshotting the whole desktop proportionate at all, versus M3 options? (Strongest single risk reducer.)
- Cross-border transfer: data goes to Cloudflare (D1 location) and ImageKit (storage region). Acceptable? DPAs signed?
- Who is the data controller contact for employee requests (access, erasure, objection) and how will it be fulfilled from D1 + ImageKit?
- Personal-device or BYOD exclusion confirmed? (Cyril is intended for company-owned devices only.)

## 6. Approval sheet

| ID | Feature | Approve / Amend / Reject | Conditions / notes | Legal initials & date |
|---|---|---|---|---|
| M1 | URL minimisation | | | |
| M2 | Sensitive-site/app exclusion | | | |
| M3 | Screenshot minimisation | | | |
| M4 | PII scrubbing | | | |
| M5 | Pseudonymised dashboard + audited unmask | | | |
| M6 | Tiered retention | | | |
| M7 | Working-hours boundaries | | | |
| M8 | Alert minimisation | | | |
| M9 | Per-person exemptions | | | |

Approved by: ____________________  Role: ____________  Date: ____________
