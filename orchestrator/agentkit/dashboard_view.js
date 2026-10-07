"use strict";
const $ = id => document.getElementById(id);
let latest = null;
let activeTab = "working";
const pages = {working:0,waiting:0,attention:0,history:0};
const pageSize = 12;
const tabNames = {working:"Working",waiting:"Waiting",attention:"Needs attention",history:"History"};
const explanations = {
  working:"Live owned sessions and active external-manager registrations. Starting sessions are labelled separately.",
  waiting:"These tasks have no live session. Each card explains the recorded prerequisite, pause, review or scheduling reason.",
  attention:"Ownership, liveness or task state needs inspection. Uncertain records are not counted as working or silently treated as finished.",
  history:"Stopped sessions and terminal tasks. Stale means an expired heartbeat or obsolete state, not successful completion. Transfers show recorded continuation ownership."
};
const tokenFields = [
  ["input_tokens", "Reported input"], ["output_tokens", "Output incl. thinking"], ["thinking_tokens", "Thinking (within output)"],
  ["cached_input_tokens", "Cache reused across turns"], ["cache_write_input_tokens", "New cached input"]
];
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function known(value) {
  return value !== null && value !== undefined && value !== "" && value !== "unknown";
}
function text(value) { return known(value) ? String(value) : "unknown"; }
function pretty(value) { return text(value).replaceAll("_", " "); }
function number(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : "unknown";
}
function time(value) {
  if (!known(value)) return "unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "unknown" : date.toLocaleTimeString();
}
function alive(value) { return value === true ? "alive" : value === false ? "stopped" : "unknown"; }
function provider(value) {
  if (/claude/i.test(value || "")) return "Claude";
  if (/codex/i.test(value || "")) return "Codex";
  return text(value);
}
function state(row, task, manager = false) {
  const value = window.AgentKitCategories.classify(row,task,manager,latest || {});
  return [value.label,value.color];
}
function facts(card, entries) {
  const list = node("dl", null, "facts");
  entries.forEach(([label, value]) => {
    list.append(node("dt", label), node("dd", text(value)));
  });
  card.append(list);
}
function scope(card, task) {
  const writes = Array.isArray(task?.expected_write) ? task.expected_write : [];
  const reads = Array.isArray(task?.expected_read) ? task.expected_read : [];
  const wrap = node("details", null, "scope");
  wrap.append(node("summary", "Assigned files · " + writes.length + " write / " + reads.length + " read"));
  if (!writes.length && !reads.length) wrap.append(node("span", "Not recorded", "muted"));
  else {
    const list = node("ul");
    writes.forEach(path => list.append(node("li", "Write · " + String(path))));
    reads.forEach(path => list.append(node("li", "Read · " + String(path))));
    wrap.append(list);
  }
  card.append(wrap);
}
function tokens(card, usage) {
  const grid = node("div", null, "tokens");
  tokenFields.forEach(([key, label]) => {
    const cell = node("div", null, "token");
    cell.append(node("span", label), node("strong", number(usage?.[key])));
    grid.append(cell);
  });
  card.append(grid);
  const reported = tokenFields.some(([key]) => Number.isSafeInteger(usage?.[key]));
  const qualifier = usage?.counter_scope === "session_total" ? "provider session totals" : reported ? (usage?.complete === true ? "complete" : "partial") : "unavailable";
  card.append(node("p", "Usage: " + qualifier + " · Source: " + text(usage?.source), "token-source"));
}
function taskIntro(card, task, fallback) {
  const heading = task ? "#" + number(task.id) + " · " + text(task.title) : fallback;
  card.append(node("h3", heading, "tasktitle"));
  card.append(node("p", task?.description || (task ? "No overview recorded." :
    "No task assignment is recorded."), "overview"));
}
function sessionCard(row, task, manager = false) {
  const card = node("article", null, "card");
  const [label, color] = state(row, task, manager);
  const head = node("div", null, "cardhead");
  head.append(node("span", pretty(manager ? "External manager" : row.role), "role"),
    node("span", label, "badge " + color));
  card.append(head);
  const identity = manager ? text(row.holder) + " · " + provider(row.provider) :
    provider(row.provider) + " · Run #" + number(row.id);
  card.append(node("div", identity, "identity"));
  taskIntro(card, task, manager ? "Project coordination" : "No numbered task assigned");
  const reported = manager ? text(row.model) + " (reported registration)" :
    text(row.observed_model) + (row.model_verified ? " (verified)" : " (unverified)");
  const liveness = manager ? pretty(row.state) :
    "Monitor " + alive(row.monitor_alive) + " · Agent " + alive(row.child_alive);
  facts(card, [
    [manager ? "Saved manager model" : "Requested model", manager ? row.model : row.requested_model],
    [manager ? "Actual chat model" : "Reported model", manager ? "Not verified by AgentKit" : reported],
    [manager ? "Saved job effort" : "Requested effort", manager ? row.effort : row.requested_effort],
    [manager ? "Actual chat effort" : "Reported effort", manager ? "Not reported by chat" : row.observed_effort],
    ["Agent turns", number(row.usage?.agent_turns)],
    ["Liveness", liveness],
    ["Recorded state", row.status || row.state],
    ["Heartbeat", time(row.heartbeat_at)],
    ["Session ID", row.session_id],
    ["Job", row.job_id]
  ]);
  if (task) scope(card, task);
  tokens(card, row.usage);
  const activity = node("div", null, "activity");
  const events = Array.isArray(row.stream?.activity) ? row.stream.activity : [];
  activity.append(node("div", "Recent activity"));
  if (!events.length) activity.append(node("div", manager ? "Heartbeat metadata only." :
    "No activity recorded."));
  events.slice(-3).forEach(event => activity.append(node("div",
    time(event.at) + " · " + pretty(event.name || event.kind) +
    (event.state ? " · " + pretty(event.state) : ""))));
  if (row.ownership_uncertain || row.liveness_risk)
    activity.append(node("div", "Process ownership or liveness needs attention."));
  card.append(activity);
  return card;
}
function taskCard(task) {
  const card = node("article", null, "card");
  const [label, color] = state(task, task);
  const head = node("div", null, "cardhead");
  head.append(node("span", pretty(task.role), "role"), node("span", label, "badge " + color));
  card.append(head, node("div", provider(task.adapter) + " · No active session", "identity"));
  taskIntro(card, task, "");
  facts(card, [["Task state", task.status], ["Requested model", task.model],
    ["Last activity", task.last_event], ["Next check", task.retry_at ? time(task.retry_at) : "unknown"]]);
  scope(card, task);
  return card;
}
function empty(target, message) { target.append(node("div", message, "empty")); }
function render(view) {
  if (window.AgentKitActivity) window.AgentKitActivity.update(view);
  if (window.AgentKitRecovery && $("recovery")) window.AgentKitRecovery.render(view, $("recovery"));
  window.AgentKitAllowance.render(view, $("allowance"));
  $("project").textContent = "AgentKit · " + text(view.project);
  window.AgentKitTeam.updateSnapshot(view);
  const tasks = Array.isArray(view.tasks) ? view.tasks : [];
  const processes = Array.isArray(view.processes) ? view.processes : [];
  const managers = Array.isArray(view.external_managers) ? view.external_managers : [];
  const byId = new Map(tasks.map(task => [task.id, task]));
  const groups = window.AgentKitCategories.group(view);
  const selected = $("history-filter").value;
  const filtered = groups[activeTab].filter(item => activeTab !== "history" || selected === "all" || item.state.historyKind === selected);
  pages[activeTab] = Math.min(pages[activeTab],Math.max(0,Math.ceil(filtered.length / pageSize) - 1));
  const start = pages[activeTab] * pageSize;
  const items = filtered.slice(start,start + pageSize);
  $("page-controls").hidden = filtered.length <= pageSize;
  $("page-previous").disabled = start === 0;
  $("page-next").disabled = start + pageSize >= filtered.length;
  $("page-range").textContent = filtered.length ? (start + 1) + " - " + Math.min(start + pageSize,filtered.length) + " of " + filtered.length : "0 records";
  const sessions = $("sessions");const taskGrid = $("tasks");const summary = $("summary");
  sessions.replaceChildren();taskGrid.replaceChildren();summary.replaceChildren();
  for (const item of items) {
    const card = item.taskOnly ? taskCard(item.task) : sessionCard(item.row,item.task,item.manager);
    card.append(node("p", item.state.reason, "overview"));
    (item.taskOnly ? taskGrid : sessions).append(card);
  }
  $("sessions-heading").hidden = !items.some(item => !item.taskOnly);
  $("tasks-heading").hidden = !items.some(item => item.taskOnly);
  $("tasks-heading").textContent = activeTab === "waiting" ? "Tasks awaiting prerequisites or scheduling" : "Task records";
  if (!items.length) empty(sessions, "No records in " + tabNames[activeTab].toLowerCase() + ".");
  $("history-controls").hidden = activeTab !== "history";
  $("activity-panel").setAttribute("aria-labelledby", "tab-" + activeTab);
  $("tab-explanation").textContent = explanations[activeTab];
  for (const name of Object.keys(groups)) {
    const button = $("tab-" + name);
    button.textContent = tabNames[name] + " (" + groups[name].length + ")";
    button.setAttribute("aria-selected",String(activeTab === name));button.tabIndex = activeTab === name ? 0 : -1;
  }
  const running = groups.working.length;
  const waiting = groups.waiting.length;
  const attention = groups.attention.length;
  const counted = processes.filter(row => tokenFields.some(([key]) =>
    Number.isSafeInteger(row.usage?.[key]))).length;
  [[running, "Working sessions"], [waiting, "Waiting tasks"], [attention, "Need attention"],
   [counted + "/" + processes.length, "Sessions with recorded tokens"]].forEach(([value, label]) => {
    const stat = node("div", null, "stat");
    stat.append(node("strong", value), node("span", label));
    summary.append(stat);
  });
  const notices = [];
  if (view.execution_paused) notices.push("Execution paused by the operator. No worker launches, model checks, automatic retries or integration.");
  if (view.workflow_mode) notices.push("Worker policy: " + view.workflow_mode + ".");
  if (view.review_mode) notices.push("Applied review policy: " + view.review_mode + ". Team choices above are a preview until applied.");
  if (view.message) notices.push(String(view.message));
  if (view.bounded) notices.push("Only the most recent 200 records per table are shown.");
  $("notice").textContent = notices.join(" ");
  $("notice").hidden = !notices.length;
}
$("history-filter").addEventListener("change", () => {pages.history = 0;if (latest) render(latest);});
$("page-previous").addEventListener("click", () => {pages[activeTab] = Math.max(0,pages[activeTab] - 1);if (latest) render(latest);});
$("page-next").addEventListener("click", () => {pages[activeTab] += 1;if (latest) render(latest);});
const tabs = Object.keys(tabNames);
for (const name of tabs) {
  const button = $("tab-" + name);
  button.addEventListener("click", () => {activeTab = name;if (latest) render(latest);});
  button.addEventListener("keydown", event => {
    const index = tabs.indexOf(activeTab);
    const target = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
    if (target === null) return;
    event.preventDefault();activeTab = tabs[target];if (latest) render(latest);$("tab-" + activeTab).focus();
  });
}
async function poll() {
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 10000);
  try {
    const response = await fetch("/api/snapshot", {cache:"no-store", signal:abort.signal});
    if (!response.ok) throw new Error("Snapshot unavailable");
    latest = await response.json();
    render(latest);
    $("connection").textContent = "Updated " + time(latest.at) + " · Polling locally";
  } catch {
    $("connection").textContent = "Connection unavailable · Retrying";
    $("notice").hidden = false;
    $("notice").textContent = latest ? "Showing the last saved view. Local polling will retry." :
      "The local snapshot is unavailable. Polling will retry.";
  } finally {
    clearTimeout(timeout);
    setTimeout(poll, 2500);
  }
}
window.AgentKitView = {state, number, provider};
poll();
