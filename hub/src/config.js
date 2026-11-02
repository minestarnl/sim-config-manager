// Configuration: config.json (path in SCM_HUB_CONFIG or ./config.json) + environment variables.
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const DEFAULTS = {
  port: 8780,
  host: "0.0.0.0",
  dataDir: "./data",
  title: "Sim Center",
  offlineAfterMinutes: 15,
  trustProxy: false,
  users: [],
  kioskToken: "",
  webhooks: [],
  maxReportKB: 2048,
  anthropicApiKey: "",                 // optional: "Why is this sim NO-GO?" answered by Claude
  anthropicModel: "claude-sonnet-5-5",
};

export function loadConfig(overrides = {}) {
  const file = process.env.SCM_HUB_CONFIG || path.resolve("config.json");
  let fromFile = {};
  if (fs.existsSync(file)) {
    fromFile = JSON.parse(fs.readFileSync(file, "utf8"));
  }
  const env = {};
  if (process.env.PORT) env.port = Number(process.env.PORT);
  if (process.env.HOST) env.host = process.env.HOST;
  if (process.env.SCM_HUB_DATA) env.dataDir = process.env.SCM_HUB_DATA;
  if (process.env.SCM_HUB_TITLE) env.title = process.env.SCM_HUB_TITLE;
  if (process.env.SCM_HUB_KIOSK_TOKEN) env.kioskToken = process.env.SCM_HUB_KIOSK_TOKEN;
  if (process.env.SCM_HUB_TRUST_PROXY) env.trustProxy = process.env.SCM_HUB_TRUST_PROXY === "1";
  if (process.env.ANTHROPIC_API_KEY) env.anthropicApiKey = process.env.ANTHROPIC_API_KEY;
  const cfg = { ...DEFAULTS, ...fromFile, ...env, ...overrides };
  // Quick start: ADMIN_PASSWORD env var creates/overrides the "admin" user.
  if (process.env.SCM_HUB_ADMIN_PASSWORD) {
    cfg.users = [...(cfg.users || []).filter((u) => u.name !== "admin"),
      { name: "admin", password: process.env.SCM_HUB_ADMIN_PASSWORD, role: "admin" }];
  }
  cfg.dataDir = path.resolve(path.dirname(fs.existsSync(file) ? file : "."), cfg.dataDir);
  cfg.configFile = fs.existsSync(file) ? file : null;
  return cfg;
}

// Passwords may be stored plain (convenient) or as "scrypt$salt$hash" (node src/server.js hash-password).
export function hashPassword(pw) {
  const salt = crypto.randomBytes(16).toString("hex");
  const hash = crypto.scryptSync(pw, salt, 32).toString("hex");
  return `scrypt$${salt}$${hash}`;
}

export function checkPassword(stored, given) {
  if (typeof stored !== "string" || typeof given !== "string") return false;
  if (stored.startsWith("scrypt$")) {
    const [, salt, hash] = stored.split("$");
    const h = crypto.scryptSync(given, salt, 32);
    const want = Buffer.from(hash, "hex");
    return want.length === h.length && crypto.timingSafeEqual(want, h);
  }
  const a = Buffer.from(stored), b = Buffer.from(given);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
