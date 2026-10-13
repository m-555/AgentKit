"use strict";
// Settings: every choice the manager makes for the repository chosen above, and Start/Stop.
(() => {
  const labels = {sol: "Codex GPT-6.1 Sol", opus: "Claude Opus 5.5", sonnet: "Claude Sonnet 5", qwen: "Local Qwen 3.8"};
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const pick = choice => choice ? (choice.profile === "qwen" ? {profile: "qwen"} :
    {profile: choice.profile, effort: choice.effort}) : choice;

  // Only what the user changed is saved; an untouched form saves nothing.
  function changes(saved, form) {
    const out = {};
    for (const key of ["manager", "reviewer"])
      if (form[key] && !same(pick(saved[key]), pick(form[key]))) out[key] = pick(form[key]);
    const groups = {assignments: value => pick(value), worker_slots: value => value, profiles: value => value};
    for (const [group, shape] of Object.entries(groups)) {
      const changed = {};
      for (const [name, value] of Object.entries(form[group] || {}))
        if (!same(shape((saved[group] || {})[name]), shape(value))) changed[name] = shape(value);
      if (Object.keys(changed).length) out[group] = changed;
    }
    for (const key of ["mode", "review", "max_workers", "visible_windows", "max_turns", "job_tokens"])
      if (key in form && form[key] !== saved[key]) out[key] = form[key];
    return out;
  }
  window.AgentKitSettings = {changes};

  const panel = typeof document === "undefined" ? null : document.getElementById("settings-form");
  if (!panel) return;
  const el = (tag, text, cls) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = String(text);
    if (cls) item.className = cls;
    return item;
  };
  function choose(label, options, value, parent) {
    const wrap = el("label");
    const select = el("select");
    options.forEach(([key, title]) => {
      const option = el("option", title);
      option.value = String(key);
      select.append(option);
    });
    select.value = String(value);
    wrap.append(el("span", label), select);
    parent.append(wrap);
    return select;
  }
  function number(label, value, min, parent) {
    const wrap = el("label");
    const input = el("input");
    input.type = "number";input.min = String(min);input.step = "1";input.value = String(value ?? "");
    wrap.append(el("span", label), input);
    parent.append(wrap);
    return input;
  }
  function toggle(label, on, parent) {
    const wrap = el("label", undefined, "check");
    const input = el("input");
    input.type = "checkbox";input.checked = Boolean(on);
    wrap.append(input, el("span", label));
    parent.append(wrap);
    return input;
  }
  function section(title, intro) {
    const box = el("fieldset");
    box.append(el("legend", title));
    if (intro) box.append(el("p", intro, "muted"));
    panel.append(box);
    return box;
  }
  const status = document.getElementById("settings-status");
  const say = text => {status.textContent = text;};
  const post = (route, body) => window.AgentKitOperator.post(route, body);
  const time = value => value ? new Date(value).toLocaleString() : "unknown";

  let saved = null;
  let form = null;
  function modelChoice(title, current, profiles, efforts, parent) {
    const box = el("div", undefined, "choice");
    box.append(el("strong", title));
    const model = choose("Model", profiles.map(name => [name, labels[name]]), current.profile, box);
    model.title = profiles.map(name => labels[name] + ": " + saved.choices.models[name]).join("\n");
    // The local model has no effort setting; its router decides.
    const local = profiles.includes("qwen") ? [["", "Router default"]] : [];
    const effort = choose("Effort", [...local, ...efforts.map(value => [value, value])], current.effort || "", box);
    const sync = () => {
      if (model.value === "qwen") effort.value = "";
      else if (!effort.value) effort.value = efforts.includes("high") ? "high" : efforts[0];
      effort.disabled = model.value === "qwen" || model.disabled;
    };
    model.addEventListener("change", sync);
    sync();
    parent.append(box);
    return {read: () => pick({profile: model.value, effort: effort.value}), model, effort, sync};
  }
  function render(data) {
    saved = data;
    panel.replaceChildren();
    const c = data.choices;
    const notes = [];
    if (data.blocked) notes.push(data.blocked);
    if (data.execution_paused) notes.push("Paused: nothing runs until you press Start.");
    notes.push("Service: " + data.service.state + (data.service.note ? " - " + data.service.note : ""));
    const operator = window.AgentKitOperator?.enabled === true;
    if (!operator) notes.push("This view is read-only. Run `agentkit view` to change settings and start repositories.");
    const top = el("div", undefined, "settings-notes");
    notes.forEach(text => top.append(el("p", text)));
    panel.append(top);

    const manager = section("Assigner", "Turns each job into tasks, once, and again when you change the job. " +
      "A change re-pins every unfinished job; it needs the repository stopped and no chat manager holding a fresh lease.");
    const managerChoice = modelChoice("Assigner model", data.manager, c.control, c.control_efforts, manager);
    data.managers.forEach(row => {
      const pin = row.pin ? row.pin.provider + "/" + row.pin.model + " " + (row.pin.effort || "") : "not pinned yet";
      const lease = row.lease ? row.lease.holder + (row.lease.fresh ? " holds the lease until " : " lease lapsed at ") +
        time(row.lease.expires_at) : "no chat manager attached; AgentKit's own manager runs it";
      manager.append(el("p", "Job " + row.job + ": " + pin + " · " + lease, "muted"));
    });

    const flow = section("Workflow", "How work is split. Feature: one agent plans, builds and tests each " +
      "feature in one session, then an independent review. Separate tasks: builders and testers in fresh sessions.");
    const modes = [["feature", "One agent per feature"], ["separate-tasks", "Separate builder and tester tasks"]];
    if (!modes.some(([key]) => key === data.mode)) modes.push([data.mode, "Unmanaged (" + data.mode + ")"]);
    const mode = choose("Workflow", modes, data.mode, flow);
    const review = section("Review", "Tasks always have to pass their own tests. Once: one AI review of the " +
      "whole job after its tasks merge. Every task: an AI review of each task. Tests only: no AI review.");
    const reviewMode = choose("Review", [["once", "One AI review of the whole job"], ["ai", "AI review of every task"],
      ["off", "Tests only"], ["human", "You review"]], data.review, review);
    const reviewer = modelChoice("Reviewer model", data.reviewer, c.control, c.control_efforts, review);
    const syncReview = () => {reviewer.model.disabled = ["human", "off"].includes(reviewMode.value);reviewer.sync();};
    reviewMode.addEventListener("change", syncReview);

    const workers = section("Worker models", "The model and effort each builder and tester assignment uses.");
    const assignments = {};
    Object.entries(data.assignments).forEach(([name, value]) => {
      const title = name + (value.roles.length ? " (" + value.roles.join(", ") + ")" : "");
      assignments[name] = modelChoice(title, value, c.worker, c.worker_efforts, workers);
    });

    const slots = section("Worker slots", "How many of each worker may run at once; 0 max workers is unlimited.");
    const slotFields = {};
    c.worker_roles.forEach(role => {
      slotFields[role] = choose(role, [["", "AgentKit default"], [1, "1"], [2, "2"], [3, "3"]],
        data.worker_slots[role] ?? "", slots);
    });
    const maxWorkers = number("Max workers", data.max_workers, 0, slots);

    const profiles = section("Models allowed", "A model switched off is never launched.");
    const switches = {};
    Object.entries(data.profiles).forEach(([name, on]) => {switches[name] = toggle(labels[name], on, profiles);});

    const runs = section("Run options");
    const windows = toggle("Show agent windows", data.visible_windows, runs);
    const turns = number("Max turns per session", data.max_turns, 1, runs);
    const budget = number("Job token budget (empty: none)", data.job_tokens, 1, runs);

    form = {
      read: () => ({mode: mode.value, manager: managerChoice.read(), reviewer: reviewer.read(), review: reviewMode.value,
        assignments: Object.fromEntries(Object.entries(assignments).map(([name, field]) => [name, field.read()])),
        worker_slots: Object.fromEntries(Object.entries(slotFields).filter(([, field]) => field.value !== "")
          .map(([role, field]) => [role, Number(field.value)])),
        // An empty number keeps what is saved, so an unset limit is never sent as zero.
        max_workers: maxWorkers.value === "" ? data.max_workers : Number(maxWorkers.value),
        visible_windows: windows.checked, max_turns: turns.value === "" ? data.max_turns : Number(turns.value),
        job_tokens: budget.value === "" ? null : Number(budget.value),
        profiles: Object.fromEntries(Object.entries(switches).map(([name, box]) => [name, box.checked]))}),
    };
    syncReview();
    // Settings change only while the repository is stopped; providers and thinking work any time.
    if (data.blocked) [manager, flow, review, workers, slots, profiles, runs].forEach(box => {box.disabled = true;});

    const accounts = section("Providers", "Hold stops new launches on a provider until you clear it. Works any time.");
    data.providers.forEach(row => {
      const line = el("div", undefined, "provider-row");
      line.append(el("strong", row.provider), el("span", row.status + (row.reason ? " - " + row.reason : ""), "muted"));
      const held = row.status === "HELD" || row.status === "COOLDOWN";
      const reason = el("input");reason.type = "text";reason.placeholder = "Why hold it?";reason.maxLength = 500;
      const button = el("button", held ? "Clear" : "Hold");button.type = "button";
      button.addEventListener("click", () => act(() => post("/api/provider",
        {provider: row.provider, action: held ? "clear" : "hold", reason: held ? "" : reason.value}),
        row.provider + (held ? " cleared." : " held.")));
      if (!held) line.append(reason);
      line.append(button);
      reason.disabled = button.disabled = !operator;
      accounts.append(line);
    });

    const local = section("Local model thinking", "Local Qwen router, for every repository; lasts until the router restarts.");
    const thinking = choose("Thinking", [["default", "Per-model default"], ["on", "On"], ["off", "Off"]],
      data.local_thinking.mode || "default", local);
    thinking.disabled = !data.local_thinking.available || !operator;
    if (!data.local_thinking.available) local.append(el("p", data.local_thinking.message, "muted"));
    thinking.addEventListener("change", () => act(() => post("/api/local-thinking", {mode: thinking.value}),
      "Local thinking: " + thinking.value + "."));

    const actions = el("div", undefined, "settings-actions");
    const save = el("button", "Save settings");save.type = "button";save.disabled = Boolean(data.blocked) || !operator;
    save.addEventListener("click", () => saveSettings().catch(error => say(error.message)));
    actions.append(save);
    panel.append(actions);
  }
  async function saveSettings() {
    const delta = changes(saved, form.read());
    if (!Object.keys(delta).length) return say("Nothing changed.");
    const result = await post("/api/settings", {settings: delta});
    say("Saved " + result.saved.join(", ") + (result.repinned.length ? "; manager re-pinned for " +
      result.repinned.join(", ") : "") + ".");
    await load();
  }
  async function act(action, done) {
    let message;
    try {
      const result = await action();
      message = typeof result === "string" ? result : done;
    } catch (error) {message = error.message;}
    await load();  // the form shows the new state before the message confirms it
    say(message);
  }
  async function load() {
    try {
      const response = await fetch("/api/settings", {cache: "no-store"});
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || "Settings unavailable.");
      render(data);
    } catch (error) {say(error.message);}
  }

  // The repository bar: choose a repository, then Start or Stop it.
  const picker = document.getElementById("repo-select");
  const state = document.getElementById("repo-state");
  const start = document.getElementById("repo-start");
  const stop = document.getElementById("repo-stop");
  let shown = null;
  async function repos() {
    try {
      const response = await fetch("/api/repos", {cache: "no-store"});
      const data = await response.json();
      const current = data.repos.find(row => row.selected);
      if (picker.options.length !== data.repos.length) {
        picker.replaceChildren(...data.repos.map(row => {
          const option = el("option", row.name + " · " + row.root);option.value = row.id;return option;}));
      }
      picker.value = current ? current.id : "";
      const running = current && current.state !== "stopped";
      const waiting = current && current.problems ? " · " + current.problems + " waiting for you (Jobs tab)" : "";
      state.textContent = current ? (running ? "Running" : current.execution_paused ? "Stopped · paused" : "Stopped") + waiting : "";
      state.classList.toggle("alert", Boolean(current && current.problems));
      start.disabled = !window.AgentKitOperator?.enabled || running;
      stop.disabled = !window.AgentKitOperator?.enabled || !running;
      if (shown !== null && shown !== running) await load();
      shown = running;
    } catch (error) {state.textContent = "Repository list unavailable";}
  }
  picker.addEventListener("change", () => {
    document.cookie = "agentkit_repo=" + encodeURIComponent(picker.value) + "; path=/; SameSite=Strict; max-age=31536000";
    location.reload();
  });
  start.addEventListener("click", () => act(async () => {
    if (saved && form && Object.keys(changes(saved, form.read())).length) await saveSettings();
    return (await post("/api/repo-start", {})).message;
  }, "Started.").then(repos));
  stop.addEventListener("click", () => act(async () => (await post("/api/repo-stop", {})).message,
    "Stop requested.").then(repos));
  document.addEventListener("agentkit-controls", () => {repos();if (saved) render(saved);});
  load();
  repos();
  setInterval(repos, 5000);
})();
