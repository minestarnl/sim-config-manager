// "Why is this sim NO-GO?" — a plain-language answer for the phone page and the sim page.
// With an Anthropic API key in the config, Claude writes it from the sim's latest report; without one the hub
// explains the problems itself. Only the status summary is sent (never file contents — the hub has none).

function ruleBased(sum) {
  if (sum.maintenance?.on) return `${sum.name} is in maintenance mode${sum.maintenance.note ? `: ${sum.maintenance.note}` : ""}.`;
  if (sum.ready) return `${sum.name} is ready (GO): online, every file matches v${sum.version?.number}, the rulebook is OK and all required cockpit hardware is connected.`;
  const lines = [`${sum.name} is NO-GO because:`];
  for (const p of sum.problems) lines.push(`• ${p}`);
  const next = [];
  if (!sum.online) next.push("Check that the sim PC is on and Sim Config Manager is running.");
  if (sum.hardwareMissing.length) next.push("Reconnect the missing hardware (check USB cables and hubs), then wait 30 seconds.");
  if (sum.mismatches) next.push("Restore the files to the current version (Versions page) or create a new version if the change was intended.");
  if (sum.violations) next.push("Put the locked settings back to their rulebook value.");
  if (!sum.version) next.push("Create the first version once the sim is set up.");
  if (next.length) lines.push("", "What to do:", ...next.map((n) => `• ${n}`));
  return lines.join("\n");
}

export async function explain(cfg, sim, sum, log = () => {}) {
  const text = ruleBased(sum);
  if (!cfg.anthropicApiKey) return { text, source: "rules" };
  const r = sim.report || {};
  const facts = {
    sim: sum.name, ready: sum.ready, state: sum.state, online: sum.online, maintenance: sum.maintenance,
    problems: sum.problems, version: r.version, files_that_differ: r.mismatches, rulebook: r.rulebook,
    hardware: r.hardware, health: r.health?.warnings, maintenance_tasks: r.maintenance, last_session: r.last_session,
    recent_alerts: r.recent_alerts,
  };
  try {
    const res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "content-type": "application/json", "x-api-key": cfg.anthropicApiKey, "anthropic-version": "2023-06-01" },
      body: JSON.stringify({
        model: cfg.anthropicModel || "claude-sonnet-5-5", max_tokens: 600,
        system: "You help flight-simulator technicians. Explain in plain language, in at most 8 short lines, why " +
          "this simulator is or isn't ready for a training session and what to do first. Use only the facts given.",
        messages: [{ role: "user", content: JSON.stringify(facts) }],
      }),
      signal: AbortSignal.timeout(30_000),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error?.message || res.statusText);
    const out = (data.content || []).filter((c) => c.type === "text").map((c) => c.text).join("\n").trim();
    return out ? { text: out, source: "ai" } : { text, source: "rules" };
  } catch (e) {
    log("explain: AI request failed:", e.message);
    return { text, source: "rules", note: "The AI answer was not available; this is the hub's own summary." };
  }
}
