"use strict";
// Jobs: what waits for your reply, and the jobs of the repository chosen above.
(() => {
  // Replies, cheapest first. Only "Re-plan" starts a planner; nothing runs while a problem waits.
  const REPLIES = {
    retry: ["Retry", "Run the stopped step again as it is, after you fixed the tool, login or setup."],
    accept: ["Accept", "Take the builder's commit as it is; its tests still have to pass."],
    skip: ["Skip", "Cancel this task and every task that depends on it."],
    dismiss: ["Dismiss", "Close the request; the task keeps its files."],
    hint: ["Send hint", "The same builder continues with your words."],
    plan: ["Re-plan", "The assigner re-plans the job with your words."],
  };
  window.AgentKitJobs = {REPLIES};
  const list = typeof document === "undefined" ? null : document.getElementById("problem-list");
  if (!list) return;
  const jobsBox = document.getElementById("job-list");
  const status = document.getElementById("jobs-status");
  const el = (tag, text, cls) => {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = String(text);
    if (cls) item.className = cls;
    return item;
  };
  const say = text => {status.textContent = text;};
  const drafts = new Map();  // words typed into a problem survive the 5-second refresh

  function card(problem, operator) {
    const item = el("article", undefined, "problem-card");
    const waiting = problem.status === "WAITING";
    item.append(el("strong", problem.title), el("p", problem.detail),
      el("p", "Code " + problem.code + " · job " + problem.job_id +
        (waiting ? " · with the assigner; it comes back here if the new plan leaves it stopped" : ""), "muted"));
    if (waiting) return item;
    const needsWords = problem.options.some(option => option === "hint" || option === "plan");
    let words = null;
    if (needsWords) {
      words = el("textarea");
      words.rows = 2;words.maxLength = 2000;words.disabled = !operator;
      words.placeholder = problem.kind === "question" ? "Your answer for the assigner" : "Your words, for a hint or a re-plan";
      words.value = drafts.get(problem.id) || "";
      words.addEventListener("input", () => drafts.set(problem.id, words.value));
      item.append(words);
    }
    const row = el("div", undefined, "problem-actions");
    problem.options.forEach(option => {
      const [label, help] = REPLIES[option];
      const button = el("button", problem.kind === "question" && option === "plan" ? "Answer" : label);
      button.type = "button";button.title = help;button.disabled = !operator;
      button.addEventListener("click", async () => {
        const text = words ? words.value.trim() : "";
        if ((option === "hint" || option === "plan") && !text) return say("Write your words first.");
        button.disabled = true;
        try {
          const result = await window.AgentKitOperator.post("/api/problem", {problem: problem.id, action: option, text});
          drafts.delete(problem.id);
          say(result.message);
        } catch (error) {say(error.message);}
        await load();
      });
      row.append(button);
    });
    if (!problem.options.length) row.append(el("span", "Change this in the Settings tab.", "muted"));
    item.append(row);
    return item;
  }

  function job(row) {
    const item = el("article", undefined, "job-card");
    const counts = Object.entries(row.tasks).map(([state, n]) => n + " " + state.toLowerCase()).join(", ");
    item.append(el("strong", row.id + " · " + row.status + " · revision " + row.revision),
      el("p", row.request), el("p", (counts || "no tasks yet") + (row.updates ? " · " + row.updates + " update(s)" : "") +
        (row.screenshots ? " · " + row.screenshots + " screenshot(s)" : ""), "muted"));
    if (row.last_error) item.append(el("p", row.last_error, "muted"));
    return item;
  }

  async function load() {
    try {
      const response = await fetch("/api/jobs", {cache: "no-store"});
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || "Jobs unavailable.");
      const operator = window.AgentKitOperator?.enabled === true;
      const focused = document.activeElement && list.contains(document.activeElement);
      if (!focused) {
        list.replaceChildren(...(data.problems.length ? [el("h2", data.problems.length + " waiting for you"),
          ...data.problems.map(problem => card(problem, operator))] : [el("p", "Nothing waits for you.", "muted")]));
      }
      jobsBox.replaceChildren(el("h2", "Jobs"), ...(data.jobs.length ? data.jobs.map(job) : [el("p", "No jobs yet.", "muted")]));
      const readOnly = "This view is read-only. Run `agentkit view` to answer problems.";
      if (!operator) say(readOnly);
      else if (status.textContent === readOnly) say("");  // controls arrived after the first load
    } catch (error) {say(error.message);}
  }
  document.addEventListener("agentkit-controls", load);
  load();
  setInterval(load, 5000);
})();
