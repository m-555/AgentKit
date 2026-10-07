"use strict";
(() => {
  let token = null;
  const api = {enabled:false, post};
  window.AgentKitOperator = api;
  const root = document.getElementById("review-jobs");
  if (!root) return;
  const cards = new Map();
  function node(tag, text) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = String(text);
    return element;
  }
  async function post(route, data) {
    if (!api.enabled || !token) throw new Error("Local operator controls are disabled.");
    const response = await fetch(route, {method:"POST", headers:{"Content-Type":"application/json",
      "X-AgentKit-Token":token}, body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || "Action refused.");
    return result;
  }
  function card(packet) {
    const item = node("article");item.className = "card team";
    item.append(node("h3", packet.job_id + " / revision " + packet.revision),
      node("p", "Preview commit: " + packet.head),
      node("p", "Checkout to test: " + packet.preview_path));
    const requests = node("details");requests.append(node("summary", "User requests"));
    packet.requests.forEach(request => requests.append(node("p", request.text)));
    if (packet.status === "DONE") item.append(node("p", "Accepted feature - report any follow-up changes below."));
    item.append(requests);
    const criteria = node("ul");
    packet.acceptance.forEach(value => criteria.append(node("li", value)));
    item.append(node("h4", "Acceptance criteria"), criteria,
      node("p", packet.checks.passed ? "Combined checks: PASS" : "Combined checks: pending"));
    if (packet.cancelled_tasks.length) item.append(node("p", "Cancelled tasks to account for: " + packet.cancelled_tasks.join(", ")));
    packet.reasons.forEach(reason => item.append(node("p", reason)));
    const evidence = node("textarea");evidence.maxLength = 4000;evidence.rows = 3;
    evidence.placeholder = "Describe what you tested, or explain the changes you need.";
    const label = node("label", "Your test notes or requested changes");label.append(evidence);item.append(label);
    const tested = node("input");tested.type = "checkbox";
    const testedLabel = node("label", "I tested this preview and checked its requirements");
    testedLabel.append(tested);item.append(testedLabel);
    const status = node("p");status.setAttribute("role", "status");
    const approve = node("button", "Approve tested preview");
    const changes = node("button", "Request changes");
    approve.hidden = packet.review !== "human" || packet.status === "DONE";
    const refresh = node("button", "Load changed preview");refresh.hidden = true;
    let stale = false;let sending = false;
    const update = () => {
      approve.disabled = !api.enabled || !packet.ready || stale || sending || !tested.checked || !evidence.value.trim();
      changes.disabled = !api.enabled || !packet.ready || stale || sending || !evidence.value.trim();
    };
    async function decide(verdict) {
      sending = true;update();
      try {
        const result = await post("/api/review-decision", {job_id:packet.job_id, revision:packet.revision,
          head:packet.head, digest:packet.digest, verdict, evidence:evidence.value});
        status.textContent = verdict === "PASS" ? "Accepted " + result.head + ". Main was not published." :
          "Feedback saved. The planner will make bounded correction tasks when supervision resumes.";
        if (verdict === "PASS") {
          packet.status = "DONE";approve.hidden = true;testedLabel.hidden = true;
          evidence.value = "";stale = false;
        } else stale = true;
      } catch (error) {status.textContent = error.message;}
      sending = false;update();
    }
    approve.addEventListener("click", () => decide("PASS"));
    changes.addEventListener("click", () => decide("CHANGES"));
    tested.addEventListener("change", update);evidence.addEventListener("input", update);
    refresh.addEventListener("click", () => {cards.delete(packet.job_id);item.remove();poll();});
    item.append(approve, changes, refresh, status);update();
    return {item, digest:packet.digest, update, markStale() {
      stale = true;refresh.hidden = false;status.textContent = "Preview changed. Load and test the new version before deciding.";update();
    }};
  }
  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const response = await fetch("/api/reviews", {cache:"no-store", signal:AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error("Review state unavailable.");
      const data = await response.json();token = data.token;api.enabled = data.enabled === true;
      document.getElementById("dashboard-access").textContent = api.enabled ? "Local operator controls" : "Read only / Local";
      document.getElementById("review-status").textContent = data.message || (api.enabled ?
        "Decisions are saved against the exact version shown. No action starts AI sessions." :
        "Read-only view. Start this server with --operator-controls to enable decisions and profile changes.");
      const incoming = new Set(data.jobs.map(packet => packet.job_id));
      for (const [id, existing] of cards) if (!incoming.has(id)) {existing.item.remove();cards.delete(id);}
      for (const packet of data.jobs) {
        const existing = cards.get(packet.job_id);
        if (existing) {if (existing.digest !== packet.digest) existing.markStale();else existing.update();}
        else {const created = card(packet);cards.set(packet.job_id, created);root.append(created.item);}
      }
      document.dispatchEvent(new Event("agentkit-controls"));
    } catch (error) {
      api.enabled = false;
      for (const existing of cards.values()) existing.update();
      document.getElementById("review-status").textContent = error.message;
    } finally {polling = false;}
  }
  poll();setInterval(poll, 5000);
})();
