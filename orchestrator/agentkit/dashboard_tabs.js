"use strict";
// Tabs at the top: one part of the view at a time instead of one long page to scroll.
(() => {
  const tabs = [...document.querySelectorAll('.main-tabs [role="tab"]')];
  if (!tabs.length) return;
  const KEY = "agentkit-view-tab";

  function show(tab, focus) {
    for (const item of tabs) {
      const on = item === tab;
      item.setAttribute("aria-selected", String(on));
      item.tabIndex = on ? 0 : -1;
      const panel = document.getElementById(item.getAttribute("aria-controls"));
      if (panel) panel.hidden = !on;
    }
    try { localStorage.setItem(KEY, tab.id); } catch (error) { /* private window: no memory */ }
    if (focus) tab.focus();
    window.dispatchEvent(new CustomEvent("agentkit-tab", {detail: tab.id}));
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => show(tab));
    tab.addEventListener("keydown", event => {
      const moves = {ArrowRight: 1, ArrowLeft: tabs.length - 1};
      if (event.key in moves) {
        event.preventDefault();
        show(tabs[(index + moves[event.key]) % tabs.length], true);
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        show(event.key === "Home" ? tabs[0] : tabs[tabs.length - 1], true);
      }
    });
  });

  let saved = null;
  try { saved = localStorage.getItem(KEY); } catch (error) { saved = null; }
  show(tabs.find(tab => tab.id === saved) || tabs[0]);

  // The supervisor restarts the view when it stops; a restarted view may carry new code,
  // so the page reloads itself once it answers again (the open tab is remembered).
  let started = null;
  setInterval(async () => {
    try {
      const response = await fetch("/api/view", {cache: "no-store"});
      if (!response.ok) return;
      const view = await response.json();
      if (started === null) started = view.started_at;
      else if (view.started_at !== started) location.reload();
    } catch (error) { /* the view is restarting; keep trying */ }
  }, 10000);
})();
