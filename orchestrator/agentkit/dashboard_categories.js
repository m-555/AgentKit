"use strict";
(() => {
  const result = (bucket, label, color, reason, historyKind = "") => ({bucket,label,color,reason,historyKind});
  function waiting(task, view) {
    const reasons = [];
    if (view.execution_paused) reasons.push("Project execution is paused; no launch is scheduled.");
    if (task.job_state && task.job_state !== "ACTIVE") reasons.push("Job is " + task.job_state + "; its plan is not active.");
    const pending = (task.dependencies || []).filter(item => item.status !== "DONE");
    if (pending.length) reasons.push("Prerequisites: " + pending.map(item => item.id + " (" + item.status + ")").join(", ") + ".");
    if (task.progress === "WAITING_QUOTA") reasons.push("Provider allowance is blocked; the reset time permits a check, not a guaranteed restart.");
    if (task.blocker) reasons.push(task.blocker);
    if (task.next_action) reasons.push("Next action: " + task.next_action);
    if (!reasons.length) reasons.push(task.status === "REVIEW" ? "Commit awaits independent review." :
      task.status === "INTEGRATION_READY" ? "Reviewed commit awaits integration checks." :
      task.status === "READY" ? "Ready for scheduler checks of provider, isolation, file leases and available worker slots." :
      "No session owns this task; recorded state is " + (task.status || "unknown") + ".");
    return reasons.join(" ");
  }
  function classify(row, task, manager, view = {}) {
    if (manager) {
      if (row.state === "ACTIVE") return result("working","REGISTERED","running","Live external-manager registration; token activity may be unavailable.");
      const stale = row.state === "STALE";
      return result("history",row.state || "UNKNOWN","alert",stale ?
        "Heartbeat expired. This is not proof of task completion; recovery inspection is required." :
        row.state === "CRASHED" ? "Registered manager process stopped." : "External-manager registration is no longer active.",
        stale ? "stale" : row.state === "CRASHED" ? "crashed" : "done");
    }
    if (row.purpose) {
      if (row.ownership_uncertain || row.liveness_risk || ["ORPHANED","TERMINAL_PID_ALIVE","EXIT_UNCONFIRMED"].includes(row.state))
        return result("attention","OWNERSHIP CHECK","alert","Process ownership is uncertain. This record cannot safely be treated as stopped or reassigned.");
      const live = row.child_alive === true || row.monitor_alive === true;
      if (live && ["RUNNING","STARTING"].includes(row.state))
        return result("working",row.state === "STARTING" ? "STARTING" : row.purpose === "review" || row.purpose === "reviewer" ? "REVIEWING" : "RUNNING","running","A live owned process is recorded.");
      if (live) return result("attention","LIVE PROCESS","alert","A process remains alive outside a running state; inspect ownership before continuation.");
      if (row.continuation) return result("history","TRANSFERRED","", "Continuation handed to " + row.continuation.target_provider + " / " + row.continuation.target_model +
        " at generation " + row.continuation.generation + ". This old session is stopped; the handoff alone does not prove its replacement is running.", "transferred");
      if (row.state === "CRASHED" || row.status === "FAILED") return result("history",row.state === "CRASHED" ? "CRASHED" : "FAILED","alert","Session failed or crashed. The task may still need repair or continuation.","crashed");
      if (task?.status === "CANCELLED" || row.status === "CANCELLED") return result("history","CANCELLED","","Task or session was cancelled.","cancelled");
      if (["STALE","STOPPED","FINISHED"].includes(row.status) || row.ended_at || ["STOPPED","STALE"].includes(row.state))
        return result("history",task?.status === "DONE" ? "DONE" : row.status === "STALE" ? "STALE" : "SESSION FINISHED","",
          task?.status === "DONE" ? "Task is recorded DONE. Final feature acceptance is a separate check." : "Session ended; task state is " + (task?.status || "unknown") + ".",row.status === "STALE" ? "stale" : "done");
      return result("attention","LIVENESS UNKNOWN","alert","No reliable live or terminal evidence is available.");
    }
    if (["DONE","CANCELLED"].includes(row.status)) return result("history",row.status,"","Recorded task state is " + row.status + ".",row.status === "DONE" ? "done" : "cancelled");
    if (["FAILED","STALE","NEEDS_REPLAN","RUNNING","INTEGRATING","VERIFYING"].includes(row.status))
      return result("attention",row.status,"alert",row.blocker || "No live owner is recorded for this state; planner inspection is required.");
    return result("waiting",row.progress === "WAITING_QUOTA" ? "WAITING FOR QUOTA" : row.status === "REVIEW" ? "AWAITING REVIEW" :
      row.status === "INTEGRATION_READY" ? "AWAITING MERGE" : "WAITING","waiting",waiting(row,view));
  }
  function group(view) {
    const groups = {working:[],waiting:[],attention:[],history:[]};
    const tasks = view.tasks || [];
    const byId = new Map(tasks.map(task => [task.id,task]));
    const occupied = new Set();
    const recorded = new Set();
    for (const row of view.external_managers || []) {
      const state = classify(row,null,true,view);
      groups[state.bucket].push({row,task:null,manager:true,state});
    }
    for (const row of view.processes || []) {
      const task = byId.get(row.task_id);
      const state = classify(row,task,false,view);
      groups[state.bucket].push({row,task,state});
      recorded.add(row.task_id);
      if (state.bucket !== "history") occupied.add(row.task_id);
    }
    for (const task of tasks) {
      if (occupied.has(task.id)) continue;
      const state = classify(task,task,false,view);
      if (state.bucket === "history" && recorded.has(task.id)) continue;
      groups[state.bucket].push({row:task,task,taskOnly:true,state});
    }
    return groups;
  }
  (typeof window === "undefined" ? globalThis : window).AgentKitCategories = {classify,group,waiting};
})();
