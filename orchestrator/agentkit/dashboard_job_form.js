"use strict";
// Write a job: your own words and screenshots, for a new job or as an update to one.
(() => {
  const MAX_IMAGES = 10;
  const MAX_BYTES = 8 * 1024 * 1024;
  const TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
  // Lines of the acceptance box, without blanks; shared with tests.
  const lines = text => text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  window.AgentKitJobForm = {lines, MAX_IMAGES, MAX_BYTES, TYPES};
  const form = typeof document === "undefined" ? null : document.getElementById("job-form");
  if (!form) return;
  const target = document.getElementById("job-target");
  const text = document.getElementById("job-text");
  const acceptance = document.getElementById("job-acceptance");
  const acceptanceLabel = document.getElementById("job-acceptance-label");
  const picker = document.getElementById("job-images");
  const thumbs = document.getElementById("job-thumbs");
  const send = document.getElementById("job-send");
  const status = document.getElementById("jobs-status");
  const images = [];  // {name, url, data}

  const say = message => {status.textContent = message;};
  const read = file => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Could not read " + file.name));
    reader.readAsDataURL(file);
  });

  async function add(files) {
    for (const file of files) {
      if (!TYPES.includes(file.type)) {say(file.name + " is not a PNG, JPEG, GIF or WebP image.");continue;}
      if (file.size > MAX_BYTES) {say(file.name + " is over 8 MB.");continue;}
      if (images.length >= MAX_IMAGES) {say("Up to " + MAX_IMAGES + " screenshots per job or update.");break;}
      const url = await read(file);
      images.push({name: file.name || "pasted image", url, data: url.split(",", 2)[1]});
    }
    show();
  }

  function show() {
    thumbs.replaceChildren(...images.map((image, index) => {
      const figure = document.createElement("figure");
      const img = document.createElement("img");
      img.src = image.url;img.alt = image.name;
      const remove = document.createElement("button");
      remove.type = "button";remove.textContent = "Remove";
      remove.addEventListener("click", () => {images.splice(index, 1);show();});
      figure.append(img, remove);
      return figure;
    }));
  }

  function mode() {
    const update = target.value !== "";
    acceptanceLabel.hidden = update;
    send.textContent = update ? "Send update" : "Save job";
  }

  async function jobs() {
    try {
      const data = await (await fetch("/api/jobs", {cache: "no-store"})).json();
      const chosen = target.value;
      target.replaceChildren(new Option("A new job", ""),
        ...data.jobs.map(job => new Option("Update " + job.id + " (" + job.status.toLowerCase() + ")", job.id)));
      if ([...target.options].some(option => option.value === chosen)) target.value = chosen;
      mode();
    } catch (error) {/* the jobs list shows its own error */}
  }

  picker.addEventListener("change", async () => {await add([...picker.files]);picker.value = "";});
  text.addEventListener("paste", event => {
    const files = [...(event.clipboardData?.files || [])].filter(file => TYPES.includes(file.type));
    if (files.length) {event.preventDefault();add(files);}
  });
  target.addEventListener("change", mode);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!text.value.trim()) return say("Write the job in your own words first.");
    if (window.AgentKitOperator?.enabled !== true) return say("This view is read-only. Run `agentkit view` to write jobs.");
    send.disabled = true;
    const shots = images.map(image => ({data: image.data}));
    const body = target.value ? {job_id: target.value, text: text.value, images: shots} :
      {text: text.value, acceptance: lines(acceptance.value), images: shots};
    try {
      const result = await window.AgentKitOperator.post(target.value ? "/api/job-update" : "/api/job", body);
      text.value = "";acceptance.value = "";images.length = 0;show();
      say(result.message);
      await jobs();
    } catch (error) {say(error.message);}
    send.disabled = false;
  });
  mode();
  jobs();
  setInterval(() => {if (!form.contains(document.activeElement)) jobs();}, 15000);
})();
