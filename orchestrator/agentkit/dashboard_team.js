"use strict";
(() => {
  const modelCatalog = {
    opus: {label:"Claude Opus 5.5", model:"claude-opus-5-5"},
    sol: {label:"Codex GPT-6.1 Sol", model:"gpt-6.1-sol"},
    qwen: {label:"Local Qwen3.8 · future projects — qualification required",
      model:"local/qwen3.8-27b"}
  };
  const roles = [
    ["coordinator", "Planner / manager", "opus", true], ["reviewer", "Reviewer", "sol", true],
    ["backend-builder", "Backend builder", "opus"], ["backend-tester", "Backend tester", "opus"],
    ["frontend-builder", "Frontend builder", "sol"], ["frontend-tester", "Frontend tester", "sol"]
  ];
  const workerEfforts = ["low", "medium", "high", "xhigh", "max"];
  const controlEfforts = ["medium", "high", "xhigh", "max"];
  function pools(state) {
    const values = ["backend_builders", "backend_testers", "frontend_builders", "frontend_testers"]
      .map(key => state[key]);
    if (values.some(value => !Number.isInteger(value) || ![2, 3].includes(value)))
      throw new Error("Choose 2 or 3 slots for each builder and tester pool.");
    return values;
  }
  function reviewChoice(state) {
    const value = state.workflow_review || "ai";
    if (!["human", "ai"].includes(value)) throw new Error("Choose human or AI review.");
    return value;
  }
  function totalSessions(state) {
    return (reviewChoice(state) === "human" || state.review_mode === "combined" ? 1 : 2) + pools(state).reduce((a, b) => a + b, 0);
  }
  function assignment(choice, control, state) {
    if (!choice || !modelCatalog[choice.profile]) throw new Error("Choose a known model preset.");
    if (choice.profile === "qwen") {
      if (control) throw new Error("Qwen is unavailable for manager and reviewer roles.");
      if (state.future_project !== true) throw new Error("Qwen requires a qualified future-project preview.");
      const model = String(state.qwen_model || modelCatalog.qwen.model).trim();
      if (!model || model.length > 200 || /[\r\n]/.test(model)) throw new Error("Enter a local model ID.");
      return {profile:"qwen", model};
    }
    if (!(control ? controlEfforts : workerEfforts).includes(choice.effort))
      throw new Error("Choose an effort supported by this role.");
    return {profile:choice.profile, model:modelCatalog[choice.profile].model, effort:choice.effort};
  }
  function configFor(state) {
    const coordinator = assignment(state.coordinator, true, state);
    const review = reviewChoice(state);
    const controls = {coordinator};
    if (review === "ai") {
      const reviewer = assignment(state.reviewer, true, state);
      controls.reviewer = state.review_mode === "combined" ? {...coordinator} : reviewer;
    }
    return {max_workers:pools(state).reduce((a, b) => a + b, 0),
      workflow:{mode:"separate-tasks", review, worker_slots:Object.fromEntries(roles.slice(2).map(([key], i) => [key, pools(state)[i]]))},
      model_policy:{roles:controls,
        assignments:Object.fromEntries(roles.slice(2).map(([key]) => [key, assignment(state[key], false, state)]))}};
  }
  function toYaml(state) {
    const config = configFor(state);
    const lines = ["# Team setup preview; no runtime changes have been applied.",
      "# Manager source: " + (state.manager_mode === "native-external" ? "current native external manager" : "supervised session"),
      "# Review layout: " + (reviewChoice(state) === "human" ? "user reviews; no AI reviewer" : state.review_mode === "combined" ? "combined with manager" : "independent AI reviewer"),
      "# Worker slots: backend builders " + pools(state)[0] + ", backend testers " + pools(state)[1],
      "# Frontend worker slots: builders " + pools(state)[2] + ", testers " + pools(state)[3],
      "# Separate-task roles automatically use the matching assignment name below.",
      "# An explicit task model_assignment overrides the role choice.",
      "# Session allocation: " + totalSessions(state) + "; provider limits can reduce concurrency."];
    if (Object.values(config.model_policy.assignments).some(value => value.profile === "qwen"))
      lines.push("# Qwen: qualified future projects only; current tasks remain unchanged.",
        "# Future TEST_ONLY task documentation: complexity: easy.",
        "# Current Qwen eligibility is RESEARCH only; qualify task-kind support before use.");
    lines.push("max_workers: " + config.max_workers, "workflow:", "  mode: separate-tasks",
      "  review: " + config.workflow.review, "  worker_slots:");
    Object.entries(config.workflow.worker_slots).forEach(([key, value]) => lines.push("    " + key + ": " + value));
    lines.push("model_policy:");
    [["roles", config.model_policy.roles], ["assignments", config.model_policy.assignments]].forEach(([group, entries]) => {
      lines.push("  " + group + ":");
      Object.entries(entries).forEach(([key, value]) => {
        lines.push("    " + key + ":");
        Object.entries(value).forEach(([name, field]) => lines.push("      " + name + ": " + JSON.stringify(field)));
      });
    });
    return lines.join("\n") + "\n";
  }
  let dom = null;
  let nativeManager = null;
  function updateSnapshot(view) {
    if (!dom) return;
    const managers = Array.isArray(view.external_managers) ? view.external_managers : [];
    const current = managers.find(row => row.state === "ACTIVE");
    const profile = Object.keys(modelCatalog).find(key => key !== "qwen" &&
      modelCatalog[key].model === current?.model &&
      current.provider === (key === "opus" ? "claude-code" : "codex"));
    nativeManager = profile && controlEfforts.includes(current.effort) ? {profile, effort:current.effort} : null;
    dom.current.textContent = current ? "Current registered manager: " + String(current.holder || "unknown") +
      " · " + String(current.provider || "unknown") + " · " + String(current.model || "unknown") +
      " · " + String(current.effort || "unknown") + " (reported metadata)" :
      "Current external manager: no active registration recorded.";
    dom.nativeOption.disabled = !nativeManager;
    if (!nativeManager && dom.managerMode.value === "native-external") dom.managerMode.value = "supervised";
    refresh();
  }
  const api = {modelCatalog, configFor, totalSessions, toYaml, updateSnapshot};
  (typeof window === "undefined" ? globalThis : window).AgentKitTeam = api;
  if (typeof document === "undefined" || !document.getElementById("team-controls")) return;
  const element = (tag, text) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = String(text);
    return item;
  };
  const controlRoot = document.getElementById("team-controls");
  const fields = {};
  function selectField(label, choices, value, parent = controlRoot) {
    const wrap = element("label");
    wrap.append(element("span", label));
    const select = element("select");
    choices.forEach(([key, title]) => {
      const option = element("option", title);
      option.value = key;
      select.append(option);
    });
    select.value = value;
    wrap.append(select);
    parent.append(wrap);
    return select;
  }
  const managerMode = selectField("Manager source", [["supervised","Supervised manager session"],
    ["native-external","Use current native external manager"]], "supervised");
  const reviewMode = selectField("Review workflow", [["human","Human review - lower expected usage"],
    ["ai","Independent AI review - higher expected usage"]], "human");
  const backendBuilders = selectField("Backend builder slots", [["2","2"],["3","3"]], "2");
  const backendTesters = selectField("Backend tester slots", [["2","2"],["3","3"]], "2");
  const frontendBuilders = selectField("Frontend builder slots", [["2","2"],["3","3"]], "2");
  const frontendTesters = selectField("Frontend tester slots", [["2","2"],["3","3"]], "2");
  roles.forEach(([key, label, initial, control]) => {
    const group = element("fieldset");
    group.append(element("legend", label));
    const choices = Object.entries(modelCatalog).filter(([name]) => !control || name !== "qwen")
      .map(([name, preset]) => [name, preset.label + (name === "qwen" ? "" : " · " + preset.model)]);
    const model = selectField("Model", choices, initial, group);
    const effort = selectField("Effort", (control ? controlEfforts : workerEfforts).map(value => [value,value]), "xhigh", group);
    fields[key] = {model, effort, initial, control};
    controlRoot.append(group);
  });
  dom = {managerMode, reviewMode, current:document.getElementById("team-current-manager"),
    nativeOption:managerMode.querySelector('[value="native-external"]')};
  dom.nativeOption.disabled = true;
  const future = document.getElementById("team-future");
  const localModel = document.getElementById("team-local-model");
  localModel.value = modelCatalog.qwen.model;
  function readState() {
    return {...Object.fromEntries(roles.map(([key]) => [key,
      {profile:fields[key].model.value, effort:fields[key].effort.value}])),
      backend_builders:Number(backendBuilders.value), backend_testers:Number(backendTesters.value),
      frontend_builders:Number(frontendBuilders.value), frontend_testers:Number(frontendTesters.value),
      review_mode:"separate", workflow_review:reviewMode.value, manager_mode:managerMode.value,
      future_project:future.checked, qwen_model:localModel.value};
  }
  let savedReviewer = null;
  function refresh() {
    if (managerMode.value === "native-external" && nativeManager) {
      fields.coordinator.model.value = nativeManager.profile;
      fields.coordinator.effort.value = nativeManager.effort;
    }
    const humanReview = reviewMode.value === "human";
    const combined = false;
    if (combined && !savedReviewer) savedReviewer = [fields.reviewer.model.value, fields.reviewer.effort.value];
    if (!combined && savedReviewer) {
      [fields.reviewer.model.value, fields.reviewer.effort.value] = savedReviewer;
      savedReviewer = null;
    }
    if (combined) {
      fields.reviewer.model.value = fields.coordinator.model.value;
      fields.reviewer.effort.value = fields.coordinator.effort.value;
    }
    Object.entries(fields).forEach(([key, field]) => {
      const qwenOption = field.model.querySelector('[value="qwen"]');
      if (qwenOption) qwenOption.disabled = !future.checked;
      if (!future.checked && field.model.value === "qwen") field.model.value = field.initial;
      const locked = key === "reviewer" && (combined || humanReview) || key === "coordinator" && managerMode.value === "native-external";
      field.model.disabled = locked;
      field.effort.disabled = locked || field.model.value === "qwen";
    });
    localModel.disabled = !future.checked;
    try {
      const state = readState();
      document.getElementById("team-yaml").textContent = toYaml(state);
      document.getElementById("team-total").textContent = totalSessions(state) + " sessions = " +
        (humanReview ? "1 planner/manager; user reviews" : "1 planner/manager + 1 AI reviewer") + " + " +
        state.backend_builders + " backend builders + " + state.backend_testers + " backend testers + " +
        state.frontend_builders + " frontend builders + " + state.frontend_testers + " frontend testers.";
      document.getElementById("team-download").disabled = false;
      document.getElementById("team-apply").disabled = window.AgentKitOperator?.enabled !== true;
    } catch (error) {
      document.getElementById("team-yaml").textContent = String(error.message);
      document.getElementById("team-download").disabled = true;
      document.getElementById("team-apply").disabled = true;
    }
  }
  document.addEventListener("agentkit-controls", refresh);
  document.getElementById("team-apply").addEventListener("click", async () => {
    const status = document.getElementById("team-apply-status");
    try {
      if (managerMode.value === "native-external") throw new Error("Native manager registration needs its adapter-specific setup; download this preview instead.");
      await window.AgentKitOperator.post("/api/team-profile", configFor(readState()));
      status.textContent = "Profile saved for new jobs. Existing pause state is preserved; no sessions started.";
    } catch (error) {status.textContent = error.message;}
  });
  controlRoot.addEventListener("change", refresh);
  future.addEventListener("change", refresh);
  localModel.addEventListener("input", refresh);
  document.getElementById("team-download").addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([toYaml(readState())], {type:"application/yaml;charset=utf-8"}));
    const link = element("a");
    link.href = url;
    link.download = "agentkit-team-preview.yaml";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  refresh();
})();
