"use strict";
window.AgentKitAllowance = (() => {
  function element(tag, text, style) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (style) node.className = style;
    return node;
  }
  function windowName(value) {
    if (value === "not_applicable") return "Local model health";
    if (value === "300" || value === "five_hour") return "5-hour window";
    if (value === "10080" || value === "seven_day") return "7-day window";
    if (/^\d+$/.test(value || "")) return value + "-minute window";
    return value || "Window not reported";
  }
  function stamp(value) {
    const parsed = value ? new Date(value) : null;
    return parsed && Number.isFinite(parsed.getTime()) ? parsed.toLocaleString() : "Not reported";
  }
  function render(view, target) {
    target.replaceChildren();
    const rows = Array.isArray(view.allowance_reports) ? view.allowance_reports : [];
    if (!rows.length) target.append(element("div", "No provider allowance snapshot recorded. Unknown does not mean unlimited.", "empty"));
    for (const row of rows) {
      const card = element("article", undefined, "card");
      card.append(element("strong", row.provider + " · " + windowName(row.window)),
        element("p", "Bucket: " + (row.bucket || "unreported"), "muted"));
      if (row.source === "unmetered_health") {
        card.append(element("h3", "No subscription quota"),
          element("p", "No 5-hour or weekly reset. Health and capacity checks apply."),
          element("p", "Availability: " + (row.status || "UNKNOWN")),
          element("p", "Next health check: " + stamp(row.resets_at), "muted"));
        target.append(card);
        continue;
      }
      const known = typeof row.remaining_percent === "number" && Number.isFinite(row.remaining_percent);
      const saved = known ? row.remaining_percent.toLocaleString() + "% remaining (last reported)" : "Percentage not reported";
      const value = row.stale || row.reset_due ? "Current allowance unknown" : saved;
      card.append(element("h3", value), element("p", "Reset / next availability check: " + stamp(row.resets_at)),
        element("p", "Observed: " + stamp(row.observed_at), "muted"));
      if (known && (row.stale || row.reset_due)) card.append(element("p", "Saved report: " + saved, "muted"));
      if (row.reset_due) card.append(element("p", "Reset time passed; provider confirmation is still required.", "muted"));
      else if (row.stale) card.append(element("p", "Saved or incomplete report; current allowance is unknown.", "muted"));
      card.append(element("p", "Recorded availability: " + (row.status || "UNKNOWN"), "muted"));
      target.append(card);
    }
  }
  return {render, windowName};
})();
