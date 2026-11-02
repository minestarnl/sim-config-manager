// Small JSON-file store: no database needed. Everything lives in dataDir.
//   sims.json            sims (key hashes, latest report, maintenance, logbook)
//   events.jsonl         fleet-wide event feed (append-only)
//   samples/<id>.jsonl   heartbeat samples per sim (for uptime)
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

export const sha256 = (s) => crypto.createHash("sha256").update(s).digest("hex");

export class Store {
  constructor(dir) {
    this.dir = dir;
    fs.mkdirSync(path.join(dir, "samples"), { recursive: true });
    this.simsFile = path.join(dir, "sims.json");
    this.eventsFile = path.join(dir, "events.jsonl");
    this.sims = fs.existsSync(this.simsFile) ? JSON.parse(fs.readFileSync(this.simsFile, "utf8")) : {};
    this._saveTimer = null;
    this.recentEvents = this._loadRecentEvents(2000);
  }

  // ----- persistence
  saveNow() {
    clearTimeout(this._saveTimer);
    this._saveTimer = null;
    const tmp = this.simsFile + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(this.sims));
    fs.renameSync(tmp, this.simsFile);
  }

  save() {
    if (!this._saveTimer) this._saveTimer = setTimeout(() => this.saveNow(), 500);
  }

  // ----- sims
  createSim(name) {
    const id = crypto.randomBytes(6).toString("hex");
    const key = "hub_" + crypto.randomBytes(24).toString("base64url");
    this.sims[id] = { id, name: String(name).slice(0, 80), keyHash: sha256(key), created: Date.now(),
      lastSeen: 0, lastIp: "", report: null, maintenance: { on: false }, notes: [], online: false };
    this.saveNow();
    return { sim: this.sims[id], key };
  }

  rotateKey(id) {
    const key = "hub_" + crypto.randomBytes(24).toString("base64url");
    this.sims[id].keyHash = sha256(key);
    this.saveNow();
    return key;
  }

  deleteSim(id) {
    delete this.sims[id];
    this.saveNow();
    try { fs.unlinkSync(path.join(this.dir, "samples", `${id}.jsonl`)); } catch { /* none */ }
  }

  simByKey(key) {
    if (!key) return null;
    const h = sha256(key);
    return Object.values(this.sims).find((s) => s.keyHash.length === h.length &&
      crypto.timingSafeEqual(Buffer.from(s.keyHash), Buffer.from(h))) || null;
  }

  // ----- events
  _loadRecentEvents(max) {
    if (!fs.existsSync(this.eventsFile)) return [];
    const lines = fs.readFileSync(this.eventsFile, "utf8").trim().split("\n").filter(Boolean);
    return lines.slice(-max).map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  }

  addEvent(ev) {
    const e = { ts: Date.now(), level: "info", ...ev };
    fs.appendFileSync(this.eventsFile, JSON.stringify(e) + "\n");
    this.recentEvents.push(e);
    if (this.recentEvents.length > 2000) this.recentEvents.splice(0, this.recentEvents.length - 2000);
    return e;
  }

  events({ sim, limit = 200, level } = {}) {
    let ev = this.recentEvents;
    if (sim) ev = ev.filter((e) => e.sim === sim);
    if (level) ev = ev.filter((e) => e.level === level);
    return ev.slice(-limit).sort((a, b) => b.ts - a.ts);
  }

  // ----- heartbeat samples (one line per report, compacted to ≥60 s apart)
  addSample(id, sample) {
    const f = path.join(this.dir, "samples", `${id}.jsonl`);
    const s = this.sims[id];
    if (s._lastSample && sample.ts - s._lastSample < 60_000) return;
    s._lastSample = sample.ts;
    fs.appendFileSync(f, JSON.stringify(sample) + "\n");
  }

  samples(id, sinceMs) {
    const f = path.join(this.dir, "samples", `${id}.jsonl`);
    if (!fs.existsSync(f)) return [];
    const out = [];
    for (const l of fs.readFileSync(f, "utf8").split("\n")) {
      if (!l) continue;
      try {
        const x = JSON.parse(l);
        if (x.ts >= sinceMs) out.push(x);
      } catch { /* skip */ }
    }
    return out;
  }

  pruneSamples(id, keepDays = 90) {
    const f = path.join(this.dir, "samples", `${id}.jsonl`);
    const keep = this.samples(id, Date.now() - keepDays * 86_400_000);
    fs.writeFileSync(f, keep.map((x) => JSON.stringify(x)).join("\n") + (keep.length ? "\n" : ""));
  }
}
