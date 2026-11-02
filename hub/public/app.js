// Sim Config Manager Hub — web dashboard (no framework, no build step).
const IC = { fleet: "", board: "", events: "", admin: "", check: "", warn: "",
  error: "", circle: "", tools: "", note: "", tag: "", refresh: "",
  signout: "", export: "", remote: "", clock: "", back: "", key: "", trash: "" };

let ME = null, INFO = {}, SIMS = [], EVENTS_BADGE = 0, es = null;

// ---------------------------------------------------------------- helpers
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "style") e.style.cssText = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k instanceof Node ? k : String(k));
  return e;
}
const ic = (name, cls = "") => el("i", { class: `ci ${cls}` }, IC[name] || IC.circle);
const $ = (s) => document.querySelector(s);
const ago = (ms) => {
  if (!ms) return "never";
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 60) return `${s}s ago`; if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`; return new Date(ms).toLocaleString();
};
const fmt = (ms) => (ms ? new Date(ms < 1e12 ? ms * 1000 : ms).toLocaleString() : "–");
const STATE = { ok: ["OK", "ok-t", "check"], warn: ["Attention", "warn-t", "warn"], bad: ["Problem", "bad-t", "error"],
  offline: ["Offline", "off-t", "circle"] };
const can = (role) => ME && ({ viewer: 1, tech: 2, admin: 3 }[ME.role] >= { viewer: 1, tech: 2, admin: 3 }[role]);

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Requested-With": "scm",
    ...(opts.headers || {}) }, body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body });
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (res.status === 401 && path !== "/api/login") { ME = null; render(); throw new Error("Signed out"); }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function modal(...content) {
  const m = $("#modal");
  m.replaceChildren(el("div", { class: "box" }, ...content));
  m.hidden = false;
  m.onclick = (e) => { if (e.target === m) closeModal(); };
}
const closeModal = () => { $("#modal").hidden = true; };

function setHead(title, subtitle = "") {
  $("#title").textContent = title;
  $("#subtitle").textContent = subtitle;
  document.title = `${title} — ${INFO.title || "Sim Config Manager Hub"}`;
}

// ---------------------------------------------------------------- chrome
function renderChrome() {
  const route = location.hash.slice(2).split("/")[0] || "fleet";
  const items = [["fleet", "fleet", "Fleet"], ["board", "board", "Readiness board"], ["events", "events", "Events"]];
  const nav = $("#activity");
  nav.replaceChildren(...items.map(([r, icon, t]) => el("a", { href: `#/${r}`, title: t,
    class: route === r || (r === "fleet" && route === "sim") ? "active" : "" }, ic(icon),
    r === "events" && EVENTS_BADGE ? el("span", { class: "badge" }, EVENTS_BADGE > 99 ? "99+" : EVENTS_BADGE) : null)),
    el("div", { class: "spacer" }),
    can("admin") ? el("a", { href: "#/admin", title: "Sims & settings", class: route === "admin" ? "active" : "" }, ic("admin")) : null);
  $("#userbox").replaceChildren(ME ? el("span", {}, `${ME.name} · ${ME.role}`) : "",
    ME && ME.name !== "Wall display" ? el("button", { class: "small", title: "Sign out", onclick: logout }, ic("signout")) : "");
  const on = SIMS.filter((s) => s.online).length;
  const ready = SIMS.filter((s) => s.ready).length;
  const att = SIMS.filter((s) => s.state === "warn" || s.state === "bad").length;
  $("#sb-left").replaceChildren(el("span", {}, ic("fleet"), ` ${INFO.title || "Hub"}`),
    el("span", {}, ic("check"), ` ${ready}/${SIMS.length} ready`),
    att ? el("span", {}, ic("warn"), ` ${att} need attention`) : "");
  $("#sb-right").replaceChildren(el("span", {}, `${on}/${SIMS.length} online`),
    el("span", {}, es && es.readyState === 1 ? "● live" : "○ connecting…"), el("span", {}, `Hub ${INFO.hub || ""}`));
}

// ---------------------------------------------------------------- views
function viewLogin(v) {
  setHead("Sign in", INFO.title || "");
  const name = el("input", { placeholder: "Name", autocomplete: "username" });
  const pw = el("input", { type: "password", placeholder: "Password", autocomplete: "current-password" });
  const err = el("div", { class: "bad-t small" });
  const go = async (e) => {
    e?.preventDefault();
    try { ME = await api("/api/login", { method: "POST", body: { name: name.value, password: pw.value } }); start(); }
    catch (x) { err.textContent = x.message; }
  };
  v.replaceChildren(el("form", { class: "login", onsubmit: go },
    el("h2", {}, INFO.title || "Sim Config Manager Hub"), el("div", { class: "muted" }, "Status of every simulator in one place."),
    name, pw, el("button", { class: "primary", type: "submit" }, "Sign in"), err,
    INFO.usersConfigured ? "" : el("div", { class: "warn-t small" },
      "No users are configured on this hub yet — set SCM_HUB_ADMIN_PASSWORD or add users to config.json.")));
  name.focus();
}

function simCard(s) {
  const [label, cls, icon] = STATE[s.state];
  return el("div", { class: `card ${s.state}`, onclick: () => (location.hash = `#/sim/${s.id}`) },
    el("div", { class: "name" }, ic(icon, cls), s.name, el("span", { class: "grow" }),
      s.maintenance?.on ? el("span", { class: "pill maint-t" }, "MAINTENANCE") : "",
      el("span", { class: `pill ${s.ready ? "ok-t" : "bad-t"}` }, s.ready ? "GO" : "NO-GO")),
    el("div", {}, s.version ? `v${s.version.number} – ${s.version.title || ""}` : el("span", { class: "muted" }, "No version yet")),
    s.problems.length ? el("ul", { class: `small ${cls}` }, s.problems.slice(0, 4).map((p) => el("li", {}, p)))
      : el("div", { class: "ok-t small" }, "All files match the current version · rulebook OK"),
    el("div", { class: "meta" }, `${s.online ? "Online" : "Last seen"} · ${ago(s.lastSeen)}`,
      s.driver ? ` · GPU driver ${s.driver}` : "", s.appVersion ? ` · app ${s.appVersion}` : ""));
}

function viewFleet(v) {
  setHead("Fleet", "Every sim at a glance");
  const n = (f) => SIMS.filter(f).length;
  const stats = [[SIMS.length, "Sims"], [n((s) => s.online), "Online"], [n((s) => s.ready), "Ready (GO)"],
    [n((s) => s.state === "warn" || s.state === "bad"), "Need attention"], [n((s) => s.maintenance?.on), "In maintenance"]];
  v.replaceChildren(
    el("div", { class: "stats" }, stats.map(([num, l]) => el("div", { class: "stat" }, el("div", { class: "n" }, num), el("div", { class: "l" }, l)))),
    el("div", { class: "row", style: "margin-bottom:10px" }, el("span", { class: "grow" }),
      el("a", { class: "btn", href: "/api/export.csv" }, ic("export"), " Export CSV")),
    SIMS.length ? el("div", { class: "grid" }, SIMS.map(simCard))
      : el("div", { class: "section" }, el("h3", {}, "No sims yet"),
        can("admin") ? el("div", {}, "Add your sims under ", el("a", { href: "#/admin" }, "Sims & settings"),
          ", then enter the Hub address and sim key in Sim Config Manager on each sim (Sharing → Hub).")
          : "An administrator has to add the sims first."));
}

function viewBoard(v) {
  setHead("Readiness board", "GO / NO-GO for the next session");
  const clock = el("span", { class: "clock" }, new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  v.replaceChildren(el("div", { class: "row", style: "margin-bottom:14px" }, el("span", { class: "grow" }), clock),
    el("div", { class: "board" }, SIMS.map((s) => {
      const cls = s.maintenance?.on ? "maint" : !s.online ? "offline" : s.ready ? "go" : "nogo";
      const big = s.maintenance?.on ? ["MAINTENANCE", "maint-t"] : !s.online ? ["OFFLINE", "off-t"]
        : s.ready ? ["GO", "ok-t"] : ["NO-GO", "bad-t"];
      return el("div", { class: `tile ${cls}` }, el("div", { class: "nm" }, s.name),
        el("div", { class: `big ${big[1]}` }, big[0]),
        el("div", { class: "muted" }, s.version ? `v${s.version.number} – ${s.version.title || ""}` : "No version yet"),
        s.maintenance?.on && s.maintenance.note ? el("div", { class: "maint-t" }, s.maintenance.note) : "",
        !s.ready && !s.maintenance?.on ? el("div", { class: "small bad-t" }, s.problems.slice(0, 2).join(" · ")) : "");
    })));
}

async function viewSim(v, id) {
  let d;
  try { d = await api(`/api/sims/${id}`); } catch (e) { v.replaceChildren(el("div", { class: "banner err" }, e.message)); return; }
  const s = d.summary, r = d.report || {};
  const [label, cls, icon] = STATE[s.state];
  setHead(s.name, s.online ? `online · last report ${ago(s.lastSeen)}` : `offline · last seen ${ago(s.lastSeen)}`);
  const sec = (title, ...kids) => el("div", { class: "section" }, el("h3", {}, title), ...kids);
  const maintBtn = can("tech") ? el("button", { onclick: async () => {
    const on = !s.maintenance?.on;
    const note = on ? prompt("Why is the sim in maintenance? (shown on the board)", "") : "";
    if (on && note === null) return;
    await api(`/api/sims/${id}/maintenance`, { method: "POST", body: { on, note } }); route();
  } }, ic("tools"), s.maintenance?.on ? " End maintenance" : " Maintenance mode") : "";

  const files = (r.files_status || []).filter((f) => f.status !== "match");
  const noteText = el("textarea", { placeholder: "e.g. 'Projector 2 lamp replaced' or 'Yoke recalibrated'" });
  const noteCat = el("select", {}, ["note", "hardware", "software", "calibration", "incident"].map((c) => el("option", { value: c }, c)));
  const up = d.uptime;
  v.replaceChildren(
    el("div", { class: "row", style: "margin-bottom:12px" },
      el("a", { href: "#/fleet", class: "btn" }, ic("back"), " Fleet"),
      el("span", { class: `pill ${cls}` }, label), el("span", { class: `pill ${s.ready ? "ok-t" : "bad-t"}` }, s.ready ? "GO" : "NO-GO"),
      el("span", { class: "grow" }), maintBtn),
    s.maintenance?.on ? el("div", { class: "banner maint" }, ic("tools"),
      ` In maintenance since ${fmt(s.maintenance.since)} (${s.maintenance.by})${s.maintenance.note ? " — " + s.maintenance.note : ""}. Notifications are muted.`) : "",
    el("div", { class: "stats" }, [
      [s.version ? `v${s.version.number}` : "–", s.version?.title || "Current version"],
      [s.mismatches, "Files that differ"], [s.violations, "Rulebook broken"], [s.alerts24h, "Alerts (24 h)"],
      [s.pending, "Proposed changes"], [r.files ?? "–", "Tracked files"]].map(([n, l]) =>
      el("div", { class: "stat" }, el("div", { class: "n" }, n), el("div", { class: "l" }, l)))),
    el("div", { class: "cols" },
      el("div", {},
        sec("Problems", s.problems.length ? el("ul", {}, s.problems.map((p) => el("li", { class: cls }, p)))
          : el("div", { class: "ok-t" }, ic("check"), " Nothing to report — ready for a session")),
        files.length ? sec("Files that differ from the current version", el("table", {}, el("tr", {}, el("th", {}, "File"), el("th", {}, "Group"), el("th", {}, "Status")),
          files.map((f) => el("tr", {}, el("td", {}, f.file), el("td", { class: "muted" }, f.group), el("td", { class: "warn-t" }, f.status_text || f.status))))) : "",
        (r.rulebook || []).length ? sec("Rulebook", el("table", {}, el("tr", {}, el("th", {}, ""), el("th", {}, "Setting"), el("th", {}, "Required"), el("th", {}, "Current")),
          r.rulebook.map((b) => el("tr", {}, el("td", { class: b.ok === false ? "bad-t" : b.ok ? "ok-t" : "muted" }, ic(b.ok === false ? "error" : b.ok ? "check" : "circle")),
            el("td", {}, b.label, b.note ? el("div", { class: "muted small" }, b.note) : ""), el("td", {}, b.required || "–"), el("td", {}, b.current ?? "–"))))) : "",
        sec("Uptime (last 7 days)", el("div", { class: "uptime" }, up.days.map((x) => el("div", { title: `${x.day}: ${x.pct}%`, style: `height:${Math.max(2, x.pct)}%` }))),
          el("div", { class: "row small muted", style: "justify-content:space-between" }, el("span", {}, up.days[0]?.day), el("span", {}, "today")),
          el("div", { class: "small muted", style: "margin-top:8px" }, "Last 24 hours"), el("div", { class: "strip" }, up.last24h.map((x) => el("div", { class: x ? "on" : "off" })))),
        sec("Versions", (r.versions || []).length ? el("table", {}, (r.versions || []).map((x) =>
          el("tr", {}, el("td", {}, el("b", {}, `v${x.number}`)), el("td", {}, x.title || ""), el("td", { class: "muted small" }, fmt(x.created_at)), el("td", { class: "muted small" }, x.created_by || ""))))
          : el("div", { class: "muted" }, "No versions yet"))),
      el("div", {},
        sec("Logbook", can("tech") ? el("div", {}, noteText, el("div", { class: "row", style: "margin:6px 0 10px" }, noteCat, el("span", { class: "grow" }),
          el("button", { class: "primary", onclick: async () => {
            if (!noteText.value.trim()) return;
            await api(`/api/sims/${id}/notes`, { method: "POST", body: { text: noteText.value, category: noteCat.value } }); route();
          } }, ic("note"), " Add to logbook"))) : "",
          (d.notes || []).length ? d.notes.slice(0, 50).map((n) => el("div", { class: "note" },
            el("div", { class: "small muted" }, `${fmt(n.ts)} · ${n.author} · ${n.category || "note"}`), el("div", {}, n.text)))
            : el("div", { class: "muted" }, "No entries yet.")),
        sec("Recent alerts on the sim", (r.recent_alerts || []).length ? el("table", {}, r.recent_alerts.map((a) =>
          el("tr", {}, el("td", { class: "muted small", style: "white-space:nowrap" }, fmt(a.ts)), el("td", {}, a.message)))) : el("div", { class: "muted" }, "None in the last 24 h.")),
        sec("Hub events", d.events.length ? el("table", {}, d.events.slice(0, 40).map((e) =>
          el("tr", {}, el("td", { class: "muted small", style: "white-space:nowrap" }, fmt(e.ts)),
            el("td", { class: e.level === "alert" ? "bad-t" : e.level === "warning" ? "warn-t" : "" }, e.message)))) : el("div", { class: "muted" }, "No events.")),
        sec("System", el("div", { class: "small muted" }, `PC ${r.host?.hostname || r.machine || ""} · ${r.host?.os || ""} · app ${r.app_version || ""} · from ${s.lastIp || "?"}`),
          ...Object.entries(r.system || {}).map(([k, val]) => [el("div", { class: "small", style: "margin-top:8px" }, k), el("pre", {}, val)])))));
}

async function viewEvents(v) {
  setHead("Events", "Everything that happened across the fleet");
  EVENTS_BADGE = 0; renderChrome();
  const simSel = el("select", {}, el("option", { value: "" }, "All sims"), SIMS.map((s) => el("option", { value: s.id }, s.name)));
  const lvlSel = el("select", {}, [["", "All levels"], ["alert", "Alerts"], ["warning", "Warnings"], ["info", "Info"]].map(([vv, t]) => el("option", { value: vv }, t)));
  const table = el("table");
  const load = async () => {
    const q = new URLSearchParams({ limit: 500 });
    if (simSel.value) q.set("sim", simSel.value);
    if (lvlSel.value) q.set("level", lvlSel.value);
    const { events } = await api(`/api/events?${q}`);
    table.replaceChildren(el("tr", {}, el("th", {}, "When"), el("th", {}, "Sim"), el("th", {}, "Type"), el("th", {}, "Message")),
      ...events.map((e) => el("tr", {}, el("td", { class: "muted small", style: "white-space:nowrap" }, fmt(e.ts)),
        el("td", {}, e.simName || ""), el("td", { class: "muted" }, e.type),
        el("td", { class: e.level === "alert" ? "bad-t" : e.level === "warning" ? "warn-t" : "" }, e.message))));
  };
  simSel.onchange = lvlSel.onchange = load;
  v.replaceChildren(el("div", { class: "row", style: "margin-bottom:10px" }, simSel, lvlSel), el("div", { class: "section" }, table));
  await load();
}

async function viewAdmin(v) {
  setHead("Sims & settings", "Add sims and give each its own key");
  if (!can("admin")) { v.replaceChildren(el("div", { class: "banner err" }, "Admins only.")); return; }
  const name = el("input", { placeholder: "Sim name, e.g. Sim 1" });
  const showKey = (simName, key) => modal(el("h3", { style: "margin:0" }, `Key for ${simName}`),
    el("div", {}, "Shown only once. In Sim Config Manager on that sim: ", el("b", {}, "Sharing → Hub"), ", tick ", el("b", {}, "Report this sim's status to a Hub"), " and enter:"),
    el("div", { class: "muted" }, "Hub address"), el("code", { class: "key" }, location.origin),
    el("div", { class: "muted" }, "Sim key"), el("code", { class: "key" }, key),
    el("div", { class: "row" }, el("button", { onclick: () => navigator.clipboard?.writeText(key) }, "Copy key"),
      el("span", { class: "grow" }), el("button", { class: "primary", onclick: () => { closeModal(); route(); } }, "Done")));
  const rows = SIMS.map((s) => el("tr", {}, el("td", {}, el("b", {}, s.name)), el("td", { class: STATE[s.state][1] }, STATE[s.state][0]),
    el("td", { class: "muted" }, ago(s.lastSeen)), el("td", { class: "muted" }, s.machine || ""),
    el("td", {}, el("div", { class: "row" },
      el("button", { class: "small", onclick: async () => {
        if (!confirm(`Make a new key for ${s.name}? The old key stops working.`)) return;
        const r = await api(`/api/sims/${s.id}/rotate`, { method: "POST" }); showKey(s.name, r.key);
      } }, ic("key"), " New key"),
      el("button", { class: "small danger", onclick: async () => {
        if (!confirm(`Remove ${s.name} from the hub? (The sim itself is not touched.)`)) return;
        await api(`/api/sims/${s.id}`, { method: "DELETE" }); await refreshSims(); route();
      } }, ic("trash"), " Remove")))));
  v.replaceChildren(
    el("div", { class: "section" }, el("h3", {}, "Add a sim"), el("div", { class: "row" }, name,
      el("button", { class: "primary", onclick: async () => {
        try { const r = await api("/api/sims", { method: "POST", body: { name: name.value } }); await refreshSims(); showKey(r.name, r.key); }
        catch (e) { alert(e.message); }
      } }, "Add sim"))),
    el("div", { class: "section" }, el("h3", {}, "Sims"), SIMS.length ? el("table", {}, el("tr", {}, el("th", {}, "Name"), el("th", {}, "Status"),
      el("th", {}, "Last report"), el("th", {}, "PC"), el("th", {}, "")), rows) : el("div", { class: "muted" }, "None yet.")),
    el("div", { class: "section" }, el("h3", {}, "Notifications & access"),
      el("div", {}, "Users, roles, webhooks (Slack, Teams, Discord, ntfy) and the wall-display link are set in the hub's ", el("code", {}, "config.json"), " — see the README."),
      el("div", { class: "row", style: "margin-top:8px" }, el("button", { onclick: async () => {
        const r = await api("/api/test-webhooks", { method: "POST" }); alert(`Test sent to ${r.webhooks} webhook(s).`);
      } }, "Send test notification"))));
}

// ---------------------------------------------------------------- routing + live updates
async function refreshSims() { const d = await api("/api/sims"); SIMS = d.sims; renderChrome(); }

async function route() {
  const v = $("#view");
  renderChrome();
  if (!ME) return viewLogin(v);
  const [r, arg] = location.hash.slice(2).split("/");
  if (r === "sim" && arg) return viewSim(v, arg);
  if (r === "board") return viewBoard(v);
  if (r === "events") return viewEvents(v);
  if (r === "admin") return viewAdmin(v);
  return viewFleet(v);
}
const render = route;

function connect() {
  if (es) es.close();
  es = new EventSource("/api/stream");
  es.onopen = renderChrome;
  es.onerror = renderChrome;
  es.addEventListener("sim", (m) => {
    const s = JSON.parse(m.data);
    const i = SIMS.findIndex((x) => x.id === s.id);
    if (i >= 0) SIMS[i] = s; else SIMS.push(s);
    const r = location.hash.slice(2).split("/");
    if (!r[0] || r[0] === "fleet" || r[0] === "board") route(); else renderChrome();
    if (r[0] === "sim" && r[1] === s.id) route();
  });
  es.addEventListener("event", (m) => {
    const ev = JSON.parse(m.data);
    if (ev.level !== "info" && !location.hash.startsWith("#/events")) { EVENTS_BADGE++; renderChrome(); }
    if (location.hash.startsWith("#/events")) route();
  });
  es.addEventListener("removed", () => refreshSims().then(route));
}

async function logout() { await api("/api/logout", { method: "POST" }).catch(() => {}); ME = null; if (es) es.close(); route(); }

async function start() {
  await refreshSims();
  connect();
  route();
}

window.addEventListener("hashchange", route);
setInterval(() => { if (ME) { renderChrome(); if (location.hash.startsWith("#/board") || !location.hash || location.hash === "#/fleet") route(); } }, 30_000);
(async () => {
  INFO = await api("/api/me").catch(() => ({}));
  ME = INFO.user;
  if (ME) start(); else route();
})();
