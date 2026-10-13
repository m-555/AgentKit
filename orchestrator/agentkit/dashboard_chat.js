"use strict";
// Manager tab (#4): the manager's conversation for one job on a monitor screen, and a prompt to write to it.
(() => {
  const $ = id => document.getElementById(id);
  const list = $("chat-messages"), select = $("chat-job"), text = $("chat-text"), send = $("chat-send"), status = $("chat-status");
  if (!list || !select) return;
  let shown = "";
  // Who manages the job now, so a waiting message is never a mystery.
  let presence = $("chat-manager");
  if (!presence) {
    presence = document.createElement("p");
    presence.className = "muted";
    presence.id = "chat-manager";
    list.before(presence);
  }

  const clock = at => {
    const stamp = new Date(at);
    return Number.isNaN(stamp.getTime()) ? "--:--:--" : stamp.toLocaleTimeString([], {hour12: false});
  };
  // A monitor shows plain text: drop markdown emphasis and code ticks, keep lists and line breaks.
  const plain = value => String(value || "").replace(/\*\*(.+?)\*\*/g, "$1").replace(/`/g, "");
  // Sessions in the user's words: the coordinator is the automatic manager.
  const named = value => String(value || "").replace(/^coordinator process (\d+)$/, "automatic manager #$1")
    .replace(/^external manager$/, "VS Code chat");

  function line(entry) {
    const item = document.createElement("div");
    item.className = "line";
    const time = document.createElement("span");
    time.className = "time";
    time.textContent = clock(entry.at);
    const who = document.createElement("span");
    const user = entry.from === "user";
    who.className = "who" + (user ? " user" : "");
    who.textContent = user ? "You" : "Manager";
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = user ? (entry.delivered_to ? "  · read by " + named(entry.delivered_to) : "  · waiting for the manager")
      : (entry.source ? "  · " + named(entry.source) : "");
    const body = document.createElement("span");
    body.className = "text";
    body.textContent = plain(entry.text);  // never HTML
    item.append(time, who, meta, body);
    return item;
  }

  async function jobs() {
    const response = await fetch("/api/manager-chat", {cache: "no-store"});
    const data = await response.json();
    const current = select.value;
    select.replaceChildren(...(data.jobs || []).map(id => { const o = document.createElement("option"); o.value = o.textContent = id; return o; }));
    if (current && [...select.options].some(o => o.value === current)) select.value = current;
  }

  async function refresh() {
    if (!select.value) return;
    const response = await fetch("/api/manager-chat?job=" + encodeURIComponent(select.value), {cache: "no-store"});
    if (!response.ok) return;
    const data = await response.json();
    presence.textContent = data.manager || "";
    const signature = JSON.stringify(data.messages);
    if (signature === shown) return;
    shown = signature;
    const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 8;
    const messages = data.messages || [];
    if (!messages.length) {
      const empty = document.createElement("div");
      empty.className = "quiet";
      empty.textContent = "No conversation yet for this job.";
      list.replaceChildren(empty);
    } else {
      list.replaceChildren(...messages.map(line));
    }
    if (atBottom || !list.dataset.seen) list.scrollTop = list.scrollHeight;
    list.dataset.seen = "1";
  }

  async function submit() {
    const message = text.value.trim();
    if (!message || !select.value) return;
    try {
      const operator = window.AgentKitOperator;
      if (!operator || !operator.enabled) throw new Error("Start the view with --operator-controls to send messages.");
      const result = await operator.post("/api/manager-chat", {job_id: select.value, text: message});
      text.value = "";
      status.textContent = result.message || "Sent.";
      shown = "";
      await refresh();
      list.scrollTop = list.scrollHeight;
    } catch (error) {
      status.textContent = error.message;
    }
  }

  send.addEventListener("click", submit);
  text.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });
  select.addEventListener("change", () => { shown = ""; list.dataset.seen = ""; refresh(); });
  jobs().then(refresh);
  setInterval(() => { jobs().then(refresh).catch(() => {}); }, 4000);
})();
