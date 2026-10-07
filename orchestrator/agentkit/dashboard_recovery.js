(function () {
  "use strict";
  const labels = {
    WAITING_AVAILABILITY:"Waiting for provider or host",
    READY_TO_WAKE:"Ready to resume",
    CLAIMED:"Wake attempt claimed",
    DELIVERY_ACCEPTED:"Accepted · start not yet verified",
    TURN_STARTED:"Resumed turn observed",
    RECONCILING:"Unknown delivery · checking before retry",
    NEEDS_USER_ACTION:"Manual action required",
    CANCELLED:"Cancelled",
    RECOVERED:"Resumed turn completed"
  };
  function render(view, target) {
    target.replaceChildren();
    for (const host of view.wake_capabilities || []) {
      const line = document.createElement("p"); line.className = "muted";
      line.textContent = `${host.host}: ${host.support} ? ${host.reason}`; target.append(line);
    }

    for (const registration of view.native_recovery_registrations || []) {
      const card = document.createElement("article"); card.className = "card";
      const title = document.createElement("strong");
      title.textContent = registration.enabled ? "Persistent quota recovery registered" : "Native recovery stopped";
      const detail = document.createElement("p"); detail.className = "identity";
      detail.textContent = `${registration.job_id || "Unknown job"} · ${registration.thread || "Unknown chat"}`;
      const status = document.createElement("p"); status.className = "overview";
      status.textContent = `${registration.status || "unknown"}${registration.reason ? ": " + registration.reason : ""}`;
      const policy = document.createElement("p"); policy.className = "muted";
      policy.textContent = "Quota only · experimental native transport · no idle turns or duplicate deliveries";
      card.append(title, detail, status, policy); target.append(card);
    }
    const rows = view.recovery_intents || [];
    const active = rows.filter(row => !["RECOVERED","CANCELLED"].includes(row.state));
    if (!active.length) {
      const empty = document.createElement("p"); empty.className = "muted";
      empty.textContent = "No pending recorded recovery. This does not certify quota-reset wake."; target.append(empty);
      return;
    }
    for (const row of active.slice(0, 20)) {
      const card = document.createElement("article"); card.className = "card";
      const title = document.createElement("strong"); title.textContent = labels[row.state] || row.state;
      const detail = document.createElement("p"); detail.className = "identity";
      detail.textContent = `${row.role} · ${row.provider} · ${row.host} · ${row.task_id ? "Task #" + row.task_id : row.reference}`;
      const reason = document.createElement("p"); reason.className = "overview"; reason.textContent = row.reason || row.failure;
      const policy = document.createElement("p"); policy.className = "muted";
      policy.textContent = row.policy === "unmetered" ? "Local health recovery · no subscription reset" : "All blocking account windows must recover";
      card.append(title, detail, reason, policy); target.append(card);
    }
  }
  window.AgentKitRecovery = {render};
})();
