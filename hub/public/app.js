// Sim Config Manager Hub — web dashboard (no framework, no build step).
const IC = { fleet: "", board: "", events: "", admin: "", check: "", warn: "",
  error: "", circle: "", tools: "", note: "", tag: "", refresh: "",
  signout: "", export: "", remote: "", clock: "", back: "", key: "", trash: "" };

Object.assign(IC, { maint: "\u{EB6D}", phone: "\u{EADB}", pulse: "\u{EB31}", hw: "\u{EABE}", health: "\u{EB05}",
  pkg: "\u{EB29}", calendar: "\u{EAB0}", question: "\u{EB32}", qr: "\u{EAB3}", sparkle: "\u{EC10}", add: "\u{EA60}" });

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
const toMsJs = (ts) => (!ts ? 0 : ts < 1e12 ? ts * 1000 : ts);
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
  const items = [["fleet", "fleet", "Fleet"], ["board", "board", "Readiness board"], ["maintenance", "maint", "Maintenance"],
    ["events", "events", "Events"]];
  const nav = $("#activity");
  nav.replaceChildren(...items.map(([r, icon, t]) => el("a", { href: `#/${r}`, title: t,
    class: route === r || (r === "fleet" && (route === "sim" || route === "m")) ? "active" : "" }, ic(icon),
    r === "events" && EVENTS_BADGE ? el("span", { class: "badge" }, EVENTS_BADGE > 99 ? "99+" : EVENTS_BADGE) : null)),
    el("div", { class: "spacer" }),
    can("admin") ? el("a", { href: "#/admin", title: "Sims & settings", class: route === "admin" || route === "qr" ? "active" : "" }, ic("admin")) : null);
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
    s.lastSession ? el("div", { class: "meta" }, ic("pulse"), ` Last session ${ago(toMsJs(s.lastSession.started))}` +
      (s.lastSession.avg_fps ? ` · ${s.lastSession.avg_fps} fps` : "") + (s.lastSession.crashed ? " · crashed" : "")) : "",
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
      el("span", { class: "grow" }), el("button", { onclick: () => explainModal(id) }, ic("question"), s.ready ? " Why GO?" : " Why NO-GO?"),
      el("a", { class: "btn", href: `#/m/${id}`, title: "Phone page (also behind the QR sticker)" }, ic("phone"), " Phone page"), maintBtn),
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
          : el("div", { class: "muted" }, "No versions yet")),
        secSessions(sec, r)),
      el("div", {},
        secHardware(sec, r), secMaintenance(sec, r, id, () => route()), secHealth(sec, r), secIntegrity(sec, r),
        sec("Logbook", can("tech") ? el("div", {}, noteText, el("div", { class: "row", style: "margin:6px 0 10px" }, noteCat, el("span", { class: "grow" }),
          el("button", { class: "primary", onclick: async () => {
            if (!noteText.value.trim()) return;
            await api(`/api/sims/${id}/notes`, { method: "POST", body: { text: noteText.value, category: noteCat.value } }); route();
          } }, ic("note"), " Add to logbook"))) : "",
          (d.notes || []).length ? d.notes.slice(0, 50).map((n) => el("div", { class: "note" },
            el("div", { class: "small muted" }, `${fmt(n.ts)} · ${n.author} · ${n.category || "note"}`), el("div", {}, n.text),
            n.photo ? el("a", { href: `/api/photos/${n.photo}`, target: "_blank" }, el("img", { class: "photo", src: `/api/photos/${n.photo}`, alt: "photo" })) : ""))
            : el("div", { class: "muted" }, "No entries yet.")),
        sec("Recent alerts on the sim", (r.recent_alerts || []).length ? el("table", {}, r.recent_alerts.map((a) =>
          el("tr", {}, el("td", { class: "muted small", style: "white-space:nowrap" }, fmt(a.ts)), el("td", {}, a.message)))) : el("div", { class: "muted" }, "None in the last 24 h.")),
        sec("Hub events", d.events.length ? el("table", {}, d.events.slice(0, 40).map((e) =>
          el("tr", {}, el("td", { class: "muted small", style: "white-space:nowrap" }, fmt(e.ts)),
            el("td", { class: e.level === "alert" ? "bad-t" : e.level === "warning" ? "warn-t" : "" }, e.message)))) : el("div", { class: "muted" }, "No events.")),
        sec("System", el("div", { class: "small muted" }, `PC ${r.host?.hostname || r.machine || ""} · ${r.host?.os || ""} · app ${r.app_version || ""} · from ${s.lastIp || "?"}`),
          ...Object.entries(r.system || {}).map(([k, val]) => [el("div", { class: "small", style: "margin-top:8px" }, k), el("pre", {}, val)])))));
}

// ---------------------------------------------------------------- sim page sections (sessions, hardware, health …)
const mins = (m) => (m == null ? "–" : m >= 60 ? `${Math.floor(m / 60)} h ${String(Math.round(m % 60)).padStart(2, "0")} min` : `${Math.round(m)} min`);

function secSessions(sec, r) {
  const perf = r.performance || [], ss = r.sessions || [];
  if (!perf.length && !ss.length) return "";
  return sec("Sessions & performance",
    perf.length ? el("table", {}, el("tr", {}, ["Version", "Sessions", "Hours", "Avg fps", "1% low", "Crashes / 10 h"].map((h) => el("th", {}, h))),
      perf.map((p) => el("tr", {}, el("td", {}, el("b", {}, p.label)), el("td", {}, p.sessions), el("td", {}, p.hours),
        el("td", {}, p.avg_fps ?? "–"), el("td", {}, p.low1_fps ?? "–"),
        el("td", { class: p.crashes_per_10h ? "bad-t" : "" }, p.crashes_per_10h ?? "–")))) : "",
    el("div", { class: "small muted", style: "margin:10px 0 4px" }, "Recent sessions"),
    el("table", {}, ss.slice(0, 8).map((x) => {
      const st = x.stats || {}, cr = x.crash || {};
      const res = !x.ended ? ["running", "warn-t"] : (cr.crashes || []).length ? [`crashed (${cr.crashes[0].module})`, "bad-t"]
        : (cr.display_resets || []).length ? ["display reset", "bad-t"] : ["OK", "ok-t"];
      return el("tr", {}, el("td", { class: "muted small", style: "white-space:nowrap" }, fmt(x.started)),
        el("td", {}, x.kind === "benchmark" ? `Benchmark: ${x.label}` : (x.aircraft || "Session")),
        el("td", {}, mins(st.minutes)), el("td", {}, x.version_number ? `v${x.version_number}` : "–"),
        el("td", {}, st.avg_fps ? `${st.avg_fps} fps` : "–"), el("td", { class: res[1] }, res[0]));
    })));
}

function secHardware(sec, r) {
  const hw = r.hardware || [];
  if (!hw.length) return "";
  return sec("Cockpit hardware", el("table", {}, hw.map((h) => el("tr", {},
    el("td", { class: h.ok ? "ok-t" : "bad-t" }, ic(h.ok ? "check" : "error")), el("td", {}, el("b", {}, h.label),
      h.note ? el("div", { class: "muted small" }, h.note) : ""),
    el("td", { class: h.ok ? "muted" : "bad-t" }, h.ok ? (h.device || "connected") : "missing")))));
}

function secMaintenance(sec, r, id, after) {
  const tasks = r.maintenance || [];
  if (!tasks.length) return "";
  const cls = { ok: "ok-t", soon: "warn-t", overdue: "bad-t" };
  return sec(`Maintenance${r.sim_hours ? ` · ${r.sim_hours} sim hours` : ""}`, el("table", {}, tasks.map((t) => el("tr", {},
    el("td", { class: cls[t.state] }, t.state === "ok" ? "OK" : t.state === "soon" ? "Due soon" : "Overdue"),
    el("td", {}, el("b", {}, t.name), el("div", { class: "muted small" }, [t.due_date ? `due ${fmt(t.due_date)}` : "",
      t.due_in_hours != null ? `${t.due_in_hours >= 0 ? "in" : "overdue by"} ${Math.abs(t.due_in_hours)} sim h` : ""].filter(Boolean).join(" · "))),
    el("td", { class: "muted small" }, t.last_done ? `last ${fmt(t.last_done)}` : "never done"),
    el("td", {}, can("tech") ? el("button", { class: "small", onclick: () => markDone(id, t, after) }, ic("check"), " Done") : "")))));
}

async function markDone(id, t, after) {
  const note = prompt(`${t.name} — done. Note (optional):`, "");
  if (note === null) return;
  try { await api(`/api/sims/${id}/maintenance-done`, { method: "POST", body: { task: t.id, note } }); after(); }
  catch (e) { alert(e.message); }
}

function secHealth(sec, r) {
  const h = r.health || {};
  if (!h.disks && !h.gpu && !(h.projectors || []).length) return "";
  const warn = h.warnings || [];
  return sec("Health",
    warn.length ? el("ul", { class: "warn-t" }, warn.map((w) => el("li", {}, w))) : el("div", { class: "ok-t" }, ic("check"), " No warnings"),
    h.gpu ? el("div", { class: "small", style: "margin-top:6px" }, `GPU ${h.gpu.name || ""}: ${Math.round(h.gpu.temp)} °C, ${Math.round(h.gpu.util)}% busy`) : "",
    (h.disks || []).length ? el("table", { style: "margin-top:6px" }, h.disks.map((d) => el("tr", {}, el("td", {}, d.name),
      el("td", { class: d.health === "Healthy" ? "ok-t" : "bad-t" }, d.health || "–"), el("td", {}, d.temp ? `${d.temp} °C` : "–"),
      el("td", {}, d.wear != null ? `${d.wear}% worn` : "")))) : "",
    (h.projectors || []).length ? el("table", { style: "margin-top:6px" }, h.projectors.map((p) => el("tr", {}, el("td", {}, el("b", {}, p.name)),
      el("td", { class: p.error ? "bad-t" : "" }, p.error ? "not reachable" : (p.power || "–")),
      el("td", {}, p.lamp_hours != null ? `lamp ${p.lamp_hours} h${p.lamp_pct != null ? ` (${p.lamp_pct}%)` : ""}` : ""),
      el("td", { class: "bad-t" }, (p.errors || []).join(", "))))) : "");
}

function secIntegrity(sec, r) {
  const mans = r.manifests || [], ins = r.installs || [];
  if (!mans.length && !ins.length) return "";
  return sec("Integrity & installs",
    mans.length ? el("table", {}, mans.map((m) => el("tr", {}, el("td", {}, el("b", {}, m.name)), el("td", { class: "muted small" }, `${m.files} files`),
      el("td", { class: !m.last_check ? "muted" : m.last_check.ok ? "ok-t" : "bad-t" }, !m.last_check ? "not verified"
        : m.last_check.ok ? `all files match (${fmt(m.last_check.ts)})` : `${m.last_check.counts.changed} changed, ${m.last_check.counts.added} added, ${m.last_check.counts.removed} missing`)))) : "",
    ins.length ? el("table", { style: "margin-top:6px" }, ins.map((i) => el("tr", {}, el("td", { class: "muted small", style: "white-space:nowrap" }, fmt(i.started)),
      el("td", {}, i.name), el("td", { class: "muted small" }, `+${i.counts.added ?? "?"} ~${i.counts.changed ?? "?"} −${i.counts.removed ?? "?"}`),
      el("td", { class: i.status === "rolled_back" ? "muted" : "" }, i.status.replace("_", " "))))) : "");
}

async function explainModal(id) {
  const box = el("div", { class: "explain" }, "Thinking…");
  modal(el("h3", { style: "margin:0" }, ic("question"), " Is this sim ready?"), box,
    el("div", { class: "row" }, el("span", { class: "grow" }), el("button", { class: "primary", onclick: closeModal }, "Close")));
  try {
    const r = await api(`/api/sims/${id}/explain`);
    box.replaceChildren(el("div", { style: "white-space:pre-wrap" }, r.text),
      el("div", { class: "small muted", style: "margin-top:8px" }, r.source === "ai" ? "Answered by Claude from the sim's latest report." : (r.note || "")));
  } catch (e) { box.textContent = e.message; }
}

// ---------------------------------------------------------------- phone page (behind the QR sticker on each sim)
async function viewPhone(v, id) {
  let d;
  try { d = await api(`/api/sims/${id}`); } catch (e) { v.replaceChildren(el("div", { class: "banner err" }, e.message)); return; }
  const s = d.summary, r = d.report || {};
  setHead(s.name, "Phone page");
  const big = s.maintenance?.on ? ["MAINTENANCE", "maint-t"] : !s.online ? ["OFFLINE", "off-t"] : s.ready ? ["GO", "ok-t"] : ["NO-GO", "bad-t"];
  const text = el("textarea", { placeholder: "What's wrong? e.g. 'Left projector flickers', 'FO yoke loose'" });
  const cat = el("select", {}, ["incident", "hardware", "software", "calibration", "note"].map((c) => el("option", { value: c }, c)));
  const photo = el("input", { type: "file", accept: "image/*", capture: "environment" });
  const msg = el("div", { class: "small" });
  const send = async () => {
    if (!text.value.trim()) { msg.textContent = "Write what happened first."; return; }
    msg.textContent = "Sending…";
    try {
      const body = { text: text.value, category: cat.value };
      if (photo.files[0]) body.photo = await shrink(photo.files[0]);
      await api(`/api/sims/${id}/notes`, { method: "POST", body });
      text.value = ""; photo.value = ""; msg.className = "small ok-t"; msg.textContent = "Logged. Thank you!";
    } catch (e) { msg.className = "small bad-t"; msg.textContent = e.message; }
  };
  const due = (r.maintenance || []).filter((t) => t.state !== "ok");
  v.replaceChildren(el("div", { class: "phone" },
    el("div", { class: `tile ${big[1].replace("-t", "")}` }, el("div", { class: "nm" }, s.name), el("div", { class: `big ${big[1]}` }, big[0]),
      el("div", { class: "muted" }, s.version ? `v${s.version.number} – ${s.version.title || ""}` : "No version yet"),
      !s.ready && s.problems.length ? el("ul", { class: "small bad-t" }, s.problems.slice(0, 4).map((p) => el("li", {}, p))) : ""),
    el("button", { class: "wide", onclick: () => explainModal(id) }, ic("question"), s.ready ? " Why is it GO?" : " Why is it NO-GO?"),
    can("tech") ? el("div", { class: "section" }, el("h3", {}, "Log an issue"), text, el("div", { class: "row", style: "margin:6px 0" }, cat, photo),
      el("button", { class: "primary wide", onclick: send }, ic("add"), " Log it"), msg) : "",
    can("tech") && due.length ? el("div", { class: "section" }, el("h3", {}, "Maintenance due"), due.map((t) =>
      el("div", { class: "row", style: "margin:4px 0" }, el("span", { class: `grow ${t.state === "overdue" ? "bad-t" : "warn-t"}` }, t.name),
        el("button", { onclick: () => markDone(id, t, () => route()) }, ic("check"), " Done")))) : "",
    can("tech") ? el("button", { class: "wide", onclick: async () => {
      const on = !s.maintenance?.on;
      const note = on ? prompt("Why? (shown on the board)", "") : "";
      if (on && note === null) return;
      await api(`/api/sims/${id}/maintenance`, { method: "POST", body: { on, note } }); route();
    } }, ic("tools"), s.maintenance?.on ? " End maintenance" : " Start maintenance") : "",
    el("a", { class: "btn wide", href: `#/sim/${id}` }, "Full sim page")));
}

function shrink(file, max = 1600) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const k = Math.min(1, max / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(img.src);
      resolve(c.toDataURL("image/jpeg", 0.82));
    };
    img.onerror = () => reject(new Error("That file is not an image"));
    img.src = URL.createObjectURL(file);
  });
}

// ---------------------------------------------------------------- maintenance across the fleet
async function viewMaintenance(v) {
  setHead("Maintenance", "Every sim's schedule — overdue first");
  const rows = [];
  await Promise.all(SIMS.map(async (s) => {
    try {
      const d = await api(`/api/sims/${s.id}`);
      for (const t of d.report?.maintenance || []) rows.push({ sim: s, t, hours: d.report.sim_hours });
    } catch { /* offline hub call */ }
  }));
  const order = { overdue: 0, soon: 1, ok: 2 };
  rows.sort((a, b) => order[a.t.state] - order[b.t.state] || (a.t.due_date || 9e12) - (b.t.due_date || 9e12));
  const cls = { ok: "ok-t", soon: "warn-t", overdue: "bad-t" };
  v.replaceChildren(rows.length ? el("div", { class: "section" }, el("table", {},
    el("tr", {}, ["", "Sim", "Task", "Due", "Last done", ""].map((h) => el("th", {}, h))),
    rows.map(({ sim, t }) => el("tr", {}, el("td", { class: cls[t.state] }, t.state === "ok" ? "OK" : t.state === "soon" ? "Due soon" : "Overdue"),
      el("td", {}, el("a", { href: `#/sim/${sim.id}` }, sim.name)), el("td", {}, el("b", {}, t.name), t.note ? el("div", { class: "muted small" }, t.note) : ""),
      el("td", {}, [t.due_date ? fmt(t.due_date) : "", t.due_in_hours != null ? `${t.due_in_hours} sim h` : ""].filter(Boolean).join(" · ")),
      el("td", { class: "muted small" }, t.last_done ? fmt(t.last_done) : "never"),
      el("td", {}, can("tech") ? el("button", { class: "small", onclick: () => markDone(sim.id, t, () => route()) }, ic("check"), " Done") : "")))))
    : el("div", { class: "section" }, el("h3", {}, "No maintenance tasks"), "Add tasks in Sim Config Manager on each sim (Hardware & health page)."));
}

// ---------------------------------------------------------------- QR stickers
function viewQr(v) {
  setHead("QR stickers", "Print and stick one on each sim — scanning opens its phone page");
  const qrImg = (text) => {
    if (typeof qrcode !== "function") return el("div", { class: "muted" }, "QR library not loaded");
    const q = qrcode(0, "M"); q.addData(text); q.make();
    return el("img", { class: "qr", src: q.createDataURL(6, 2), alt: text });
  };
  v.replaceChildren(el("div", { class: "row noprint", style: "margin-bottom:12px" }, el("span", { class: "muted grow" },
    "Uses this address: ", el("b", {}, location.origin), ". Open the hub on its public address before printing if phones use another one."),
    el("button", { class: "primary", onclick: () => window.print() }, "Print")),
  el("div", { class: "stickers" }, SIMS.map((s) => el("div", { class: "sticker" }, qrImg(`${location.origin}/#/m/${s.id}`),
    el("div", { class: "nm" }, s.name), el("div", { class: "small" }, `${INFO.title || "Sim Center"} · scan to report an issue`)))));
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
    el("div", { class: "section" }, el("h3", {}, "QR stickers"), el("div", { class: "row" },
      el("span", { class: "grow" }, "A sticker per sim that opens its phone page: status, \u201cwhy NO-GO?\u201d, log an issue with a photo, maintenance."),
      el("a", { class: "btn", href: "#/qr" }, ic("qr"), " Print QR stickers"))),
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
  if (r === "m" && arg) return viewPhone(v, arg);
  if (r === "maintenance") return viewMaintenance(v);
  if (r === "qr" && can("admin")) return viewQr(v);
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
    if ((r[0] === "sim" || r[0] === "m") && r[1] === s.id) route();
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
