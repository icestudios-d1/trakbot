/** Cyril server (Cloudflare Worker): ingest API, reports, rules + alerts, dashboard host. */
import { Hono } from "hono";
import { classify, LABELS, matchingRule, type Rule } from "./classify";
import { DEFAULT_RULES } from "./defaultRules";
import { deleteFiles, signedUrl, uploadJpeg } from "./imagekit";
import installPs1 from "./winscripts/install.ps1";
import uninstallPs1 from "./winscripts/uninstall.ps1";

export interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
  CYRIL_ADMIN_TOKEN: string;
  CYRIL_AGENT_KEY: string;
  IMAGEKIT_PRIVATE_KEY: string;
  IMAGEKIT_URL_ENDPOINT: string;
  SCREENSHOT_RETENTION_DAYS?: string;
  DEFAULT_TZ_OFFSET_MIN?: string;
}

const MAX_EVENT_SECONDS = 3600;
const MAX_SHOT_B64 = 3_000_000; // ~2.2MB decoded; agent targets far smaller JPEGs
const INGEST_CHUNK = 200;       // events per INSERT (keeps each bound JSON string small)

const app = new Hono<{ Bindings: Env }>();
const enc = new TextEncoder();

// ------------------------------------------------------------------ auth
async function safeEqual(a: string, b: string): Promise<boolean> {
  const [ha, hb] = await Promise.all([a, b].map((s) => crypto.subtle.digest("SHA-256", enc.encode(s))));
  return (crypto.subtle as any).timingSafeEqual(ha, hb);
}
async function check(given: string, expected: string | undefined) {
  return !!expected && !!given && (await safeEqual(given, expected));
}
app.use("/api/*", async (c, next) => {
  const p = new URL(c.req.url).pathname;
  if (p === "/api/health") return next();
  const isAgent = p === "/api/ingest" || p === "/api/screenshot";
  const ok = isAgent
    ? await check(c.req.header("x-api-key") ?? "", c.env.CYRIL_AGENT_KEY)
    : await check((c.req.header("authorization") ?? "").replace(/^Bearer /, ""), c.env.CYRIL_ADMIN_TOKEN);
  if (!ok) return c.json({ detail: "unauthorized" }, 401);
  return next();
});

const bad = (c: any, msg: string, code = 422) => c.json({ detail: msg }, code);
const isStr = (v: unknown, min: number, max: number) => typeof v === "string" && v.length >= min && v.length <= max;

// ----------------------------------------------------------------- rules
let seeded = false;
async function getRules(db: D1Database): Promise<Rule[]> {
  if (!seeded) {
    const n = await db.prepare("SELECT COUNT(*) n FROM rules").first<{ n: number }>();
    if (!n?.n) {
      await db.prepare(
        "INSERT INTO rules(kind,pattern,category,label,alert) SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]')," +
        "json_extract(value,'$[2]'),json_extract(value,'$[3]'),json_extract(value,'$[4]') FROM json_each(?1)")
        .bind(JSON.stringify(DEFAULT_RULES)).run();
    }
    seeded = true;
  }
  const { results } = await db.prepare("SELECT * FROM rules ORDER BY priority DESC, id ASC").all<Rule>();
  return results;
}

function domainOf(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return ""; }
}

// ---------------------------------------------------------------- ingest
app.post("/api/ingest", async (c) => {
  const b: any = await c.req.json().catch(() => null);
  if (!b || !isStr(b.user, 1, 128) || !isStr(b.device, 1, 128) || !Array.isArray(b.events) || b.events.length > 2000)
    return bad(c, "invalid batch");
  const rules = await getRules(c.env.DB);
  const rows: any[] = [];
  const alerts: any[] = [];
  for (const e of b.events) {
    if (typeof e?.ts !== "number" || typeof e?.dur !== "number" || e.dur < 0 || e.dur > MAX_EVENT_SECONDS) return bad(c, "invalid event");
    const app = String(e.app ?? ""), title = String(e.title ?? ""), url = String(e.url ?? "");
    const idle = e.idle ? 1 : 0, domain = domainOf(url);
    rows.push({ ts: e.ts, dur: e.dur, app, title, url, domain, idle });
    const r = matchingRule({ app, title, domain, idle }, rules);
    if (r?.alert) alerts.push({ ts: e.ts, rule_id: r.id, category: r.category, app, title, url });
  }
  const stmts: D1PreparedStatement[] = [];
  for (let i = 0; i < rows.length; i += INGEST_CHUNK)
    stmts.push(c.env.DB.prepare(
      "INSERT INTO events(user,device,ts,dur,app,title,url,domain,idle) SELECT ?1,?2,json_extract(value,'$.ts')," +
      "json_extract(value,'$.dur'),json_extract(value,'$.app'),json_extract(value,'$.title'),json_extract(value,'$.url')," +
      "json_extract(value,'$.domain'),json_extract(value,'$.idle') FROM json_each(?3)")
      .bind(b.user, b.device, JSON.stringify(rows.slice(i, i + INGEST_CHUNK))));
  if (alerts.length)
    stmts.push(c.env.DB.prepare(
      "INSERT INTO alerts(ts,user,device,rule_id,category,app,title,url) SELECT json_extract(value,'$.ts'),?1,?2," +
      "json_extract(value,'$.rule_id'),json_extract(value,'$.category'),json_extract(value,'$.app')," +
      "json_extract(value,'$.title'),json_extract(value,'$.url') FROM json_each(?3)")
      .bind(b.user, b.device, JSON.stringify(alerts)));
  if (stmts.length) await c.env.DB.batch(stmts);
  return c.json({ stored: rows.length, alerts: alerts.length });
});

// --------------------------------------------------------------- reports
/** Workers run in UTC, so "today" and the hourly chart use a caller-supplied offset (minutes east of UTC). */
function tzOf(c: any): number {
  const q = Number(c.req.query("tz"));
  const d = Number(c.env.DEFAULT_TZ_OFFSET_MIN ?? 0);
  const v = c.req.query("tz") !== undefined && Number.isFinite(q) ? q : d;
  return Math.max(-840, Math.min(840, Math.round(v)));
}
function rangeOf(day: string | undefined, days: number, tz: number): [number, number] {
  let startMs: number;
  if (day && /^\d{4}-\d{2}-\d{2}$/.test(day)) startMs = Date.parse(day + "T00:00:00Z");
  else startMs = Math.floor((Date.now() + tz * 60000) / 86400000) * 86400000;
  const start = startMs / 1000 - tz * 60;
  return [start, start + days * 86400];
}
const intQ = (c: any, name: string, def: number, min: number, max: number) => {
  const n = Number(c.req.query(name) ?? def);
  return Number.isFinite(n) ? Math.max(min, Math.min(max, Math.trunc(n))) : def;
};
const top = (m: Map<string, number>, n = 10) =>
  [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, n).map(([name, s]) => ({ name, seconds: Math.round(s) }));
const add = (m: Map<string, number>, k: string, v: number) => m.set(k, (m.get(k) ?? 0) + v);

app.get("/api/users", async (c) => {
  const { results } = await c.env.DB.prepare(
    "SELECT user, MAX(ts+dur) last_seen, COUNT(DISTINCT device) devices FROM events GROUP BY user ORDER BY user").all();
  return c.json(results);
});

app.get("/api/summary", async (c) => {
  const user = c.req.query("user") || null;
  const days = intQ(c, "days", 1, 1, 92);
  const [start, end] = rangeOf(c.req.query("day"), days, tzOf(c));
  const rules = await getRules(c.env.DB);
  // Pre-aggregate in SQL (hour buckets relative to local midnight), classify the groups in JS.
  const { results } = await c.env.DB.prepare(
    "SELECT user, app, title, domain, idle, CAST((ts-?1)/3600 AS INTEGER) hb, SUM(dur) dur FROM events " +
    "WHERE ts>=?1 AND ts<?2 AND (?3 IS NULL OR user=?3) GROUP BY user, app, title, domain, idle, hb")
    .bind(start, end, user).all<any>();
  const labels = new Map<string, number>(), cats = new Map<string, number>();
  const apps = new Map<string, number>(), sites = new Map<string, number>();
  const perUser = new Map<string, Record<string, number>>();
  const hourly: Record<string, number>[] = Array.from({ length: 24 }, () => ({}));
  for (const e of results) {
    const [cat, lab] = classify(e, rules);
    add(labels, lab, e.dur); add(cats, cat, e.dur);
    const pu = perUser.get(e.user) ?? {}; pu[lab] = (pu[lab] ?? 0) + e.dur; perUser.set(e.user, pu);
    const h = hourly[((e.hb % 24) + 24) % 24]; h[lab] = (h[lab] ?? 0) + e.dur;
    if (lab !== "idle") {
      add(apps, e.app || "unknown", e.dur);
      if (e.domain) add(sites, e.domain, e.dur);
    }
  }
  let active = 0;
  for (const [k, v] of labels) if (k !== "idle") active += v;
  const prod = labels.get("productive") ?? 0;
  const rnd = (o: Record<string, number>) => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, Math.round(v)]));
  return c.json({
    start, end, user,
    seconds: Object.fromEntries([...LABELS, "idle"].map((k) => [k, Math.round(labels.get(k) ?? 0)])),
    productivity_pct: active ? Math.round((1000 * prod) / active) / 10 : null,
    categories: top(cats, 15), apps: top(apps), sites: top(sites),
    by_user: Object.fromEntries([...perUser].map(([u, m]) => [u, rnd(m)])),
    hourly: hourly.map(rnd),
  });
});

app.get("/api/timeline", async (c) => {
  const user = c.req.query("user");
  if (!user) return bad(c, "user required");
  const [start, end] = rangeOf(c.req.query("day"), 1, tzOf(c));
  const rules = await getRules(c.env.DB);
  const { results } = await c.env.DB.prepare("SELECT * FROM events WHERE user=?1 AND ts>=?2 AND ts<?3 ORDER BY ts")
    .bind(user, start, end).all<any>();
  return c.json(results.map((e) => {
    const [category, label] = classify(e, rules);
    return { ts: e.ts, dur: e.dur, app: e.app, title: e.title, url: e.url, category, label };
  }));
});

app.get("/api/trend", async (c) => {
  const user = c.req.query("user") || null;
  const days = intQ(c, "days", 14, 1, 92);
  const tz = tzOf(c);
  const [todayStart] = rangeOf(undefined, 1, tz);
  const start = todayStart - (days - 1) * 86400, end = todayStart + 86400;
  const rules = await getRules(c.env.DB);
  const { results } = await c.env.DB.prepare(
    "SELECT app, title, domain, idle, CAST((ts-?1)/86400 AS INTEGER) d, SUM(dur) dur FROM events " +
    "WHERE ts>=?1 AND ts<?2 AND (?3 IS NULL OR user=?3) GROUP BY app, title, domain, idle, d")
    .bind(start, end, user).all<any>();
  const buckets: Record<string, number>[] = Array.from({ length: days }, () => ({}));
  for (const e of results) {
    if (e.d < 0 || e.d >= days) continue;
    const [, lab] = classify(e, rules);
    buckets[e.d][lab] = (buckets[e.d][lab] ?? 0) + e.dur;
  }
  return c.json(buckets.map((b, i) => ({
    day: new Date((start + i * 86400 + tz * 60) * 1000).toISOString().slice(0, 10),
    ...Object.fromEntries([...LABELS, "idle"].map((k) => [k, Math.round(b[k] ?? 0)])),
  })));
});

// ----------------------------------------------------------------- rules
app.get("/api/rules", async (c) => c.json(await getRules(c.env.DB)));

app.post("/api/rules", async (c) => {
  const r: any = await c.req.json().catch(() => null);
  if (!r || !["app", "domain", "title"].includes(r.kind) || !isStr(r.pattern, 1, 256) || !isStr(r.category, 1, 64) ||
      !LABELS.includes(r.label)) return bad(c, "invalid rule");
  const res = await c.env.DB.prepare("INSERT INTO rules(kind,pattern,category,label,alert,priority) VALUES(?,?,?,?,?,?)")
    .bind(r.kind, r.pattern, r.category, r.label, r.alert ? 1 : 0, Number.isInteger(r.priority) ? r.priority : 0).run();
  return c.json({ id: res.meta.last_row_id });
});

app.delete("/api/rules/:id", async (c) => {
  await c.env.DB.prepare("DELETE FROM rules WHERE id=?").bind(Number(c.req.param("id"))).run();
  return c.json({ ok: true });
});

// ---------------------------------------------------------------- alerts
app.get("/api/alerts", async (c) => {
  const limit = intQ(c, "limit", 200, 1, 1000);
  const q = "SELECT * FROM alerts" + (c.req.query("unacked") === "true" ? " WHERE acked=0" : "") + " ORDER BY ts DESC LIMIT ?";
  return c.json((await c.env.DB.prepare(q).bind(limit).all()).results);
});

app.post("/api/alerts/:id/ack", async (c) => {
  await c.env.DB.prepare("UPDATE alerts SET acked=1 WHERE id=?").bind(Number(c.req.param("id"))).run();
  return c.json({ ok: true });
});

// ------------------------------------------------------------ screenshots
const safe = (s: string) => s.replace(/[^a-zA-Z0-9_.-]/g, "_");

app.post("/api/screenshot", async (c) => {
  const s: any = await c.req.json().catch(() => null);
  if (!s || !isStr(s.user, 1, 128) || !isStr(s.device, 1, 128) || typeof s.ts !== "number" || !isStr(s.image_b64, 1, MAX_SHOT_B64))
    return bad(c, "invalid screenshot");
  let bytes: Uint8Array;
  try { bytes = Uint8Array.from(atob(s.image_b64), (ch) => ch.charCodeAt(0)); } catch { return bad(c, "bad image data", 400); }
  let up;
  try {
    up = await uploadJpeg(c.env, bytes, `${Math.trunc(s.ts * 1000)}_${safe(s.device)}.jpg`, `/cyril/${safe(s.user)}`);
  } catch (e) {
    console.error(e);
    return c.json({ detail: "image store unavailable" }, 502);
  }
  const res = await c.env.DB.prepare(
    "INSERT INTO screenshots(user,device,ts,app,title,ik_file_id,ik_path,bytes) VALUES(?,?,?,?,?,?,?,?)")
    .bind(s.user, s.device, s.ts, String(s.app ?? ""), String(s.title ?? ""), up.fileId, up.filePath, bytes.length).run();
  return c.json({ id: res.meta.last_row_id });
});

app.get("/api/screenshots", async (c) => {
  const user = c.req.query("user"), start = Number(c.req.query("start")), end = Number(c.req.query("end"));
  if (!user || !Number.isFinite(start) || !Number.isFinite(end)) return bad(c, "user, start, end required");
  const { results } = await c.env.DB.prepare(
    "SELECT id,user,device,ts,app,title,bytes FROM screenshots WHERE user=? AND ts>=? AND ts<? ORDER BY ts LIMIT ?")
    .bind(user, start, end, intQ(c, "limit", 500, 1, 2000)).all();
  return c.json(results);
});

// Proxied through the Worker so every view is admin-authenticated and no long-lived image URL ever exists.
app.get("/api/screenshots/:id/image", async (c) => {
  const row = await c.env.DB.prepare("SELECT ik_path FROM screenshots WHERE id=?").bind(Number(c.req.param("id")))
    .first<{ ik_path: string }>();
  if (!row) return c.body(null, 404);
  const r = await fetch(await signedUrl(c.env, row.ik_path, 60));
  if (!r.ok) return c.body(null, r.status === 404 ? 404 : 502);
  return new Response(r.body, { headers: { "Content-Type": "image/jpeg", "Cache-Control": "private, max-age=300" } });
});

/** Deletes from ImageKit first, then D1 — so a failed ImageKit call never orphans stored files. */
async function removeScreenshots(env: Env, ids: number[]): Promise<number> {
  let n = 0;
  for (let i = 0; i < ids.length; i += 90) {
    const part = ids.slice(i, i + 90);
    const marks = part.map(() => "?").join(",");
    const { results } = await env.DB.prepare(`SELECT id, ik_file_id FROM screenshots WHERE id IN (${marks})`).bind(...part).all<any>();
    if (!results.length) continue;
    await deleteFiles(env, results.map((r) => r.ik_file_id));
    await env.DB.prepare(`DELETE FROM screenshots WHERE id IN (${marks})`).bind(...part).run();
    n += results.length;
  }
  return n;
}

app.post("/api/screenshots/delete", async (c) => {
  const d: any = await c.req.json().catch(() => null);
  if (!d || !Array.isArray(d.ids) || d.ids.length > 5000 || !d.ids.every(Number.isInteger)) return bad(c, "invalid ids");
  if (!d.ids.length) return c.json({ deleted: 0 });
  try { return c.json({ deleted: await removeScreenshots(c.env, d.ids) }); }
  catch (e) { console.error(e); return c.json({ detail: "image store unavailable" }, 502); }
});

// ------------------------------------------------------------- misc routes
const ps1 = (body: string) => new Response(body, { headers: { "Content-Type": "text/plain; charset=utf-8" } });
app.get("/install.ps1", () => ps1(installPs1));
app.get("/uninstall.ps1", () => ps1(uninstallPs1));
app.get("/api/health", (c) => c.json({ ok: true, time: Date.now() / 1000 }));

// ------------------------------------------------------------ retention cron
/** Daily sweep: screenshots older than SCREENSHOT_RETENTION_DAYS (default 90) are deleted from ImageKit + D1. */
export async function sweepOldScreenshots(env: Env, now = Date.now() / 1000): Promise<number> {
  const days = Number(env.SCREENSHOT_RETENTION_DAYS ?? 90);
  if (!(days > 0)) return 0;
  const cutoff = now - days * 86400;
  let total = 0;
  for (let i = 0; i < 10; i++) { // <= 900 per run keeps us inside the 50-subrequest free-plan cap; cron runs daily
    const { results } = await env.DB.prepare("SELECT id FROM screenshots WHERE ts<? ORDER BY ts LIMIT 90").bind(cutoff).all<{ id: number }>();
    if (!results.length) break;
    total += await removeScreenshots(env, results.map((r) => r.id));
  }
  return total;
}

export default {
  fetch: app.fetch,
  async scheduled(_ev: ScheduledController, env: Env, ctx: ExecutionContext) {
    ctx.waitUntil(sweepOldScreenshots(env).then((n) => console.log(`retention sweep deleted ${n} screenshots`)));
  },
} satisfies ExportedHandler<Env>;
