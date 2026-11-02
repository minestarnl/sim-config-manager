// Webhook notifications: Slack, Microsoft Teams, Discord, ntfy.sh, or plain JSON.
const ICON = { offline: "🔴", online: "🟢", rulebook: "⛔", drift: "🟠", alert: "⚠️", version: "🏷️",
  maintenance: "🔧", note: "📝", proposal: "✨" };

export function formatPayload(format, ev, title) {
  const text = `${ICON[ev.type] || "•"} ${ev.simName ? `*${ev.simName}* — ` : ""}${ev.message}`;
  switch (format) {
    case "slack":
      return { body: { text: `[${title}] ${text}` } };
    case "discord":
      return { body: { content: `[${title}] ${text.replace(/\*/g, "**")}` } };
    case "teams":
      return { body: { text: `**${title}** — ${text.replace(/\*/g, "**")}` } };
    case "ntfy":
      return { raw: `${ev.simName ? ev.simName + ": " : ""}${ev.message}`,
        headers: { Title: title, Tags: ev.level === "alert" ? "warning" : "information_source",
          Priority: ev.level === "alert" ? "4" : "3" } };
    default:
      return { body: { title, ...ev } };
  }
}

export async function sendWebhooks(webhooks, ev, title, log = () => {}) {
  for (const hook of webhooks || []) {
    if (!hook.url) continue;
    if (Array.isArray(hook.events) && hook.events.length && !hook.events.includes(ev.type)) continue;
    if (hook.sims && hook.sims.length && !hook.sims.includes(ev.simName)) continue;
    const p = formatPayload(hook.format || "json", ev, title);
    try {
      const res = await fetch(hook.url, {
        method: "POST",
        headers: p.raw !== undefined ? { "Content-Type": "text/plain; charset=utf-8", ...(p.headers || {}) }
          : { "Content-Type": "application/json" },
        body: p.raw !== undefined ? p.raw : JSON.stringify(p.body),
        signal: AbortSignal.timeout(10_000),
      });
      if (!res.ok) log(`webhook ${hook.url.slice(0, 40)}… answered ${res.status}`);
    } catch (e) {
      log(`webhook ${hook.url.slice(0, 40)}… failed: ${e.message}`);
    }
  }
}
