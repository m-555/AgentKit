"use strict";
(() => {
  const panel = document.getElementById("workspace-panel");
  const list = document.getElementById("workspace-list");
  const status = document.getElementById("workspace-status");
  function item(tag, value) {
    const element = document.createElement(tag);
    element.textContent = value;
    return element;
  }
  async function refresh() {
    status.textContent = "Reading local Git inventory; no model calls.";
    try {
      const response = await fetch("/api/workspaces", {cache:"no-store"});
      const view = await response.json();
      if (!response.ok) throw new Error(view.message);
      list.replaceChildren();
      if (view.integration) {
        const integration = item("article", "");
        integration.className = "card";
        integration.append(item("h3", "Combined integration"),
          item("p", "Branch: " + view.integration.branch),
          item("p", "Checkout: " + (view.integration.checkout || "not checked out; do not create a competing checkout")),
          item("p", "Commit: " + (view.integration.head || "not created")),
          item("p", "Run the project's documented preview from this checkout to test combined changes. Task checkouts show individual changes; they may not include the latest integration."));
        list.append(integration);
      }
      for (const workspace of view.workspaces) {
        const card = item("article", "");
        card.className = "card";
        card.append(item("h3", workspace.spec_id || workspace.branch),
          item("p", "Task #" + (workspace.task_id ?? "unknown") + " | " + (workspace.phase || workspace.status || "unknown")),
          item("p", "Branch: " + (workspace.branch || "unknown")),
          item("p", "Reserved path: " + (workspace.path || "not registered")),
          item("p", "Checkout: " + (workspace.checkout || "missing")),
          item("p", "Integration: " + (workspace.integration_branch || "not yet staged")));
        const environment = workspace.environment || {};
        card.append(item("p", "Setup: " + (environment.status || "not prepared") +
          " | Profile: " + (environment.profile || "unassigned") +
          " | Strategy: " + (environment.strategy || "unknown")),
          item("p", "Host setup: " + (environment.duration_s ?? "unknown") +
          " seconds | Attempts: " + (environment.attempts ?? 0) + " | AI calls: 0"));
        if (environment.key) card.append(item("p", "Dependency fingerprint: " + environment.key.slice(0, 16)));
        if (environment.free_bytes != null) card.append(item("p", "Free at setup: " +
          (environment.free_bytes / 1073741824).toFixed(2) + " GiB | Reserved: " +
          ((environment.reserve_bytes || 0) / 1073741824).toFixed(2) + " GiB"));
        if (environment.reused_snapshot) card.append(item("p", "Reused prepared dependencies as a private copy."));
        if (environment.reason) card.append(item("p", environment.reason));
        if (environment.remediation) card.append(item("p", environment.remediation));
        if (workspace.attention || workspace.detail) card.append(item("p", workspace.attention || workspace.detail));
        list.append(card);
      }
      status.textContent = view.total + " recorded or discovered workspaces" + (view.truncated ? " (first 256 shown)" : "") + ".";
    } catch (error) {
      status.textContent = "Inventory unavailable: " + error.message;
    }
  }
  panel.addEventListener("toggle", () => { if (panel.open) refresh(); });
  document.getElementById("workspace-refresh").addEventListener("click", refresh);
})();
