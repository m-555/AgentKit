"use strict";
(() => {
  const screens = new Map();
  const $ = id => document.getElementById(id);
  const node = (tag, text, cls) => {
    const element = document.createElement(tag);
    if (text != null) element.textContent = String(text);
    if (cls) element.className = cls;
    return element;
  };
  const number = value => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : "unknown";
  function totals(view) {
    const usage = view.project_usage || {};
    const target = $("project-usage");
    if (!target) return;
    target.replaceChildren();
    for (const [value, label] of [
      [usage.recorded_tokens, usage.complete ? "Recorded project tokens" : "Recorded tokens (partial coverage)"],
      [usage.normalized_input_tokens, "Input including cache, counted once"],
      [usage.counters?.output_tokens, "Output including thinking"],
      [usage.counters?.thinking_tokens, "Reported thinking (within output)"]
    ]) {
      const cell = node("div", null, "stat");
      cell.append(node("strong", number(value)), node("span", label));
      target.append(cell);
    }
    $("usage-coverage").textContent = usage.status === "unavailable" ? "Project usage unavailable; no total inferred." :
      number(usage.complete_launches) + " complete, " + number(usage.partial_launches) + " partial and " +
      number(usage.missing_launches) + " missing reports across " + number(usage.launches) + " launches. " +
      number(usage.external_sessions_unmetered) + " external manager sessions have unreported usage. " + (usage.scope || "");
    const breakdown = $("usage-breakdown");
    breakdown.replaceChildren();
    for (const row of [...(usage.providers || []), ...(usage.roles || [])]) {
      const card = node("article", null, "card");
      card.append(node("h3", row.provider || row.role), node("p", number(row.recorded_tokens) + " recorded tokens"));
      for (const [key, label] of [["input_tokens","Reported input"],["output_tokens","Output including thinking"],
        ["thinking_tokens","Thinking within output"],["cached_input_tokens","Cache reads"],
        ["cache_write_input_tokens","Cache writes"]]) {
        card.append(node("p", label + ": " + number(row.counters?.[key]) + " (" + number(row.reported_counts?.[key]) + " reporting launches)"));
      }
      breakdown.append(card);
    }
  }
  function create(row) {
    const card = node("article", null, "card");
    const heading = node("h3");
    const description = node("p", null, "overview");
    const state = node("p", "Connecting to the registered CLI window...", "muted");
    const path = node("p", null, "overview");
    const output = node("div", null, "terminal-monitor");
    output.hidden = true;
    output.tabIndex = 0;
    output.setAttribute("aria-label", "Actual CLI monitor for " + row.key);
    const image = node("img"); image.alt = "Actual registered CLI window"; image.hidden = true;
    output.append(image);
    const button = node("button", "Pause display"); button.type = "button";
    const enlarge = node("button", "Full screen"); enlarge.type = "button";
    enlarge.addEventListener("click", () => { if (card.requestFullscreen) card.requestFullscreen(); });
    const controls = node("div", null, "live-controls"); controls.append(button, enlarge);
    const screen = {card, heading, description, path, state, output, image, button,
      pending:false, paused:false, present:true, url:null, managerReady:false, sending:false};
    button.addEventListener("click", () => {
      screen.paused = !screen.paused;
      button.textContent = screen.paused ? "Resume display" : "Pause display";
      state.textContent = screen.paused ? "Display paused; the worker continues." : "Display resumed.";
    });
    card.append(heading, description, path, controls, state, output);
    if (row.manager) {
      const form = node("form", null, "manager-input");
      const input = node("input"); input.type = "text"; input.maxLength = 4000;
      input.placeholder = "Type to the CLI manager";
      input.setAttribute("aria-label", "Message to the registered CLI manager");
      const send = node("button", "Send / Enter"); send.type = "submit"; send.disabled = true;
      form.append(input, send); card.append(form);
      screen.send = send;
      form.addEventListener("submit", async event => {
        event.preventDefault();
        if (send.disabled || !input.value.trim()) return;
        screen.sending = true; send.disabled = true;
        try {
          const controlResponse = await fetch("/api/reviews", {cache:"no-store"});
          const control = await controlResponse.json();
          if (!control.enabled || !control.token) throw Error("Local operator controls are required.");
          const response = await fetch("/api/terminal/input", {method:"POST", headers:{
            "Content-Type":"application/json","X-AgentKit-Token":control.token},
            body:JSON.stringify({job_id:row.job_id,text:input.value})});
          const receipt = await response.json();
          if (!response.ok) throw Error(receipt.message);
          input.value = ""; state.textContent = receipt.message;
        } catch (error) { state.textContent = error.message + " Input is never retried automatically."; }
        finally { screen.sending = false; send.disabled = !screen.managerReady; }
      });
    }
    return screen;
  }
  async function poll(screen, row) {
    if (screen.pending || screen.paused) return;
    screen.pending = true;
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 8000);
    try {
      const response = await fetch("/api/terminal/" + row.key, {cache:"no-store", signal:abort.signal});
      if (!response.ok) {
        const result = await response.json();
        throw Error(result.message || "CLI window unavailable");
      }
      const frame = await response.blob();
      if (frame.type !== "image/png") throw Error("Invalid terminal frame received.");
      if (!screen.present) return;
      const previous = screen.url;
      screen.url = URL.createObjectURL(frame);
      screen.image.src = screen.url; screen.image.hidden = false; screen.output.hidden = false;
      if (previous) URL.revokeObjectURL(previous);
      screen.managerReady = true;
      if (screen.send) screen.send.disabled = screen.sending;
      screen.state.textContent = "Live CLI window mirror | " + (row.manager ? "Manager keyboard requires a registered interactive CLI." : "Read only; worker input is disabled.");
    } catch (error) {
      screen.managerReady = false;
      if (screen.send) screen.send.disabled = true;
      if (screen.present) screen.state.textContent = error.message + (screen.url ? " Retaining the last frame." : " No frame shown.");
    } finally {
      clearTimeout(timer); screen.pending = false;
    }
  }
  function update(view) {
    totals(view);
    const target = $("live-screens");
    if (!target) return;
    const tasks = new Map((view.tasks || []).map(task => [task.id, task]));
    const rows = (view.processes || []).filter(row => ["STARTING", "RUNNING"].includes(row.status) &&
      (row.monitor_alive === true || row.child_alive === true || row.status === "STARTING"))
      .map(row => ({...row,key:"process-" + row.id,manager:false}));
    rows.push(...(view.external_managers || []).filter(row => row.state === "ACTIVE")
      .map(row => ({...row,key:"manager-" + row.job_id,manager:true,role:"manager",worktree:"external CLI / editor registration"})));
    const active = new Set(rows.map(row => row.key));
    for (const [id, screen] of screens) if (!active.has(id)) {
      screen.present = false; screen.card.remove(); screens.delete(id);
      if (screen.url) URL.revokeObjectURL(screen.url);
    }
    target.querySelectorAll(".empty").forEach(element => element.remove());
    for (const row of rows) {
      const task = tasks.get(row.task_id);
      if (!screens.has(row.key)) {
        const screen = create(row); screens.set(row.key, screen); target.append(screen.card);
      }
      const screen = screens.get(row.key);
      screen.heading.textContent = (row.role || "agent") + " | " + row.provider + " | " + (row.manager ? row.job_id : "Run #" + row.id);
      screen.description.textContent = task ? "Task #" + task.id + ": " + task.title : "Project coordination or review";
      screen.path.textContent = "Worktree: " + (row.worktree || "not recorded") + " | " + row.state;
      poll(screen, row);
    }
    if (!rows.length) target.append(node("p", "No active CLI workers. Waiting tasks do not have a running model. This chat's messages stay in the chat; AgentKit has no CLI stream for it.", "empty"));
  }
  window.AgentKitActivity = {update};
})();
