"use strict";
// Phone: problems reach you by email and your one-word reply answers them. One mailbox for every repository.
(() => {
  const box = typeof document === "undefined" ? null : document.getElementById("phone-form");
  if (!box) return;
  const status = document.getElementById("settings-status");
  const el = (tag, text, cls) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = String(text);
    if (cls) item.className = cls;
    return item;
  };
  function field(label, type, value, parent) {
    const wrap = el("label");
    const input = el("input");
    input.type = type;
    if (type === "checkbox") {input.checked = Boolean(value);wrap.className = "check";wrap.append(input, el("span", label));}
    else {input.value = value ?? "";wrap.append(el("span", label), input);}
    parent.append(wrap);
    return input;
  }
  async function load() {
    let data;
    try {
      const response = await fetch("/api/phone", {cache: "no-store"});
      data = await response.json();
      if (!response.ok) throw new Error(data.message || "Phone settings unavailable.");
    } catch (error) {box.replaceChildren(el("p", error.message, "muted"));return;}
    const s = data.settings;
    const operator = window.AgentKitOperator?.enabled === true;
    const set = el("fieldset");
    set.append(el("legend", "Phone (email)"), el("p", "When a task stops you get one email with the replies " +
      "that fix it; answer with one word (retry, skip, hint <words> ...). Use a separate mailbox for AgentKit " +
      "with an app password (Gmail: Google account > Security > App passwords). The password is encrypted " +
      "for your Windows user and never shown again.", "muted"));
    const on = field("Send problems to my phone", "checkbox", s.enabled, set);
    const done = field("Also tell me when a job is done", "checkbox", s.notify_done, set);
    const agent = field("AgentKit mailbox (sends and reads replies)", "email", s.agent_address, set);
    const secret = field(data.password_saved ? "App password (saved; type to replace)" : "App password", "password", "", set);
    secret.autocomplete = "new-password";
    const user = field("Your address (on your phone)", "email", s.user_address, set);
    const smtp = field("Sending server", "text", s.smtp_host, set);
    const smtpPort = field("Sending port", "number", s.smtp_port, set);
    const imap = field("Reading server", "text", s.imap_host, set);
    const imapPort = field("Reading port", "number", s.imap_port, set);
    if (data.error) set.append(el("p", "Last mail problem: " + data.error, "muted"));
    else if (data.polled_at) set.append(el("p", "Replies last read " + new Date(data.polled_at).toLocaleString() + ".", "muted"));
    const actions = el("div", undefined, "settings-actions");
    const save = el("button", "Save phone settings");save.type = "button";
    const test = el("button", "Send a test message");test.type = "button";
    [save, test].forEach(button => {button.disabled = !operator;actions.append(button);});
    set.append(actions);
    box.replaceChildren(set);
    save.addEventListener("click", async () => {
      try {
        const result = await window.AgentKitOperator.post("/api/phone", {password: secret.value, settings: {
          enabled: on.checked, notify_done: done.checked, agent_address: agent.value.trim(), user_address: user.value.trim(),
          smtp_host: smtp.value.trim(), smtp_port: Number(smtpPort.value), imap_host: imap.value.trim(),
          imap_port: Number(imapPort.value)}});
        status.textContent = result.message;
        await load();
      } catch (error) {status.textContent = error.message;}
    });
    test.addEventListener("click", async () => {
      try {status.textContent = (await window.AgentKitOperator.post("/api/phone-test", {})).message;}
      catch (error) {status.textContent = error.message;}
    });
  }
  document.addEventListener("agentkit-controls", load);
  load();
})();
