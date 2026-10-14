import { env, SELF } from "cloudflare:test";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import schema from "../schema.sql?raw";
import { sweepOldScreenshots } from "../src/index";

const A = { Authorization: "Bearer admin" };
const K = { "x-api-key": "agent" };
const J = { "content-type": "application/json" };
const get = (p: string, h: HeadersInit = A) => SELF.fetch("https://t" + p, { headers: h });
const post = (p: string, body: unknown, h: HeadersInit = A) =>
  SELF.fetch("https://t" + p, { method: "POST", headers: { ...J, ...h }, body: JSON.stringify(body) });
const now = () => Date.now() / 1000;

// ImageKit stand-in: upload -> deterministic ids, delete -> 204-ish, signed image fetch -> fake JPEG.
const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3]);
let uploads: { name: string; folder: string; priv: string }[] = [];
let deleted: string[] = [];
function mockImageKit() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input: any, init?: any) => {
    const url = typeof input === "string" ? input : input.url;
    if (url.startsWith("https://upload.imagekit.io/")) {
      const f = init.body as FormData;
      uploads.push({ name: String(f.get("fileName")), folder: String(f.get("folder")), priv: String(f.get("isPrivateFile")) });
      expect(init.headers.Authorization).toBe("Basic " + btoa("private_test:"));
      const n = uploads.length;
      return Response.json({ fileId: "fid" + n, filePath: `${f.get("folder")}/${f.get("fileName")}` });
    }
    if (url.endsWith("/files/batch/deleteByFileIds")) {
      deleted.push(...JSON.parse(init.body).fileIds);
      return Response.json({ successfullyDeletedFileIds: JSON.parse(init.body).fileIds });
    }
    if (url.startsWith("https://ik.imagekit.io/test/")) {
      expect(url).toMatch(/\?ik-s=[0-9a-f]{40}&ik-t=\d+$/); // HMAC-SHA1 hex + expiry
      return new Response(JPEG, { headers: { "content-type": "image/jpeg" } });
    }
    throw new Error("unexpected fetch " + url);
  });
}

beforeAll(async () => {
  const stmts = schema.replace(/^--.*$/gm, "").split(";").map((s) => s.trim()).filter(Boolean);
  const db = (env as any).DB as D1Database;
  await db.batch(stmts.map((s) => db.prepare(s)));
});
afterEach(() => vi.restoreAllMocks());

describe("cyril server", () => {
  it("requires auth", async () => {
    expect((await get("/api/summary", {})).status).toBe(401);
    expect((await post("/api/ingest", { user: "a", device: "d", events: [] }, {})).status).toBe(401);
    expect((await post("/api/ingest", { user: "a", device: "d", events: [] }, A)).status).toBe(401); // admin token != agent key
  });

  it("ingests, classifies, alerts and re-labels history", async () => {
    const t = now();
    const ev = [
      { ts: t, dur: 600, app: "code", title: "x" },
      { ts: t + 600, dur: 300, app: "chrome", title: "YT", url: "https://www.youtube.com/watch" },
      { ts: t + 900, dur: 200, app: "chrome", title: "t", url: "https://thepiratebay.org/" },
      { ts: t + 1100, dur: 100, idle: true },
    ];
    expect(await (await post("/api/ingest", { user: "bob", device: "pc1", events: ev }, K)).json()).toEqual({ stored: 4, alerts: 1 });
    let s: any = await (await get("/api/summary?user=bob")).json();
    expect(s.seconds).toMatchObject({ productive: 600, unproductive: 500, idle: 100 });
    expect(s.productivity_pct).toBe(54.5);
    expect(s.sites[0].name).toBe("youtube.com");
    const alerts: any[] = await (await get("/api/alerts")).json();
    expect(alerts[0].category).toBe("Piracy");
    await post("/api/rules", { kind: "domain", pattern: "youtube.com", category: "Training", label: "productive", priority: 100 });
    s = await (await get("/api/summary?user=bob")).json();
    expect(s.seconds.productive).toBe(900);
  });

  it("validates events", async () => {
    const r = await post("/api/ingest", { user: "a", device: "d", events: [{ ts: 1, dur: 99999 }] }, K);
    expect(r.status).toBe(422);
  });

  it("flags private browsing", async () => {
    await post("/api/ingest", { user: "priv", device: "pc1", events: [{ ts: now(), dur: 60, app: "chrome", title: "(private/incognito browsing)" }] }, K);
    const alerts: any[] = await (await get("/api/alerts")).json();
    expect(alerts.some((a) => a.category === "Private/Incognito Browsing" && a.user === "priv")).toBe(true);
  });

  it("trend returns one row per day", async () => {
    await post("/api/ingest", { user: "trendy", device: "d", events: [{ ts: now(), dur: 300, app: "code", title: "x" }] }, K);
    const rows: any[] = await (await get("/api/trend?user=trendy&days=3")).json();
    expect(rows).toHaveLength(3);
    expect(rows.reduce((n, r) => n + r.productive, 0)).toBe(300);
  });

  it("honours the dashboard's timezone offset for 'today'", async () => {
    const s: any = await (await get("/api/summary?tz=330")).json();
    expect(s.end - s.start).toBe(86400);
    expect((s.start + 330 * 60) % 86400).toBe(0); // starts at local midnight
  });

  it("screenshots: upload (private, to ImageKit) -> list -> signed proxy fetch -> delete", async () => {
    mockImageKit(); uploads = []; deleted = [];
    const b64 = btoa(String.fromCharCode(...JPEG));
    const t = now();
    const r: any = await (await post("/api/screenshot", { user: "shotty/../x", device: "pc1", ts: t, app: "chrome", title: "t", image_b64: b64 }, K)).json();
    expect(uploads).toEqual([{ name: `${Math.trunc(t * 1000)}_pc1.jpg`, folder: "/cyril/shotty_.._x", priv: "true" }]);
    const rows: any[] = await (await get(`/api/screenshots?user=${encodeURIComponent("shotty/../x")}&start=${t - 5}&end=${t + 5}`)).json();
    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe(r.id);
    expect(rows[0]).not.toHaveProperty("ik_path");
    const img = await get(`/api/screenshots/${r.id}/image`);
    expect(img.status).toBe(200);
    expect(new Uint8Array(await img.arrayBuffer())).toEqual(JPEG);
    expect((await get(`/api/screenshots/${r.id}/image`, {})).status).toBe(401);
    expect(await (await post("/api/screenshots/delete", { ids: [r.id] })).json()).toEqual({ deleted: 1 });
    expect(deleted).toEqual(["fid1"]);
    expect((await get(`/api/screenshots/${r.id}/image`)).status).toBe(404);
  });

  it("keeps D1 rows if ImageKit delete fails", async () => {
    mockImageKit(); uploads = []; deleted = [];
    const t = now();
    const r: any = await (await post("/api/screenshot", { user: "keep", device: "d", ts: t, image_b64: btoa("abc") }, K)).json();
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 500 }));
    expect((await post("/api/screenshots/delete", { ids: [r.id] })).status).toBe(502);
    const rows: any[] = await (await get(`/api/screenshots?user=keep&start=${t - 5}&end=${t + 5}`)).json();
    expect(rows).toHaveLength(1);
  });

  it("retention sweep removes screenshots older than the window", async () => {
    mockImageKit(); uploads = []; deleted = [];
    const old = now() - 100 * 86400, fresh = now();
    await post("/api/screenshot", { user: "ret", device: "d", ts: old, image_b64: btoa("old") }, K);
    await post("/api/screenshot", { user: "ret", device: "d", ts: fresh, image_b64: btoa("new") }, K);
    expect(await sweepOldScreenshots({ ...(env as any), SCREENSHOT_RETENTION_DAYS: "90" })).toBe(1);
    const rows: any[] = await (await get(`/api/screenshots?user=ret&start=0&end=${fresh + 10}`)).json();
    expect(rows).toHaveLength(1);
    expect(rows[0].ts).toBe(fresh);
  });

  it("serves installer scripts", async () => {
    expect(await (await SELF.fetch("https://t/install.ps1")).text()).toContain("ScheduledTask");
    expect(await (await SELF.fetch("https://t/uninstall.ps1")).text()).toContain("Unregister-ScheduledTask");
  });
});
