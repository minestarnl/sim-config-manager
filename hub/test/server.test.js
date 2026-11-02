import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import http from "node:http";
import { createHub, uptime } from "../src/server.js";
import { hashPassword, checkPassword } from "../src/config.js";

function tmpdir() { return fs.mkdtempSync(path.join(os.tmpdir(), "scm-hub-")); }

async function startHub(extra = {}) {
  const hooks = [];
  const hookSrv = http.createServer((req, res) => {
    let b = ""; req.on("data", (c) => (b += c)); req.on("end", () => { hooks.push({ ct: req.headers["content-type"], body: b }); res.end("ok"); });
  });
  await new Promise((r) => hookSrv.listen(0, "127.0.0.1", r));
  const hookUrl = `http://127.0.0.1:${hookSrv.address().port}/hook`;
  const hub = createHub({ dataDir: tmpdir(), quiet: true, offlineAfterMinutes: 15, kioskToken: "wall-123",
    users: [{ name: "admin", password: hashPassword("secret"), role: "admin" },
      { name: "tech", password: "tech-pw", role: "tech" }, { name: "office", password: "view-pw", role: "viewer" }],
    webhooks: [{ url: hookUrl, format: "slack", events: ["offline", "rulebook", "drift", "alert", "version", "online"] }],
    ...extra });
  const addr = await hub.listen(0, "127.0.0.1");
  const base = `http://127.0.0.1:${addr.port}`;
  return { hub, base, hooks, close: async () => { await hub.close(); hookSrv.close(); } };
}

function client(base) {
  let cookie = "";
  return async (p, { method = "GET", body, headers = {} } = {}) => {
    const res = await fetch(base + p, { method, redirect: "manual", headers: { "Content-Type": "application/json",
      "X-Requested-With": "scm", ...(cookie ? { Cookie: cookie } : {}), ...headers },
      body: body === undefined ? undefined : JSON.stringify(body) });
    const sc = res.headers.get("set-cookie");
    if (sc) cookie = sc.split(";")[0];
    const ct = res.headers.get("content-type") || "";
    return { status: res.status, body: ct.includes("json") ? await res.json() : await res.text(), headers: res.headers };
  };
}

const report = (over = {}) => ({ machine: "SIM1-PC", app_version: "1.11.0", heartbeat: Date.now() / 1000, files: 12,
  version: { number: 3, title: "Imperial units", created_at: Date.now() / 1000 - 3600 }, mismatches: [],
  rulebook_violations: [], alerts_24h: 0, recent_alerts: [], pending_proposals: 0,
  system: { gpu: "NVIDIA RTX 4080\n  Driver version : 32.0.15.6603" }, ...over });

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

test("passwords", () => {
  const h = hashPassword("x");
  assert.ok(checkPassword(h, "x") && !checkPassword(h, "y") && checkPassword("plain", "plain"));
});

test("uptime buckets", () => {
  const now = Date.now();
  const samples = [];
  for (let t = now - 3 * 3_600_000; t <= now; t += 5 * 60_000) samples.push({ ts: t });
  const u = uptime(samples, now);
  assert.equal(u.days.length, 7);
  assert.equal(u.last24h.slice(-3).every(Boolean), true);
  assert.equal(u.last24h[0], 0);
});

test("full flow: auth, sims, reports, events, webhooks, maintenance, notes, csv, kiosk", async () => {
  const { hub, base, hooks, close } = await startHub();
  try {
    const anon = client(base);
    assert.equal((await anon("/api/sims")).status, 401);
    assert.equal((await anon("/health")).body.ok, true);
    const me = await anon("/api/me");
    assert.equal(me.body.user, null);

    // login: wrong + right; CSRF header required for writes
    assert.equal((await anon("/api/login", { method: "POST", body: { name: "admin", password: "nope" } })).status, 401);
    const admin = client(base);
    assert.equal((await admin("/api/login", { method: "POST", body: { name: "admin", password: "secret" } })).status, 200);
    const add = await admin("/api/sims", { method: "POST", body: { name: "Sim 1" } });
    assert.equal(add.status, 200);
    const key = add.body.key;
    assert.match(key, /^hub_/);
    assert.equal((await admin("/api/sims", { method: "POST", body: { name: "sim 1" } })).status, 409);
    const noCsrf = await admin("/api/sims", { method: "POST", body: { name: "Sim 9" }, headers: { "X-Requested-With": "" } });
    assert.equal(noCsrf.status, 403);

    // viewer can read, not write
    const viewer = client(base);
    await viewer("/api/login", { method: "POST", body: { name: "office", password: "view-pw" } });
    assert.equal((await viewer("/api/sims")).status, 200);
    assert.equal((await viewer("/api/sims", { method: "POST", body: { name: "x" } })).status, 403);

    // sim reports
    const push = (r, k = key) => fetch(base + "/api/v1/report", { method: "POST", headers: { Authorization: `Bearer ${k}`,
      "Content-Type": "application/json" }, body: JSON.stringify(r) });
    assert.equal((await push(report(), "hub_wrong")).status, 401);
    assert.equal((await push(report())).status, 200);
    let list = (await admin("/api/sims")).body.sims;
    assert.equal(list[0].name, "Sim 1");
    assert.equal(list[0].state, "ok");
    assert.equal(list[0].ready, true);
    assert.equal(list[0].driver, "32.0.15.6603");

    // drift + rulebook + new alert + new version → events + webhooks
    await push(report({ version: { number: 4, title: "New textures" }, mismatches: [{ file: "Prepar3D.cfg", group: "P3D", status: "modified" }],
      rulebook_violations: ["Prepar3D.cfg: GRAPHICS/TEXTURE_MAX_LOAD"],
      recent_alerts: [{ ts: Date.now() / 1000, message: "[Texture] TEXTURE_MAX_LOAD = 2048" }] }));
    list = (await admin("/api/sims")).body.sims;
    assert.equal(list[0].state, "bad");
    assert.equal(list[0].ready, false);
    const evs = (await admin("/api/events")).body.events.map((e) => e.type);
    for (const t of ["version", "drift", "rulebook", "alert", "online"]) assert.ok(evs.includes(t), `${t} in ${evs}`);
    await wait(200);
    assert.ok(hooks.some((h) => h.body.includes("Rulebook broken")), JSON.stringify(hooks));
    assert.ok(hooks.every((h) => h.ct.includes("application/json")));
    const nHooks = hooks.length;

    // maintenance (tech) mutes webhooks; notes
    const tech = client(base);
    await tech("/api/login", { method: "POST", body: { name: "tech", password: "tech-pw" } });
    const id = list[0].id;
    assert.equal((await tech(`/api/sims/${id}/maintenance`, { method: "POST", body: { on: true, note: "Projector lamp" } })).status, 200);
    await push(report({ rulebook_violations: ["dll.xml: FSUIPC"] }));
    await wait(200);
    assert.equal(hooks.length, nHooks, "muted during maintenance");
    assert.equal((await tech(`/api/sims/${id}/notes`, { method: "POST", body: { text: "Lamp replaced", category: "hardware" } })).status, 200);
    assert.equal((await viewer(`/api/sims/${id}/notes`, { method: "POST", body: { text: "x" } })).status, 403);
    const detail = (await viewer(`/api/sims/${id}`)).body;
    assert.equal(detail.notes[0].text, "Lamp replaced");
    assert.equal(detail.summary.maintenance.on, true);
    assert.equal(detail.uptime.days.length, 7);
    assert.ok(detail.report.machine === "SIM1-PC");

    // offline detection
    hub.store.sims[id].lastSeen = Date.now() - 20 * 60_000;
    hub.checkOffline();
    list = (await admin("/api/sims")).body.sims;
    assert.equal(list[0].state, "offline");
    assert.ok((await admin("/api/events")).body.events.some((e) => e.type === "offline"));

    // csv + kiosk + rotate + delete
    const csv = await viewer("/api/export.csv");
    assert.match(csv.body, /^Sim,Status,Ready/);
    assert.match(csv.body, /Sim 1,offline,NO-GO/);
    const kiosk = client(base);
    assert.equal((await kiosk("/kiosk?token=nope")).status, 403);
    const k = await kiosk("/kiosk?token=wall-123");
    assert.equal(k.status, 302);
    assert.equal((await kiosk("/api/sims")).status, 200);
    const rot = await admin(`/api/sims/${id}/rotate`, { method: "POST" });
    assert.equal((await push(report())).status, 401, "old key no longer works");
    assert.equal((await push(report(), rot.body.key)).status, 200);
    assert.equal((await admin(`/api/sims/${id}`, { method: "DELETE" })).status, 200);
    assert.equal((await admin("/api/sims")).body.sims.length, 0);

    // static UI + security headers
    const page = await fetch(base + "/");
    assert.equal(page.status, 200);
    assert.match(page.headers.get("content-security-policy"), /default-src 'self'/);
    assert.equal((await fetch(base + "/../src/server.js")).status, 404);

    // persistence: restart keeps sims
    await admin("/api/sims", { method: "POST", body: { name: "Sim 2" } });
    hub.store.saveNow();
    const dir = hub.cfg.dataDir;
    await close();
    const hub2 = createHub({ dataDir: dir, quiet: true, users: [] });
    assert.equal(Object.values(hub2.store.sims)[0].name, "Sim 2");
    await hub2.close();
  } catch (e) {
    await close().catch(() => {});
    throw e;
  }
});
