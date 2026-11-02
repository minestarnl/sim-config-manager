#!/usr/bin/env node
// SimConfigManager Hub — optional central dashboard. No dependencies: Node 18.18+ only.
//   node src/server.js                start (config.json / env vars, see README)
//   node src/server.js hash-password  print a hashed password for config.json
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";
import { loadConfig, checkPassword, hashPassword } from "./config.js";
import { Store } from "./store.js";
import { sendWebhooks } from "./notify.js";
import { explain } from "./explain.js";

export const HUB_VERSION = "1.1.0";
const PUBLIC = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "public");
const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".ttf": "font/ttf", ".svg": "image/svg+xml", ".json": "application/json",
  ".png": "image/png", ".ico": "image/x-icon", ".jpg": "image/jpeg", ".webp": "image/webp" };
const ROLE_RANK = { viewer: 1, tech: 2, admin: 3 };

const toMs = (ts) => (!ts ? 0 : ts < 1e12 ? Math.round(ts * 1000) : ts);

// --------------------------------------------------------------------------- state derived from a report

export function simState(sim, now, offlineMs) {
  const r = sim.report || {};
  const online = !!sim.lastSeen && now - sim.lastSeen < offlineMs;
  const mism = r.mismatches || [];
  const viol = r.rulebook_violations || [];
  const problems = [];
  if (!sim.lastSeen) problems.push("Never reported");
  else if (!online) problems.push("Offline");
  if (!r.version && sim.lastSeen) problems.push("No version yet");
  if (mism.length) problems.push(`${mism.length} file(s) differ from v${r.version?.number ?? "?"}`);
  if (viol.length) problems.push(`Rulebook broken: ${viol.join(", ")}`);
  if (r.pending_proposals) problems.push(`${r.pending_proposals} proposed change(s) waiting for approval`);
  const hw = r.hardware_missing || [];
  if (hw.length) problems.push(`Cockpit hardware missing: ${hw.join(", ")}`);
  const overdue = r.maintenance_overdue || [];
  if (overdue.length) problems.push(`Maintenance overdue: ${overdue.join(", ")}`);
  for (const w of (r.health_warnings || []).slice(0, 3)) problems.push(`Health: ${w}`);
  if ((r.integrity_problems || []).length) problems.push(`Integrity check found differences: ${r.integrity_problems.join(", ")}`);
  if (r.last_session?.crashed) problems.push("The last session ended with a crash");
  let state = "ok";
  if (!online) state = "offline";
  else if (viol.length || hw.length || mism.some((m) => m.status === "missing")) state = "bad";
  else if (mism.length || !r.version || overdue.length || (r.health_warnings || []).length ||
    (r.integrity_problems || []).length || r.last_session?.crashed) state = "warn";
  const ready = online && !sim.maintenance?.on && !!r.version && !mism.length && !viol.length && !hw.length;
  return { online, state, problems, ready };
}

function gpuDriver(r) {
  const g = (r?.system?.gpu || "").split("\n");
  const line = g.find((l) => l.includes("Driver version"));
  return line ? line.split(":").slice(1).join(":").trim() : "";
}

export function summary(sim, now, offlineMs) {
  const r = sim.report || {};
  const st = simState(sim, now, offlineMs);
  return {
    id: sim.id, name: sim.name, machine: r.machine || "", lastSeen: sim.lastSeen, lastIp: sim.lastIp,
    online: st.online, state: st.state, ready: st.ready, problems: st.problems, maintenance: sim.maintenance,
    version: r.version || null, mismatches: (r.mismatches || []).length,
    violations: (r.rulebook_violations || []).length, alerts24h: r.alerts_24h || 0,
    pending: r.pending_proposals || 0, files: r.files_count ?? r.files ?? 0, appVersion: r.app_version || "",
    driver: gpuDriver(r), lastChange: toMs(r.last_change), notes: (sim.notes || []).length,
    hardwareMissing: r.hardware_missing || [], maintenanceOverdue: r.maintenance_overdue || [],
    maintenanceSoon: r.maintenance_soon || [], healthWarnings: r.health_warnings || [],
    lastSession: r.last_session || null, simHours: r.sim_hours || 0,
  };
}

// --------------------------------------------------------------------------- uptime from samples

export function uptime(samples, now, days = 7, slotMin = 5) {
  const slot = slotMin * 60_000;
  const seen = new Set(samples.map((s) => Math.floor(s.ts / slot)));
  const out = [];
  const dayMs = 86_400_000;
  const startToday = new Date(now); startToday.setHours(0, 0, 0, 0);
  for (let d = days - 1; d >= 0; d--) {
    const from = startToday.getTime() - d * dayMs;
    const to = Math.min(from + dayMs, now);
    let total = 0, up = 0;
    for (let t = from; t < to; t += slot) { total++; if (seen.has(Math.floor(t / slot))) up++; }
    out.push({ day: new Date(from).toISOString().slice(0, 10), pct: total ? Math.round((up / total) * 1000) / 10 : 0 });
  }
  const hours = [];
  for (let h = 23; h >= 0; h--) {
    const from = now - (h + 1) * 3_600_000;
    let up = false;
    for (let t = from; t < from + 3_600_000; t += slot) if (seen.has(Math.floor(t / slot))) { up = true; break; }
    hours.push(up ? 1 : 0);
  }
  return { days: out, last24h: hours };
}

// --------------------------------------------------------------------------- the app

export function createHub(cfgOverrides = {}) {
  const cfg = loadConfig(cfgOverrides);
  const store = new Store(cfg.dataDir);
  const offlineMs = (cfg.offlineAfterMinutes || 15) * 60_000;
  const sessions = new Map();          // token -> {name, role, exp}
  const loginFails = new Map();        // ip -> {n, until}
  const streams = new Set();           // SSE responses
  const log = (...a) => { if (!cfg.quiet) console.log(new Date().toISOString(), ...a); };

  if (!cfg.users?.length) {
    log("WARNING: no users configured — set SCM_HUB_ADMIN_PASSWORD or add users to config.json");
  }

  // ----- events + notifications
  function emit(simId, type, message, level = "info", extra = {}) {
    const sim = store.sims[simId];
    const ev = store.addEvent({ sim: simId, simName: sim?.name, type, level, message, ...extra });
    broadcast("event", ev);
    const muted = sim?.maintenance?.on && type !== "maintenance";
    if (!muted) sendWebhooks(cfg.webhooks, ev, cfg.title, log);
    return ev;
  }

  function broadcast(kind, data) {
    const msg = `event: ${kind}\ndata: ${JSON.stringify(data)}\n\n`;
    for (const res of streams) { try { res.write(msg); } catch { streams.delete(res); } }
  }

  function diffReports(sim, prev, cur) {
    const id = sim.id;
    if (!prev) { emit(id, "online", `${sim.name} reported for the first time`); return; }
    const pv = prev.version?.number, cv = cur.version?.number;
    if (cv && cv !== pv) emit(id, "version", `Version v${cv} created: ${cur.version.title || ""}`.trim());
    const pm = (prev.mismatches || []).length, cm = (cur.mismatches || []).length;
    if (cm && (!pm || cv !== pv && cm)) {
      emit(id, "drift", `${cm} file(s) differ from v${cv ?? "?"}: ` +
        (cur.mismatches || []).slice(0, 5).map((m) => m.file).join(", "), "warning");
    } else if (pm && !cm) {
      emit(id, "drift", `All files match v${cv ?? "?"} again`);
    }
    const pvSet = new Set(prev.rulebook_violations || []);
    const cvSet = new Set(cur.rulebook_violations || []);
    for (const v of cvSet) if (!pvSet.has(v)) emit(id, "rulebook", `Rulebook broken: ${v}`, "alert");
    for (const v of pvSet) if (!cvSet.has(v)) emit(id, "rulebook", `Rulebook OK again: ${v}`);
    const lastAlert = Math.max(0, ...(prev.recent_alerts || []).map((a) => toMs(a.ts)));
    for (const a of (cur.recent_alerts || []).slice().reverse()) {
      if (toMs(a.ts) > lastAlert) emit(id, "alert", a.message, "alert", { at: toMs(a.ts) });
    }
    const ph = new Set(prev.hardware_missing || []), ch = new Set(cur.hardware_missing || []);
    for (const h of ch) if (!ph.has(h)) emit(id, "hardware", `Cockpit hardware missing: ${h}`, "alert");
    for (const h of ph) if (!ch.has(h)) emit(id, "hardware", `Cockpit hardware connected again: ${h}`);
    const po = new Set(prev.maintenance_overdue || []);
    for (const t of cur.maintenance_overdue || []) if (!po.has(t)) emit(id, "maintenance", `Maintenance overdue: ${t}`, "warning");
    if (cur.last_session?.crashed && cur.last_session.started !== prev.last_session?.started) {
      emit(id, "crash", "The sim crashed during the last session", "alert");
    }
    if ((cur.pending_proposals || 0) > (prev.pending_proposals || 0)) {
      emit(id, "proposal", `${cur.pending_proposals} AI-proposed change(s) waiting for approval`);
    }
  }

  function ingest(sim, report, ip) {
    const now = Date.now();
    const wasOnline = sim.online;
    const prev = sim.report;
    sim.report = report;
    sim.lastSeen = now;
    sim.lastIp = ip;
    sim.online = true;
    if (prev && !wasOnline) emit(sim.id, "online", `${sim.name} is back online`);
    diffReports(sim, prev, report);
    const st = simState(sim, now, offlineMs);
    store.addSample(sim.id, { ts: now, state: st.state, v: report.version?.number || null,
      m: (report.mismatches || []).length, r: (report.rulebook_violations || []).length });
    store.save();
    broadcast("sim", summary(sim, now, offlineMs));
  }

  function checkOffline() {
    const now = Date.now();
    for (const sim of Object.values(store.sims)) {
      if (sim.online && now - sim.lastSeen > offlineMs) {
        sim.online = false;
        emit(sim.id, "offline", `${sim.name} stopped reporting (last seen ${new Date(sim.lastSeen).toLocaleString()})`,
          "alert");
        broadcast("sim", summary(sim, now, offlineMs));
        store.save();
      }
    }
  }

  // ----- http helpers
  const send = (res, code, obj, headers = {}) => {
    const body = typeof obj === "string" || Buffer.isBuffer(obj) ? obj : JSON.stringify(obj);
    res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
      ...headers });
    res.end(body);
  };
  const clientIp = (req) => (cfg.trustProxy && req.headers["x-forwarded-for"]
    ? String(req.headers["x-forwarded-for"]).split(",")[0].trim() : req.socket.remoteAddress || "");
  const isHttps = (req) => req.socket.encrypted || (cfg.trustProxy && req.headers["x-forwarded-proto"] === "https");

  function readBody(req, limit) {
    return new Promise((resolve, reject) => {
      let size = 0; const chunks = [];
      req.on("data", (c) => {
        size += c.length;
        if (size > limit) { reject(Object.assign(new Error("Body too large"), { code: 413 })); req.destroy(); }
        else chunks.push(c);
      });
      req.on("end", () => resolve(Buffer.concat(chunks)));
      req.on("error", reject);
    });
  }
  async function json(req, limit = 64 * 1024) {
    const raw = await readBody(req, limit);
    try { return raw.length ? JSON.parse(raw.toString("utf8")) : {}; }
    catch { throw Object.assign(new Error("Invalid JSON"), { code: 400 }); }
  }

  function cookies(req) {
    const out = {};
    for (const part of String(req.headers.cookie || "").split(";")) {
      const i = part.indexOf("=");
      if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
    }
    return out;
  }

  function session(req) {
    const t = cookies(req).scm_session;
    const s = t && sessions.get(t);
    if (!s) return null;
    if (s.exp < Date.now()) { sessions.delete(t); return null; }
    return s;
  }

  function newSession(res, req, user, days = 7) {
    const token = crypto.randomBytes(32).toString("base64url");
    sessions.set(token, { name: user.name, role: user.role || "viewer", exp: Date.now() + days * 86_400_000 });
    res.setHeader("Set-Cookie", `scm_session=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${days * 86400}` +
      (isHttps(req) ? "; Secure" : ""));
  }

  function need(req, res, role) {
    const s = session(req);
    if (!s) { send(res, 401, { error: "Please sign in." }); return null; }
    if (ROLE_RANK[s.role] < ROLE_RANK[role]) { send(res, 403, { error: `Needs the '${role}' role.` }); return null; }
    if (req.method !== "GET" && req.headers["x-requested-with"] !== "scm") {
      send(res, 403, { error: "Missing request header." }); return null;  // CSRF guard
    }
    return s;
  }

  function serveStatic(req, res, urlPath) {
    let p = urlPath === "/" || urlPath === "/kiosk" ? "/index.html" : urlPath;
    const file = path.normalize(path.join(PUBLIC, p));
    if (!file.startsWith(PUBLIC) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      return send(res, 404, { error: "Not found" });
    }
    res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "application/octet-stream",
      "Cache-Control": p.endsWith(".ttf") ? "public, max-age=604800" : "no-cache" });
    fs.createReadStream(file).pipe(res);
  }

  function csv(rows) {
    const esc = (v) => { const s = v == null ? "" : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
    return rows.map((r) => r.map(esc).join(",")).join("\r\n") + "\r\n";
  }

  // ----- routes
  async function handle(req, res) {
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("Referrer-Policy", "no-referrer");
    res.setHeader("X-Frame-Options", "DENY");
    res.setHeader("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; " +
      "script-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'");
    const url = new URL(req.url, "http://hub");
    const p = url.pathname;
    const now = Date.now();
    let m;

    if (p === "/health") return send(res, 200, { ok: true, hub: HUB_VERSION, sims: Object.keys(store.sims).length });

    // -- sims report in (the SimConfigManager app)
    if (p === "/api/v1/report" && req.method === "POST") {
      const auth = String(req.headers.authorization || "");
      const sim = store.simByKey(auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "");
      if (!sim) return send(res, 401, { error: "Unknown hub key. Create the sim in the hub (Sims → Add sim)." });
      const report = await json(req, (cfg.maxReportKB || 2048) * 1024);
      if (typeof report !== "object" || Array.isArray(report)) return send(res, 400, { error: "Expected an object" });
      ingest(sim, report, clientIp(req));
      const commands = sim.commands || [];
      if (commands.length) { sim.commands = []; store.save(); }   // delivered once; the app logs what it applied
      return send(res, 200, { ok: true, sim: sim.name, maintenance: sim.maintenance?.on || false, serverTime: now,
        commands });
    }

    // -- auth
    if (p === "/api/login" && req.method === "POST") {
      const ip = clientIp(req);
      const f = loginFails.get(ip);
      if (f && f.n >= 8 && f.until > now) return send(res, 429, { error: "Too many attempts — wait a few minutes." });
      const body = await json(req);
      const user = (cfg.users || []).find((u) => u.name === body.name);
      if (!user || !checkPassword(user.password, String(body.password || ""))) {
        loginFails.set(ip, { n: (f && f.until > now ? f.n : 0) + 1, until: now + 15 * 60_000 });
        return send(res, 401, { error: "Wrong name or password." });
      }
      loginFails.delete(ip);
      newSession(res, req, user);
      return send(res, 200, { name: user.name, role: user.role || "viewer" });
    }
    if (p === "/api/logout" && req.method === "POST") {
      const t = cookies(req).scm_session;
      if (t) sessions.delete(t);
      res.setHeader("Set-Cookie", "scm_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0");
      return send(res, 200, { ok: true });
    }
    if (p === "/kiosk" && url.searchParams.get("token")) {
      const tok = url.searchParams.get("token");
      if (!cfg.kioskToken || tok.length !== cfg.kioskToken.length ||
          !crypto.timingSafeEqual(Buffer.from(tok), Buffer.from(cfg.kioskToken))) {
        return send(res, 403, { error: "Wrong kiosk token" });
      }
      newSession(res, req, { name: "Wall display", role: "viewer" }, 365);
      res.writeHead(302, { Location: "/#/board" });
      return res.end();
    }
    if (p === "/api/me") {
      const s = session(req);
      return send(res, 200, { user: s ? { name: s.name, role: s.role } : null, title: cfg.title, hub: HUB_VERSION,
        offlineAfterMinutes: cfg.offlineAfterMinutes, usersConfigured: !!cfg.users?.length,
        ai: !!cfg.anthropicApiKey });
    }

    // -- read (viewer+)
    if (p === "/api/sims" && req.method === "GET") {
      if (!need(req, res, "viewer")) return;
      const list = Object.values(store.sims).map((s) => summary(s, now, offlineMs))
        .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
      return send(res, 200, { sims: list, serverTime: now });
    }
    if ((m = p.match(/^\/api\/sims\/([a-f0-9]+)$/)) && req.method === "GET") {
      if (!need(req, res, "viewer")) return;
      const sim = store.sims[m[1]];
      if (!sim) return send(res, 404, { error: "No such sim" });
      return send(res, 200, { summary: summary(sim, now, offlineMs), report: sim.report, notes: sim.notes || [],
        uptime: uptime(store.samples(sim.id, now - 8 * 86_400_000), now), events: store.events({ sim: sim.id, limit: 100 }) });
    }
    if (p === "/api/events" && req.method === "GET") {
      if (!need(req, res, "viewer")) return;
      return send(res, 200, { events: store.events({ sim: url.searchParams.get("sim") || undefined,
        level: url.searchParams.get("level") || undefined, limit: Math.min(1000, Number(url.searchParams.get("limit")) || 200) }) });
    }
    if (p === "/api/stream" && req.method === "GET") {
      if (!session(req)) return send(res, 401, { error: "Please sign in." });
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store", Connection: "keep-alive" });
      res.write("retry: 5000\n\n");
      streams.add(res);
      req.on("close", () => streams.delete(res));
      return;
    }
    if (p === "/api/export.csv" && req.method === "GET") {
      if (!need(req, res, "viewer")) return;
      const rows = [["Sim", "Status", "Ready", "Online", "Last seen", "Config version", "Version title", "Files differ",
        "Rulebook broken", "Alerts 24h", "Waiting approval", "Maintenance", "App", "Graphics driver", "Problems"]];
      for (const s of Object.values(store.sims).map((x) => summary(x, now, offlineMs))) {
        rows.push([s.name, s.state, s.ready ? "GO" : "NO-GO", s.online ? "yes" : "no",
          s.lastSeen ? new Date(s.lastSeen).toISOString() : "", s.version ? `v${s.version.number}` : "",
          s.version?.title || "", s.mismatches, s.violations, s.alerts24h, s.pending,
          s.maintenance?.on ? (s.maintenance.note || "yes") : "", s.appVersion, s.driver, s.problems.join("; ")]);
      }
      return send(res, 200, csv(rows), { "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": `attachment; filename="fleet-${new Date().toISOString().slice(0, 10)}.csv"` });
    }

    // -- tech+: logbook, maintenance
    if ((m = p.match(/^\/api\/sims\/([a-f0-9]+)\/notes$/)) && req.method === "POST") {
      const s = need(req, res, "tech"); if (!s) return;
      const sim = store.sims[m[1]];
      if (!sim) return send(res, 404, { error: "No such sim" });
      const body = await json(req, 9 * 1024 * 1024);
      const text = String(body.text || "").trim().slice(0, 4000);
      if (!text) return send(res, 400, { error: "Empty note" });
      const note = { ts: now, author: s.name, text, category: String(body.category || "note").slice(0, 30) };
      if (body.photo) note.photo = store.savePhoto(String(body.photo));
      (sim.notes ||= []).unshift(note);
      store.save();
      emit(sim.id, "note", `${s.name}: ${text.slice(0, 140)}`);
      return send(res, 200, { ok: true, note });
    }
    if ((m = p.match(/^\/api\/photos\/([a-f0-9]+\.(?:jpg|png|webp))$/)) && req.method === "GET") {
      if (!need(req, res, "viewer")) return;
      const file = store.photoPath(m[1]);
      if (!file) return send(res, 404, { error: "Not found" });
      res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "image/jpeg", "Cache-Control": "private, max-age=86400" });
      return fs.createReadStream(file).pipe(res);
    }
    if ((m = p.match(/^\/api\/sims\/([a-f0-9]+)\/maintenance-done$/)) && req.method === "POST") {
      const s = need(req, res, "tech"); if (!s) return;
      const sim = store.sims[m[1]];
      if (!sim) return send(res, 404, { error: "No such sim" });
      const body = await json(req);
      const task = (sim.report?.maintenance || []).find((t) => String(t.id) === String(body.task));
      if (!task) return send(res, 404, { error: "No such maintenance task" });
      (sim.commands ||= []).push({ id: crypto.randomBytes(6).toString("hex"), type: "maintenance_done", task: task.id,
        by: s.name, note: String(body.note || "").slice(0, 300) });
      task.state = "ok"; task.last_done = now / 1000;
      sim.report.maintenance_overdue = (sim.report.maintenance_overdue || []).filter((x) => x !== task.name);
      store.save();
      emit(sim.id, "maintenance", `Maintenance done: ${task.name} (${s.name})`);
      broadcast("sim", summary(sim, now, offlineMs));
      return send(res, 200, { ok: true, queued: true });
    }
    if ((m = p.match(/^\/api\/sims\/([a-f0-9]+)\/explain$/)) && req.method === "GET") {
      if (!need(req, res, "viewer")) return;
      const sim = store.sims[m[1]];
      if (!sim) return send(res, 404, { error: "No such sim" });
      return send(res, 200, await explain(cfg, sim, summary(sim, now, offlineMs), log));
    }
    if ((m = p.match(/^\/api\/sims\/([a-f0-9]+)\/maintenance$/)) && req.method === "POST") {
      const s = need(req, res, "tech"); if (!s) return;
      const sim = store.sims[m[1]];
      if (!sim) return send(res, 404, { error: "No such sim" });
      const body = await json(req);
      sim.maintenance = body.on ? { on: true, note: String(body.note || "").slice(0, 300), by: s.name, since: now }
        : { on: false };
      store.save();
      emit(sim.id, "maintenance", body.on ? `Maintenance mode ON (${s.name}${sim.maintenance.note ? ": " +
        sim.maintenance.note : ""}) — notifications muted` : `Maintenance mode OFF (${s.name})`);
      broadcast("sim", summary(sim, now, offlineMs));
      return send(res, 200, { ok: true, maintenance: sim.maintenance });
    }

    // -- admin: manage sims
    if (p === "/api/sims" && req.method === "POST") {
      const s = need(req, res, "admin"); if (!s) return;
      const body = await json(req);
      const name = String(body.name || "").trim();
      if (!name) return send(res, 400, { error: "Give the sim a name" });
      if (Object.values(store.sims).some((x) => x.name.toLowerCase() === name.toLowerCase())) {
        return send(res, 409, { error: "A sim with that name already exists" });
      }
      const { sim, key } = store.createSim(name);
      store.addEvent({ sim: sim.id, simName: sim.name, type: "admin", message: `Sim added by ${s.name}` });
      return send(res, 200, { id: sim.id, name: sim.name, key });
    }
    if ((m = p.match(/^\/api\/sims\/([a-f0-9]+)\/rotate$/)) && req.method === "POST") {
      const s = need(req, res, "admin"); if (!s) return;
      if (!store.sims[m[1]]) return send(res, 404, { error: "No such sim" });
      const key = store.rotateKey(m[1]);
      store.addEvent({ sim: m[1], simName: store.sims[m[1]].name, type: "admin", message: `New hub key made by ${s.name}` });
      return send(res, 200, { key });
    }
    if ((m = p.match(/^\/api\/sims\/([a-f0-9]+)$/)) && req.method === "DELETE") {
      const s = need(req, res, "admin"); if (!s) return;
      const sim = store.sims[m[1]];
      if (!sim) return send(res, 404, { error: "No such sim" });
      store.addEvent({ sim: sim.id, simName: sim.name, type: "admin", message: `Sim removed by ${s.name}` });
      store.deleteSim(sim.id);
      broadcast("removed", { id: sim.id });
      return send(res, 200, { ok: true });
    }
    if (p === "/api/test-webhooks" && req.method === "POST") {
      const s = need(req, res, "admin"); if (!s) return;
      await sendWebhooks(cfg.webhooks, { type: "alert", level: "info", simName: "", message:
        `Test notification from ${cfg.title} hub (sent by ${s.name})` }, cfg.title, log);
      return send(res, 200, { ok: true, webhooks: (cfg.webhooks || []).length });
    }

    if (p.startsWith("/api/")) return send(res, 404, { error: "Unknown endpoint" });
    if (req.method === "GET") return serveStatic(req, res, p);
    return send(res, 405, { error: "Method not allowed" });
  }

  const server = http.createServer((req, res) => {
    handle(req, res).catch((e) => {
      if (!res.headersSent) send(res, e.code && Number.isInteger(e.code) ? e.code : 500, { error: e.message });
      else res.end();
    });
  });
  const timers = [setInterval(checkOffline, 30_000),
    setInterval(() => { for (const id of Object.keys(store.sims)) store.pruneSamples(id); }, 86_400_000),
    setInterval(() => broadcast("ping", { t: Date.now() }), 25_000)];
  timers.forEach((t) => t.unref());

  return {
    cfg, store, server, checkOffline,
    listen(port = cfg.port, host = cfg.host) {
      return new Promise((resolve) => server.listen(port, host, () => resolve(server.address())));
    },
    close() {
      timers.forEach(clearInterval);
      for (const r of streams) { try { r.end(); } catch { /* */ } }
      store.saveNow();
      return new Promise((resolve) => server.close(() => resolve()));
    },
  };
}

// --------------------------------------------------------------------------- CLI
const isMain = process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (isMain) {
  if (process.argv[2] === "hash-password") {
    const pw = process.argv[3];
    if (!pw) { console.error("usage: node src/server.js hash-password <password>"); process.exit(1); }
    console.log(hashPassword(pw));
  } else {
    const hub = createHub();
    hub.listen().then((a) => {
      console.log(`SimConfigManager Hub ${HUB_VERSION} — http://${a.address === "0.0.0.0" ? "localhost" : a.address}:${a.port}`);
      console.log(`data: ${hub.cfg.dataDir}${hub.cfg.configFile ? `   config: ${hub.cfg.configFile}` : "   (no config.json — using defaults/env)"}`);
    });
    const stop = () => hub.close().then(() => process.exit(0));
    process.on("SIGINT", stop);
    process.on("SIGTERM", stop);
  }
}
