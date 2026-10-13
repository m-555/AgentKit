"use strict";
// Agents tab: each agent's own words and tools on a monitor screen, read-only, as in its window.
(() => {
  const $ = id => document.getElementById(id);
  const list = $("agent-list"), screen = $("agent-screen"), title = $("agent-title"), state = $("agent-state");
  if (!list || !screen) return;
  const PREFIXES = /^(mcp__plugin_agentkit_agentkit__|mcp__agentkit__|agentkit_)/;
  let agents = [], selected = null, shown = "", onScreen = null;

  const clock = at => {
    const stamp = new Date(at);
    return Number.isNaN(stamp.getTime()) ? "--:--:--" : stamp.toLocaleTimeString([], {hour12: false});
  };
  const plain = value => String(value || "").replace(/\*\*(.+?)\*\*/g, "$1").replace(/`/g, "");
  // "tool NAME started" becomes "· uses NAME"; quota notices and lifecycle are status, not conversation.
  function toolName(value) {
    const words = String(value || "").trim().split(/\s+/);
    if (words.length < 2 || words[0] !== "tool" || !/^(started|tool_started|tool_use)$/.test(words[words.length - 1])) return null;
    return words[1].replace(PREFIXES, "");
  }

  function span(className, value) {
    const item = document.createElement("span");
    item.className = className;
    item.textContent = value;
    return item;
  }

  function button(agent) {
    const item = document.createElement("button");
    item.type = "button";
    item.setAttribute("aria-pressed", String(agent.id === selected));
    const name = document.createElement("strong");
    name.textContent = "#" + agent.id + " " + agent.speaker + (agent.task_id ? " · task " + agent.task_id : "");
    item.append(name, span("state", agent.title || agent.job_id || ""),
      span("state" + (agent.running ? " running" : ""), (agent.running ? "working" : agent.status.toLowerCase()) + " · " + (agent.model || agent.provider)));
    item.addEventListener("click", () => { selected = agent.id; shown = ""; render(); screenRefresh(); });
    return item;
  }

  function render() {
    if (!agents.length) {
      list.replaceChildren(span("muted", "No agent has run yet."));
      return;
    }
    list.replaceChildren(...agents.map(button));
  }

  async function listRefresh() {
    const response = await fetch("/api/agents", {cache: "no-store"});
    if (!response.ok) return;
    agents = (await response.json()).agents || [];
    if (!agents.some(agent => agent.id === selected)) selected = agents.length ? agents[0].id : null;
    render();
  }

  async function screenRefresh() {
    const agent = agents.find(item => item.id === selected);
    if (!agent) return;
    title.textContent = "#" + agent.id + " · " + agent.speaker + (agent.task_id ? " · task " + agent.task_id : "") + " · " + (agent.model || agent.provider);
    state.textContent = agent.running ? "working" : agent.status.toLowerCase();
    const response = await fetch("/api/activity/" + agent.id, {cache: "no-store"});
    if (!response.ok) return;
    const data = await response.json();
    const signature = agent.id + JSON.stringify(data.messages);
    if (signature === shown) return;
    const fresh = onScreen !== agent.id;  // a newly chosen agent opens at its latest line
    shown = signature;
    onScreen = agent.id;
    const atBottom = screen.scrollTop + screen.clientHeight >= screen.scrollHeight - 8;
    const rows = [];
    for (const message of data.messages || []) {
      if (message.kind === "message") {
        const item = document.createElement("div");
        item.className = "line";
        item.append(span("time", clock(message.at)), span("who", agent.speaker), span("text", plain(message.text)));
        rows.push(item);
      } else {
        const name = toolName(message.text);
        if (name) {
          const item = document.createElement("div");
          item.className = "tool";
          item.append(span("time", clock(message.at)), document.createTextNode("· uses " + name));
          rows.push(item);
        }
      }
    }
    if (!rows.length) rows.push(span("quiet", "Nothing said yet."));
    screen.replaceChildren(...rows);
    if (atBottom || fresh) screen.scrollTop = screen.scrollHeight;
  }

  function tick() {
    const panel = $("panel-agents");
    if (panel && panel.hidden) return;
    listRefresh().then(screenRefresh).catch(() => {});
  }
  window.addEventListener("agentkit-tab", event => { if (event.detail === "main-tab-agents") tick(); });
  setInterval(tick, 3000);
  tick();
})();
