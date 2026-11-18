/* Video Gateway — vanilla single-page studio.
   No build step, no framework, no third-party requests. */

(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const KEY_SESSION = "vg_api_key_session";
  const KEY_LOCAL = "vg_api_key_local";
  const LAST_JOB = "vg_last_job";

  const state = {
    key: null,
    models: [],
    selectedModel: "fasth3-v2",
    referenceFile: null,
    lastReferenceFile: null,
    promptEnhancementAvailable: false,
    regenerateHdAvailable: false,
    regenerateHdReason: "",
    pendingEnhancedPrompt: null,
    currentJob: null,
    pollTimer: null,
    tickTimer: null,
    pollBusy: false,
    currentBatchId: null,
    batchPollTimer: null,
    usage: null,
  };

  // ------------------------------------------------------------------ helpers
  function fmtUSD(value, digits = 2) {
    const n = Number(value || 0);
    if (!Number.isFinite(n)) return "$—";
    if (n === 0) return "$0.00";
    if (n < 0.01) return "$" + n.toFixed(4);
    return "$" + n.toFixed(digits);
  }

  function fmtBytes(bytes) {
    const n = Number(bytes || 0);
    if (n < 1e6) return (n / 1e3).toFixed(0) + " KB";
    if (n < 1e9) return (n / 1e6).toFixed(1) + " MB";
    return (n / 1e9).toFixed(2) + " GB";
  }

  function fmtDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function elapsed(startedAt) {
    if (!startedAt) return 0;
    const start = new Date(startedAt).getTime();
    if (Number.isNaN(start)) return 0;
    return Math.max(0, Math.floor((Date.now() - start) / 1000));
  }

  function fmtDuration(seconds) {
    const s = Math.max(0, Math.floor(seconds || 0));
    if (s < 60) return s + "s";
    return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
    })[ch]);
  }

  function humanPrompt(value) {
    const raw = String(value || "").trim();
    if (!raw) return "Untitled";
    const summary = raw.match(/(?:^|\n)summary:\s*([\s\S]*?)(?=\n\s*(?:retention_analysis:|detailed_description:|integrated_multimodal_description:|overall_soundscape:|non_diegetic_music:)|$)/i);
    const body = summary ? summary[1] : raw.replace(/^integrated_multimodal_description:\s*/i, "");
    return body.replace(/\s+/g, " ").trim();
  }

  function statusClass(status) {
    return "bi-" + String(status || "").replace(/[^a-z_]/g, "");
  }

  // The API returns absolute media URLs when PUBLIC_BASE_URL is configured
  // (useful for programmatic clients). In the browser, prefer the current
  // origin so an SSH-tunnel session at localhost does not get redirected
  // through Cloudflare Access for every poster/MP4.
  function mediaUrl(url) {
    if (!url) return url;
    try {
      const parsed = new URL(url, window.location.href);
      if (parsed.origin !== window.location.origin) {
        return window.location.origin + parsed.pathname + parsed.search;
      }
      return parsed.href;
    } catch {
      return url;
    }
  }

  function toast(message, kind = "") {
    const node = document.createElement("div");
    node.className = "toast " + kind;
    node.textContent = message;
    $("#toasts").appendChild(node);
    setTimeout(() => node.remove(), kind === "error" ? 7000 : 4200);
  }

  async function api(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    if (state.key) headers["Authorization"] = "Bearer " + state.key;
    if (options.body && !(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, Object.assign({}, options, { headers }));
    const contentType = response.headers.get("content-type") || "";
    if (response.redirected || (contentType.includes("text/html") && !path.endsWith("/"))) {
      throw new Error("Cloudflare Access session expired or the request was redirected to a login page. Reload the page and sign in again.");
    }
    let body = null;
    if (contentType.includes("application/json")) {
      body = await response.json().catch(() => null);
    } else {
      body = await response.text().catch(() => null);
    }
    if (!response.ok) {
      if (response.status === 401 && !path.includes("login")) {
        lockStudio();
      }
      const message = body && body.error ? body.error.message : (body && body.detail ? String(body.detail) : "Request failed (" + response.status + ")");
      const error = new Error(message);
      error.status = response.status;
      error.code = body && body.error ? body.error.code : null;
      throw error;
    }
    return body;
  }

  // ------------------------------------------------------------------ auth
  function storedKey() {
    return sessionStorage.getItem(KEY_SESSION) || localStorage.getItem(KEY_LOCAL) || "";
  }

  function lockStudio() {
    state.key = null;
    sessionStorage.removeItem(KEY_SESSION);
    localStorage.removeItem(KEY_LOCAL);
    $("#app").hidden = true;
    $("#login").hidden = false;
    $("#login-key").focus();
    stopPolling();
  }

  async function boot() {
    const key = storedKey();
    if (!key) {
      $("#login").hidden = false;
      return;
    }
    state.key = key;
    try {
      await refreshModels();
      $("#login").hidden = true;
      $("#app").hidden = false;
      await Promise.all([refreshUsage(), refreshLibrary()]);
      restoreLastJob();
      renderModelCards();
      populateSelects();
    } catch (error) {
      if (error.status === 401) {
        lockStudio();
      } else {
        toast("Could not connect: " + error.message, "error");
        $("#login").hidden = false;
      }
    }
  }

  async function login(event) {
    event.preventDefault();
    const key = $("#login-key").value.trim();
    if (!key) return;
    state.key = key;
    try {
      await refreshModels();
      if ($("#login-remember").checked) localStorage.setItem(KEY_LOCAL, key);
      sessionStorage.setItem(KEY_SESSION, key);
      $("#login").hidden = true;
      $("#app").hidden = false;
      await Promise.all([refreshUsage(), refreshLibrary()]);
      renderModelCards();
      populateSelects();
      restoreLastJob();
    } catch (error) {
      state.key = null;
      toast(error.status === 401 ? "That key was rejected." : "Connection failed: " + error.message, "error");
    }
  }

  // ------------------------------------------------------------------ models
  async function refreshModels() {
    const data = await api("/v1/models");
    state.models = data.data || [];
    state.promptEnhancementAvailable = Boolean(data.prompt_enhancement_available);
    state.regenerateHdAvailable = Boolean(data.regenerate_hd_available);
    state.regenerateHdReason = data.regenerate_hd_reason || "";
    const enhance = $("#enhance-prompt");
    if (enhance) {
      enhance.disabled = !state.promptEnhancementAvailable;
      if (!state.promptEnhancementAvailable) enhance.checked = false;
    }
    const preview = $("#preview-prompt");
    if (preview) preview.disabled = !state.promptEnhancementAvailable;
    if (!state.models.find((m) => m.id === state.selectedModel)) {
      state.selectedModel = state.models[0] ? state.models[0].id : "fasth3-v2";
    }
  }

  function modelById(id) {
    return state.models.find((m) => m.id === id) || state.models[0] || null;
  }

  function renderModelCards() {
    const grid = $("#model-grid");
    grid.innerHTML = "";
    for (const model of state.models) {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "model-card" + (model.id === state.selectedModel ? " selected" : "");
      card.dataset.model = model.id;
      const badge = model.quality === "quality" ? "mc-quality" : model.quality === "reference" ? "mc-ref" : "mc-fast";
      card.innerHTML = `
        <div class="mc-top">
          <strong>${escapeHtml(model.label)}</strong>
          <span class="mc-badge ${badge}">${escapeHtml(model.quality)}</span>
        </div>
        <p>${escapeHtml(model.description)}</p>
        <div class="mc-meta">
          <span>${model.native_audio ? "native audio" : "silent"}</span>
          <span>${(model.durations || [model.seconds]).join("/")}s · ${model.fps}fps</span>
          <span>~${fmtUSD(model.estimated_cost_usd.cold)} cold</span>
        </div>`;
      card.addEventListener("click", () => {
        state.selectedModel = model.id;
        renderModelCards();
        populateSizes($("#size"), model);
        populateDurations($("#duration"), model);
        updateEstimate();
        $("#reference-hint").textContent = model.supports_references
          ? "JPG, PNG, WEBP · up to 12 MB · ordered reference; add more via the API"
          : "JPG, PNG, WEBP · up to 12 MB · becomes the first frame";
        $("#last-frame-field").hidden = !(model.supports_last_frame && !model.supports_references);
      });
      grid.appendChild(card);
    }
    const selectedSpec = modelById(state.selectedModel);
    const selectedQuality = selectedSpec?.quality;
    $("#studio-mode").textContent = selectedQuality === "quality" ? "Quality"
      : selectedQuality === "reference" ? "Reference" : "Fast";
    $("#reference-hint").textContent = selectedSpec?.supports_references
      ? "JPG, PNG, WEBP · up to 12 MB · ordered reference; add more via the API"
      : "JPG, PNG, WEBP · up to 12 MB · becomes the first frame";
    if (selectedSpec) {
      $("#last-frame-field").hidden = !(selectedSpec.supports_last_frame && !selectedSpec.supports_references);
    }
  }

  function populateDurations(select, model) {
    if (!select || !model) return;
    const previous = Number(select.value);
    select.innerHTML = "";
    for (const seconds of model.durations || [model.seconds]) {
      const option = document.createElement("option");
      option.value = String(seconds);
      option.textContent = `${seconds} seconds`;
      select.appendChild(option);
    }
    select.value = (model.durations || []).includes(previous)
      ? String(previous)
      : String(model.default_duration || model.seconds || 5);
  }

  function populateSizes(select, model) {
    if (!select || !model) return;
    const previous = select.value;
    select.innerHTML = "";
    if (model.adaptive_aspect_ratio && select.id === "size") {
      const auto = document.createElement("option");
      auto.value = "auto";
      auto.textContent = "Auto from image · 768p";
      select.appendChild(auto);
    }
    for (const resolution of model.resolutions || []) {
      const option = document.createElement("option");
      option.value = resolution.size;
      option.textContent = select.id === "batch-size"
        ? `${resolution.size} · ${resolution.aspect_ratio}`
        : `${resolution.label} · ${resolution.size} · ${resolution.aspect_ratio}`;
      select.appendChild(option);
    }
    const match = Array.from(select.options).find((option) => option.value === previous);
    const fallback = model.adaptive_aspect_ratio && select.id === "size"
      ? "auto"
      : (model.default_size
        || (model.resolutions && model.resolutions[0] ? model.resolutions[0].size : ""));
    select.value = match ? previous : fallback;
  }

  function populateSelects() {
    const model = modelById(state.selectedModel);
    populateSizes($("#size"), model);
    populateDurations($("#duration"), model);
    const batchModel = $("#batch-model");
    batchModel.innerHTML = "";
    for (const item of state.models) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.label;
      batchModel.appendChild(option);
    }
    batchModel.value = state.selectedModel;
    populateSizes($("#batch-size"), modelById(batchModel.value));
    updateEstimate();
    updateBatchEstimate();
  }

  function updateEstimate() {
    const model = modelById(state.selectedModel);
    if (!model) return;
    const seconds = Number($("#duration")?.value || model.default_duration || model.seconds || 5);
    const scale = Math.max(1, seconds / (model.default_duration || model.seconds || 5));
    $("#estimate").textContent = "≤ " + fmtUSD(model.estimated_cost_usd.cold * scale);
    $("#estimate-note").textContent = `Warm runs typically ~${fmtUSD(model.estimated_cost_usd.warm * scale)}. The cold estimate is reserved from the monthly budget.`;
  }

  function updateBatchEstimate() {
    const count = $("#batch-prompts").value.split("\n").map((line) => line.trim()).filter(Boolean).length;
    const model = modelById($("#batch-model").value);
    if (!model || !count) {
      $("#batch-estimate").textContent = "—";
      return;
    }
    const seconds = Number($("#batch-duration")?.value || model.default_duration || 10);
    const scale = Math.max(1, seconds / (model.default_duration || model.seconds || 10));
    const cold = model.estimated_cost_usd.cold * scale;
    $("#batch-estimate").textContent = `${count} × ${fmtUSD(cold)} ≈ ${fmtUSD(count * cold)}`;
  }

  // ------------------------------------------------------------------ reference
  function setReference(file) {
    if (!file) return;
    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
      toast("Use a JPG, PNG, or WEBP image.", "error");
      return;
    }
    if (file.size > 12 * 1024 * 1024) {
      toast("Reference image is larger than 12 MB.", "error");
      return;
    }
    state.referenceFile = file;
    const preview = $("#reference-preview");
    preview.src = URL.createObjectURL(file);
    preview.hidden = false;
    $("#clear-reference").hidden = false;
    $(".drop-empty", $("#dropzone")).hidden = true;
  }

  function clearReference(event) {
    if (event) event.stopPropagation();
    state.referenceFile = null;
    $("#reference").value = "";
    $("#reference-preview").hidden = true;
    $("#reference-preview").src = "";
    $("#clear-reference").hidden = true;
    $(".drop-empty", $("#dropzone")).hidden = false;
  }

  function setLastReference(file) {
    if (!file) return;
    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
      toast("Use a JPG, PNG, or WEBP image.", "error");
      return;
    }
    if (file.size > 12 * 1024 * 1024) {
      toast("Last-frame image is larger than 12 MB.", "error");
      return;
    }
    state.lastReferenceFile = file;
    $("#last-reference-name").textContent = file.name;
    $("#clear-last-reference").hidden = false;
    const preview = $("#last-reference-preview");
    preview.src = URL.createObjectURL(file);
    preview.hidden = false;
  }

  function clearLastReference(event) {
    if (event) event.stopPropagation();
    state.lastReferenceFile = null;
    $("#last-reference").value = "";
    $("#last-reference-name").textContent = "No file selected";
    $("#clear-last-reference").hidden = true;
    $("#last-reference-preview").hidden = true;
    $("#last-reference-preview").src = "";
  }

  function buildCreateRequest(prompt, { enhance = true } = {}) {
    const model = modelById(state.selectedModel);
    const seedValue = $("#seed").value.trim();
    const seconds = Number($("#duration").value || model.default_duration || 10);
    if (state.referenceFile || state.lastReferenceFile) {
      const form = new FormData();
      form.append("model", state.selectedModel);
      form.append("prompt", prompt);
      form.append("size", $("#size").value);
      form.append("seconds", String(seconds));
      form.append("enhance_prompt", enhance ? "true" : "false");
      if (seedValue) form.append("seed", seedValue);
      if (state.referenceFile) {
        form.append(model.supports_references ? "reference_images" : "input_reference",
          state.referenceFile, state.referenceFile.name || "reference.jpg");
      }
      if (state.lastReferenceFile && model.supports_last_frame) {
        form.append("last_reference", state.lastReferenceFile,
          state.lastReferenceFile.name || "last.jpg");
      }
      return { body: form };
    }
    const body = {
      model: state.selectedModel,
      prompt,
      size: $("#size").value,
      seconds,
      enhance_prompt: enhance,
    };
    if (seedValue) body.seed = Number(seedValue);
    return { body: JSON.stringify(body) };
  }

  async function previewPrompt() {
    const prompt = $("#prompt").value.trim();
    if (!prompt) {
      toast("Write a prompt first.", "error");
      $("#prompt").focus();
      return;
    }
    if (!state.promptEnhancementAvailable) {
      toast("Prompt enhancement is not configured on this gateway.", "error");
      return;
    }
    const button = $("#preview-prompt");
    button.disabled = true;
    button.textContent = "Previewing…";
    try {
      const data = await api("/v1/prompts/enhance", {
        method: "POST",
        ...buildCreateRequest(prompt, { enhance: true }),
      });
      state.pendingEnhancedPrompt = data;
      $("#prompt-preview-mode").textContent = [
        (data.mode || "prompt").toUpperCase(),
        data.applied ? `enhanced with ${data.model || "model"}` : "not enhanced",
        data.images_sent ? `${data.images_sent} image(s) analyzed` : "text-only",
      ].join(" · ");
      $("#enhanced-prompt-text").value = data.enhanced || prompt;
      $("#original-prompt-text").textContent = prompt;
      $("#prompt-preview-error").textContent = data.error || "";
      $("#prompt-modal").hidden = false;
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
      button.textContent = "Preview";
    }
  }

  function useEnhancedPrompt(edit = false) {
    const data = state.pendingEnhancedPrompt;
    if (!data) return;
    $("#prompt").value = $("#enhanced-prompt-text").value;
    $("#prompt-count").textContent = `${$("#prompt").value.length} / 16000`;
    $("#enhance-prompt").checked = false;
    $("#prompt-modal").hidden = true;
    if (edit) {
      $("#prompt").focus();
      toast("Enhanced prompt loaded; edit it before generating.", "ok");
    } else {
      toast("Enhanced prompt applied. The job will use it exactly.", "ok");
    }
  }

  async function generate() {
    const prompt = $("#prompt").value.trim();
    if (!prompt) {
      toast("Write a prompt first.", "error");
      $("#prompt").focus();
      return;
    }
    const model = modelById(state.selectedModel);
    if (!model) return;
    if (model.available === false) {
      toast("The GPU worker is not available. Check /usage → Worker.", "error");
      return;
    }
    const button = $("#generate");
    button.disabled = true;
    $(".btn-label", button).textContent = "Submitting…";
    $(".btn-spinner", button).hidden = false;
    try {
      const payload = buildCreateRequest(prompt, { enhance: $("#enhance-prompt").checked });
      const job = await api("/v1/videos", { method: "POST", ...payload });
      beginJob(job);
      toast("Queued. The GPU wakes on demand.", "ok");
      await refreshUsage();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
      $(".btn-label", button).textContent = "Generate video";
      $(".btn-spinner", button).hidden = true;
    }
  }

  function beginJob(job) {
    state.currentJob = job;
    sessionStorage.setItem(LAST_JOB, job.id);
    renderStage();
    startPolling();
  }

  function restoreLastJob() {
    const id = sessionStorage.getItem(LAST_JOB);
    if (!id) return;
    api(`/v1/videos/${id}`).then((job) => {
      beginJob(job);
    }).catch(() => {
      sessionStorage.removeItem(LAST_JOB);
    });
  }

  function startPolling() {
    stopPolling();
    state.pollBusy = false;
    state.pollTimer = setInterval(pollJob, 1500);
    state.tickTimer = setInterval(updateElapsed, 1000);
    pollJob();
  }

  function stopPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    if (state.tickTimer) clearInterval(state.tickTimer);
    state.pollTimer = null;
    state.tickTimer = null;
  }

  async function pollJob() {
    if (!state.currentJob || state.pollBusy) return;
    state.pollBusy = true;
    const id = state.currentJob.id;
    try {
      const job = await api(`/v1/videos/${id}`);
      if (!state.currentJob || state.currentJob.id !== id) return;
      state.currentJob = job;
      renderStage();
      if (["completed", "failed", "cancelled"].includes(job.status)) {
        stopPolling();
        await refreshUsage();
        if (job.status === "completed") {
          toast("Render complete.", "ok");
          refreshLibrary();
        } else if (job.status === "failed") {
          toast("Render failed. See the stage panel.", "error");
        }
      }
    } catch (error) {
      if (error.status === 401) return;
      // Transient network errors should not kill a running render.
      console.warn("poll failed", error);
    } finally {
      state.pollBusy = false;
    }
  }

  function updateElapsed() {
    const node = $("#stage-elapsed");
    if (!node || !state.currentJob) return;
    if (state.currentJob.status === "in_progress") {
      node.textContent = fmtDuration(elapsed(state.currentJob.started_at || state.currentJob.created_at));
    } else if (state.currentJob.status === "queued") {
      node.textContent = "queued " + fmtDuration(elapsed(state.currentJob.created_at));
    }
  }

  const STAGE_LABELS = {
    queued: ["Queued", "Waiting for the single GPU worker."],
    submitting: ["Submitting", "Handing the job to Modal."],
    starting: ["Starting GPU", "Container boot / image setup."],
    loading: ["Loading model", "Reading weights from the Modal Volume."],
    generating: ["Generating", "Diffusion is running on the RTX PRO 6000."],
    encoding: ["Encoding", "Decoding video and audio."],
    finishing: ["Finishing", "Muxing audio and writing the result."],
    downloading: ["Downloading", "Transferring the clip to this server."],
    completed: ["Completed", "Ready to play."],
    failed: ["Failed", "The worker returned an error."],
    cancelling: ["Cancelling", "Stopping the GPU run."],
    cancelled: ["Cancelled", "The job was cancelled."],
  };

  function renderStage() {
    const job = state.currentJob;
    const empty = $("#stage-empty");
    const active = $("#stage-active");
    const result = $("#stage-result");
    const error = $("#stage-error");
    if (!job) {
      empty.hidden = false;
      active.hidden = true;
      result.hidden = true;
      error.hidden = true;
      $("#stage-badge").className = "pill pill-muted";
      $("#stage-badge").textContent = "Idle";
      $("#stage-title").textContent = "Stage";
      $("#stage-sub").textContent = "Your render appears here.";
      return;
    }

    const spec = modelById(job.model);
    const label = STAGE_LABELS[job.internal_status] || STAGE_LABELS[job.stage] || [job.stage || "Working", ""];
    $("#stage-title").textContent = job.status === "completed" ? "Take" : "Render";
    $("#stage-sub").textContent = humanPrompt(job.prompt_original || job.prompt || "");
    $("#stage-model").textContent = spec ? spec.label : job.model;
    $("#stage-size").textContent = `${job.size} · ${job.fps || 24}fps · ${job.seconds || 5}s`;
    $("#stage-elapsed").textContent = job.status === "queued"
      ? "queued " + fmtDuration(elapsed(job.created_at))
      : fmtDuration(elapsed(job.started_at || job.created_at));

    if (job.status === "completed") {
      empty.hidden = true;
      active.hidden = true;
      error.hidden = true;
      result.hidden = false;
      $("#stage-badge").className = "pill pill-mint";
      $("#stage-badge").textContent = "Completed";
      const player = $("#player");
      if (player.dataset.job !== job.id) {
        player.dataset.job = job.id;
        player.src = mediaUrl(job.content_url);
        player.poster = mediaUrl(job.thumbnail_url) || "";
        player.load();
      }
      const download = $("#download-link");
      download.href = mediaUrl(job.download_url || job.content_url);
      download.download = `video-gateway-${job.id}.mp4`;
      $("#result-prompt").textContent = humanPrompt(job.prompt_original || job.prompt || "");
      const stats = [
        ["Model", spec ? spec.label : job.model],
        ["Seed", job.seed ?? "—"],
        ["Cost est.", fmtUSD(job.cost_usd)],
        ["Cold start", job.usage && job.usage.cold_start ? "yes" : "warm reuse"],
        ["Model load", job.usage && job.usage.model_load_s != null ? job.usage.model_load_s.toFixed(1) + "s" : "—"],
        ["Inference", job.usage && job.usage.inference_s != null ? job.usage.inference_s.toFixed(1) + "s" : "—"],
        ["Peak VRAM", job.usage && job.usage.peak_vram_mb ? (job.usage.peak_vram_mb / 1024).toFixed(1) + " GB" : "—"],
        ["Created", fmtDate(job.created_at)],
      ];
      $("#result-stats").innerHTML = stats.map(([key, value]) =>
        `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
      return;
    }

    if (job.status === "failed" || job.status === "cancelled") {
      empty.hidden = true;
      active.hidden = true;
      result.hidden = true;
      error.hidden = false;
      $("#stage-badge").className = job.status === "failed" ? "pill pill-coral" : "pill pill-muted";
      $("#stage-badge").textContent = job.status === "failed" ? "Failed" : "Cancelled";
      $("#stage-error-text").textContent = job.error || "The job ended without a video.";
      return;
    }

    empty.hidden = true;
    result.hidden = true;
    error.hidden = true;
    active.hidden = false;
    $("#stage-badge").className = "pill pill-amber";
    $("#stage-badge").textContent = job.status === "queued" ? "Queued" : "Running";
    $("#stage-stage").textContent = label[0];
    let detail = label[1];
    if (job.status === "queued" && job.queue_position) detail = `Queue position ${job.queue_position}. ${detail}`;
    if (job.blocked_reason) detail = job.blocked_reason;
    if (job.warning) detail = job.warning;
    $("#stage-detail").textContent = detail;
    const pct = Math.max(0, Math.min(100, Number(job.progress || 0)));
    $("#stage-percent").textContent = pct + "%";
    $("#progress-bar").style.width = (job.status === "queued" ? Math.max(3, pct) : pct) + "%";
    $("#cancel-job").disabled = false;
  }

  async function cancelJob() {
    if (!state.currentJob) return;
    $("#cancel-job").disabled = true;
    try {
      state.currentJob = await api(`/v1/videos/${state.currentJob.id}/cancel`, { method: "POST" });
      renderStage();
      toast("Cancellation requested.", "ok");
      startPolling();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function retryJob() {
    if (!state.currentJob) return;
    try {
      const job = await api(`/v1/videos/${state.currentJob.id}/retry`, { method: "POST" });
      beginJob(job);
      toast("New variation queued.", "ok");
      await refreshUsage();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  // ------------------------------------------------------------------ library
  async function refreshLibrary() {
    const filter = $("#library-filter") ? $("#library-filter").value : "";
    try {
      const data = await api("/v1/videos?limit=60" + (filter ? "&status=" + encodeURIComponent(filter) : ""));
      renderLibrary(data.data || []);
    } catch (error) {
      if (error.status !== 401) console.warn("library", error);
    }
  }

  function renderLibrary(items) {
    const grid = $("#library-grid");
    grid.innerHTML = "";
    $("#library-empty").hidden = items.length > 0;
    for (const item of items) {
      const card = document.createElement("article");
      card.className = "lib-card";
      const statusLabel = item.status.replace("_", " ");
      const thumb = item.thumbnail_url
        ? `<img loading="lazy" src="${escapeHtml(mediaUrl(item.thumbnail_url))}" alt="">`
        : `<div class="placeholder">${escapeHtml(item.status === "completed" ? "no poster" : item.status)}</div>`;
      card.innerHTML = `
        <div class="lib-thumb">
          ${thumb}
          <span class="lib-status" style="color:${item.status === "completed" ? "var(--mint)" : item.status === "failed" || item.status === "cancelled" ? "var(--coral)" : "var(--amber)"}">${escapeHtml(statusLabel)}</span>
        </div>
        <div class="lib-body">
          <div class="lib-prompt">${escapeHtml(humanPrompt(item.prompt_original || item.prompt))}</div>
          <div class="lib-meta"><span>${escapeHtml(item.model)}</span><span>${escapeHtml(item.size)}</span><span>${fmtUSD(item.cost_usd)}</span></div>
          <div class="lib-actions">
            <button class="btn btn-ghost" data-action="open">Play</button>
            <button class="btn btn-ghost" data-action="download">Save</button>
            <button class="btn btn-ghost" data-action="delete">Delete</button>
          </div>
        </div>`;
      card.addEventListener("click", (event) => {
        const action = event.target.closest("[data-action]")?.dataset.action || "open";
        event.stopPropagation();
        if (action === "open") openVideo(item);
        if (action === "download") window.location.href = mediaUrl(item.download_url || item.content_url);
        if (action === "delete") deleteVideo(item);
      });
      grid.appendChild(card);
    }
  }

  function openVideo(item) {
    if (item.status !== "completed") {
      beginJob(item);
      switchView("studio");
      return;
    }
    $("#modal-player").src = mediaUrl(item.content_url);
    $("#modal-meta").innerHTML = `
      <strong>${escapeHtml(humanPrompt(item.prompt_original || item.prompt))}</strong><br>
      ${escapeHtml(item.model)} · ${escapeHtml(item.size)} · seed ${escapeHtml(item.seed ?? "—")} · ${fmtUSD(item.cost_usd)} estimated · ${fmtDate(item.created_at)}
      ${item.warning ? `<br><span style="color:var(--amber)">${escapeHtml(item.warning)}</span>` : ""}
      <div class="modal-actions">
        <button id="regenerate-hd" class="btn" type="button"
          ${state.regenerateHdAvailable ? "" : "disabled"}
          title="${escapeHtml(state.regenerateHdReason || "")}">
          Regenerate HD (2K)
        </button>
        ${state.regenerateHdAvailable ? "" : `<span class="muted">${escapeHtml(state.regenerateHdReason || "")}</span>`}
      </div>`;
    $("#video-modal").hidden = false;
    $("#modal-player").play().catch(() => {});
    const regen = $("#regenerate-hd");
    if (regen && !regen.disabled) {
      regen.addEventListener("click", () => regenerateHd(item));
    }
  }

  async function regenerateHd(item) {
    try {
      await api(`/v1/videos/${item.id}/regenerate`, { method: "POST" });
      toast("Regenerate HD queued.", "ok");
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function deleteVideo(item) {
    if (!confirm("Delete this video and its files?")) return;
    try {
      await api(`/v1/videos/${item.id}`, { method: "DELETE" });
      toast("Deleted.", "ok");
      refreshLibrary();
      refreshUsage();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  // ------------------------------------------------------------------ batch
  async function submitBatch() {
    const prompts = $("#batch-prompts").value.split("\n").map((line) => line.trim()).filter(Boolean);
    if (!prompts.length) {
      toast("Add at least one prompt.", "error");
      return;
    }
    const modelId = $("#batch-model").value;
    const model = modelById(modelId);
    if (!model) return;
    const size = $("#batch-size").value;
    const seconds = Number($("#batch-duration")?.value || 10);
    const baseSeed = $("#batch-seed").value.trim();
    const items = prompts.map((prompt, index) => {
      const item = { prompt, model: modelId, size, seconds };
      if (baseSeed) item.seed = Number(baseSeed) + index;
      return item;
    });
    const button = $("#batch-submit");
    button.disabled = true;
    button.textContent = "Submitting…";
    try {
      const body = await api("/v1/videos/batch", {
        method: "POST",
        body: JSON.stringify({ items, label: "Studio batch", model: modelId }),
      });
      state.currentBatchId = body.batch_id;
      renderBatch(body.data || [], body.batch_id);
      toast(`${items.length} jobs queued.`, "ok");
      pollBatch();
      refreshUsage();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
      button.textContent = "Queue batch";
    }
  }

  function renderBatch(items, batchId) {
    $("#batch-sub").textContent = batchId ? `Batch ${batchId.slice(-8)} · ${items.length} jobs` : "No batch in this session.";
    const list = $("#batch-list");
    list.innerHTML = "";
    for (const item of items) {
      const node = document.createElement("div");
      node.className = "batch-item";
      node.dataset.id = item.id;
      node.innerHTML = `
        <span class="bi-status ${statusClass(item.internal_status)}">${escapeHtml(item.status.replace("_", " "))}</span>
        <div class="bi-main">
          <div class="bi-prompt">${escapeHtml(humanPrompt(item.prompt_original || item.prompt))}</div>
          <div class="bi-sub">${escapeHtml(item.size)} · ${escapeHtml(item.model)} · ${fmtUSD(item.cost_usd)}</div>
        </div>
        <button class="btn btn-ghost btn-sm" type="button">${item.status === "completed" ? "Play" : "Open"}</button>`;
      node.querySelector("button").addEventListener("click", () => {
        beginJob(item);
        switchView("studio");
      });
      list.appendChild(node);
    }
  }

  function pollBatch() {
    if (state.batchPollTimer) clearInterval(state.batchPollTimer);
    if (!state.currentBatchId) return;
    state.batchPollTimer = setInterval(async () => {
      try {
        const batch = await api(`/v1/batches/${state.currentBatchId}`);
        renderBatch(batch.videos || [], batch.id);
        if (batch.completed >= batch.total) {
          clearInterval(state.batchPollTimer);
          state.batchPollTimer = null;
          refreshLibrary();
          refreshUsage();
        }
      } catch (error) {
        console.warn("batch poll", error);
      }
    }, 2000);
  }

  // ------------------------------------------------------------------ usage
  async function refreshUsage() {
    try {
      const usage = await api("/usage");
      state.usage = usage;
      renderUsage(usage);
    } catch (error) {
      if (error.status !== 401) console.warn("usage", error);
    }
  }

  function renderUsage(usage) {
    $("#budget-remaining").textContent = fmtUSD(usage.remaining_usd);
    const official = usage.billed_usd == null ? null : Number(usage.billed_usd);
    $("#budget-spent-label").textContent = official == null ? "Budget used" : "Budget used (guard)";
    $("#budget-spent").textContent = fmtUSD(usage.spent_usd);
    $("#billing-detail").textContent = official == null
      ? "No official Modal bill synced; the budget guard uses the conservative job ledger."
      : `Official Modal bill: ${fmtUSD(official)} · conservative ledger: ${fmtUSD(usage.estimated_spend_usd)}. The guard reserves against the larger value.`;
    const isAdmin = usage.is_admin !== false;
    const billingActions = $("#billing-sync")?.parentElement;
    if (billingActions) billingActions.hidden = !isAdmin;
    if (!isAdmin) $("#billing-reconcile").hidden = true;
    $("#budget-reserved").textContent = fmtUSD(usage.reserved_usd);
    $("#budget-cap").textContent = fmtUSD(usage.soft_budget_usd);
    $("#budget-projected").textContent = fmtUSD(usage.projected_month_usd);
    $("#budget-bar-fill").style.width = Math.min(100, usage.percent_used || 0) + "%";
    $("#billing-source").textContent = usage.billing_source === "modal" ? "Modal billing" : usage.billing_source === "manual" ? "manual invoice" : "estimated";
    $("#billing-source").className = "pill " + (usage.billing_source === "modal" ? "pill-mint" : usage.billing_source === "manual" ? "pill-amber" : "pill-muted");
    $("#cycle-label").textContent = `${fmtDate(usage.billing_cycle.start)} → ${fmtDate(usage.billing_cycle.end)}`;
    $("#stat-videos").textContent = usage.jobs_this_cycle;
    $("#stat-completed").textContent = `${usage.completed_this_cycle} completed · ${usage.jobs_today}/${usage.max_jobs_per_day} today`;
    $("#stat-avg").textContent = fmtUSD(usage.avg_cost_completed_usd);
    $("#stat-active").textContent = `${usage.active_jobs} / ${usage.queued_jobs}`;
    const storageGb = Number(usage.storage.gb || 0);
    $("#stat-storage").textContent = storageGb < 0.01 ? fmtBytes(usage.storage.bytes) : storageGb.toFixed(2) + " GB";
    $("#stat-storage-note").textContent = `${usage.storage.files} files · cap ${usage.storage.max_gb} GB`;
    $("#footer-cost").textContent = `${fmtUSD(usage.spent_usd)} est. this cycle`;
    $("#budget-pill").textContent = fmtUSD(usage.remaining_usd) + " left";
    $("#budget-pill").className = "pill " + (usage.remaining_usd < 3 ? "pill-coral" : usage.remaining_usd < 8 ? "pill-amber" : "pill-mint");

    const health = usage.backend || {};
    const banner = $("#setup-banner");
    if (health.demo) {
      banner.hidden = false;
      banner.className = "banner";
      $("#setup-banner-text").textContent = "The gateway is using the local synthetic-test backend. Add Modal credentials, run `modal run worker/modal_app.py --action prepare`, deploy the worker, then set VIDEO_BACKEND=modal.";
    } else if (!health.ready) {
      banner.hidden = false;
      banner.className = "banner error";
      $("#setup-banner-text").textContent = health.message || "The Modal worker is not reachable.";
    } else {
      banner.hidden = true;
    }
    const gpu = $("#gpu-pill");
    const ready = health.ready;
    gpu.className = "pill " + (health.demo ? "pill-amber" : ready ? "pill-mint" : "pill-coral");
    gpu.title = health.message || "";
    $("span:last-child", gpu).textContent = health.demo ? "Demo" : ready ? "GPU ready" : "Offline";

    const workerKv = $("#worker-kv");
    const workerRows = [
      ["Backend", health.backend || "—"],
      ["GPU", health.gpu || usage.worker?.gpu || "—"],
      ["App / function", `${health.app || "—"} / ${health.function || "—"}`],
      ["Ready", ready ? "yes" : "no"],
      ["Scaledown", `${usage.worker?.scaledown_seconds ?? "—"} s`],
      ["Active job", usage.dispatcher?.current_job || "none"],
      ["Billing source", usage.billing_source || "estimated"],
      ["Billing error", usage.billing_error || "none"],
    ];
    workerKv.innerHTML = workerRows.map(([key, value]) =>
      `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("");
    $("#worker-badge").textContent = health.demo ? "DEMO" : ready ? "READY" : "OFFLINE";
    $("#worker-badge").className = "pill " + (health.demo ? "pill-amber" : ready ? "pill-mint" : "pill-coral");

    const daily = usage.daily_spend || [];
    const byDay = new Map(daily.map((day) => [day.day, Number(day.total) || 0]));
    const chartDays = [];
    for (let i = 13; i >= 0; i--) {
      const day = new Date();
      day.setUTCDate(day.getUTCDate() - i);
      const key = day.toISOString().slice(0, 10);
      chartDays.push({ day: key, total: byDay.get(key) || 0 });
    }
    const max = Math.max(...chartDays.map((day) => day.total), 0.01);
    $("#spend-chart").innerHTML = chartDays.map((day) =>
      `<div class="spend-bar" style="height:${Math.max(2, (day.total / max) * 100)}%" data-label="${escapeHtml(day.day.slice(5))} · ${fmtUSD(day.total)}"></div>`).join("");

    const ledger = usage.ledger || [];
    $("#ledger").innerHTML = ledger.length
      ? ledger.map((row) => `<div class="ledger-row"><span>${escapeHtml(row.kind)} · ${row.entries} run${row.entries === 1 ? "" : "s"}</span><span>${fmtUSD(row.total)}</span></div>`).join("")
      : `<p class="muted">No billable jobs yet.</p>`;

    $("#api-example").textContent = [
      "# create a video",
      "curl -sS https://YOUR_HOST/v1/videos \\",
      "  -H \"Authorization: Bearer $VG_API_KEY\" \\",
      "  -H \"Content-Type: application/json\" \\",
      "  -d '{\"model\":\"fasth3-v2\",\"prompt\":\"a fox in snow\",\"size\":\"1344x768\",\"seconds\":10}'",
      "",
      "# poll, then download",
      "curl -sS https://YOUR_HOST/v1/videos/VIDEO_ID -H \"Authorization: Bearer $VG_API_KEY\"",
      "curl -L \"https://YOUR_HOST/v1/videos/VIDEO_ID/content?token=…\" -o clip.mp4",
    ].join("\n");
  }

  async function syncBilling() {
    try {
      const result = await api("/v1/admin/billing/sync", { method: "POST" });
      if (result.ok) {
        toast(`Synced Modal billing: ${fmtUSD(result.total_usd)} this cycle.`, "ok");
      } else {
        toast("Modal billing API unavailable; keeping conservative estimates.", "error");
      }
      refreshUsage();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  async function reconcileBilling(event) {
    event.preventDefault();
    const amount = Number($("#billing-amount").value);
    if (!Number.isFinite(amount) || amount < 0) return;
    try {
      await api("/v1/admin/billing/reconcile", {
        method: "POST",
        body: JSON.stringify({ billed_usd: amount, note: "manual invoice total" }),
      });
      toast("Invoice total saved. Budget guard uses it immediately.", "ok");
      $("#billing-reconcile").hidden = true;
      refreshUsage();
    } catch (error) {
      toast(error.message, "error");
    }
  }

  // ------------------------------------------------------------------ navigation
  function switchView(name) {
    $$(".view").forEach((view) => view.classList.toggle("active", view.id === "view-" + name));
    $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === name));
    if (name === "library") refreshLibrary();
    if (name === "usage") refreshUsage();
    if (name === "batch") {
      populateSelects();
      updateBatchEstimate();
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ------------------------------------------------------------------ bind
  function bindStatic() {
    $("#login-form").addEventListener("submit", login);
    $("#prompt").addEventListener("input", (event) => {
      $("#prompt-count").textContent = `${event.target.value.length} / 16000`;
    });
    $("#prompt").addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") generate();
    });
    $$("#prompt-chips .chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        $("#prompt").value = chip.dataset.prompt;
        $("#prompt").dispatchEvent(new Event("input"));
        $("#prompt").focus();
      });
    });
    $("#generate").addEventListener("click", generate);
    $("#preview-prompt").addEventListener("click", previewPrompt);
    $("#prompt-modal-close").addEventListener("click", () => { $("#prompt-modal").hidden = true; });
    $("#use-enhanced").addEventListener("click", () => useEnhancedPrompt(false));
    $("#edit-enhanced").addEventListener("click", () => useEnhancedPrompt(true));
    $("#keep-original").addEventListener("click", () => {
      $("#prompt-modal").hidden = true;
      $("#enhance-prompt").checked = false;
    });
    $("#cancel-job").addEventListener("click", cancelJob);
    $("#retry-job").addEventListener("click", retryJob);
    $("#retry-failed").addEventListener("click", retryJob);
    $("#copy-prompt").addEventListener("click", () => {
      const prompt = state.currentJob?.prompt || "";
      navigator.clipboard.writeText(prompt).then(() => toast("Prompt copied.", "ok")).catch(() => {});
    });

    const dropzone = $("#dropzone");
    dropzone.addEventListener("click", () => $("#reference").click());
    dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        $("#reference").click();
      }
    });
    $("#reference").addEventListener("change", (event) => setReference(event.target.files[0]));
    $("#clear-reference").addEventListener("click", clearReference);
    $("#last-reference")?.addEventListener("change", (event) => setLastReference(event.target.files[0]));
    $("#clear-last-reference")?.addEventListener("click", clearLastReference);
    ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add("drag");
    }));
    ["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove("drag");
    }));
    dropzone.addEventListener("drop", (event) => {
      const file = event.dataTransfer.files[0];
      if (file) setReference(file);
    });

    $$(".tab").forEach((tab) => tab.addEventListener("click", () => switchView(tab.dataset.view)));
    $("#batch-submit").addEventListener("click", submitBatch);
    $("#batch-prompts").addEventListener("input", updateBatchEstimate);
    $("#batch-model").addEventListener("change", () => {
      populateSizes($("#batch-size"), modelById($("#batch-model").value));
      updateBatchEstimate();
    });
    $("#batch-size").addEventListener("change", updateBatchEstimate);
    $("#batch-duration")?.addEventListener("change", updateBatchEstimate);
    $("#duration").addEventListener("change", updateEstimate);

    $("#library-refresh").addEventListener("click", refreshLibrary);
    $("#library-filter").addEventListener("change", refreshLibrary);

    $("#billing-sync").addEventListener("click", syncBilling);
    $("#billing-reconcile-toggle").addEventListener("click", () => {
      $("#billing-reconcile").hidden = !$("#billing-reconcile").hidden;
    });
    $("#billing-reconcile").addEventListener("submit", reconcileBilling);
    $("#copy-api").addEventListener("click", () => {
      navigator.clipboard.writeText($("#api-example").textContent)
        .then(() => toast("curl example copied.", "ok")).catch(() => {});
    });

    $("#logout").addEventListener("click", lockStudio);

    $("#modal-close").addEventListener("click", closeModal);
    $("#video-modal").addEventListener("click", (event) => {
      if (event.target === $("#video-modal")) closeModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeModal();
    });
  }

  function closeModal() {
    $("#video-modal").hidden = true;
    $("#modal-player").pause();
    $("#modal-player").removeAttribute("src");
    $("#modal-player").load();
  }

  // Re-render stage when the tab becomes visible again.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.currentJob) pollJob();
  });

  // Boot immediately; login form is outside the app shell.
  bindStatic();
  boot();
})();
