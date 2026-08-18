"use strict";

import { msToTime, timeToMs, constrainStart, constrainEnd } from "./time.js";

const $ = (id) => document.getElementById(id);

// TubeSnip is a long-video clipper: cap each trim at 5 minutes.
const MAX_TRIM_MS = 5 * 60 * 1000;

const els = {
  urlInput: $("url-input"),
  loadBtn: $("load-btn"),
  demoLink: $("demo-link"),
  error: $("error-msg"),
  playerSection: $("player-section"),
  loadingCard: $("loading-card"),
  player: $("player"),
  title: $("video-title"),
  playheadChip: $("current-playhead-time"),
  setStartBtn: $("btn-set-start"),
  setEndBtn: $("btn-set-end"),
  startInput: $("start-input"),
  endInput: $("end-input"),
  startSlider: $("start-slider"),
  endSlider: $("end-slider"),
  startLabel: $("slider-start-label"),
  endLabel: $("slider-end-label"),
  fill: $("slider-fill"),
  sliderRangeInfo: $("slider-range-info"),
  resStack: $("res-stack"),
  fmtStack: $("fmt-stack"),
  cutEstimate: $("cut-estimate"),
  modeOptions: $("mode-options"),
  cutBtn: $("cut-btn"),
  processingCard: $("processing-card"),
  processIcon: $("process-icon"),
  processTitle: $("process-title"),
  processSubtitle: $("process-subtitle"),
  processPercent: $("process-percent"),
  progressArea: $("progress-area"),
  progressFill: $("progress-fill"),
  progressText: $("progress-text"),
  progressStage: $("progress-stage"),
  downloadArea: $("download-area"),
  downloadLink: $("download-link"),
  downloadMeta: $("download-meta"),
  resResultBadge: $("res-result-badge"),
  durResultBadge: $("dur-result-badge"),
  sizeResultBadge: $("size-result-badge"),
  timeResultBadge: $("time-result-badge"),
  noAudioWarn: $("no-audio-warn"),
  jobDialog: $("job-dialog"),
  jobDialogText: $("job-dialog-text"),
  jobFollow: $("job-follow"),
  jobDiscard: $("job-discard"),
};

const state = { durationMs: 0, videoId: null, hasAudio: true, selectedRes: "best", selectedFmt: "mp4", estimateParams: null };

/* ---------------- util ---------------- */

function parseYouTubeId(url) {
  const m = url.match(
    /(?:youtu\.be\/|youtube\.com\/(?:watch\?.*v=|embed\/|shorts\/|live\/|v\/))([\w-]{6,})/
  );
  return m ? m[1] : null;
}

function showError(msg) {
  els.error.textContent = msg;
  els.error.classList.remove("hidden");
}

function hideError() {
  els.error.classList.add("hidden");
}

/**
 * Reset UI to its initial state (used on error): hide the processing card &
 * preview/parameter sections, destroy the player, clear all inputs, and
 * scroll to top. The error message is shown separately via showError.
 */
function resetToInitial() {
  clearTimeout(timeAdjustTimer);
  destroyPlyr();
  state.durationMs = 0;
  state.videoId = null;
  state.hasAudio = true;
  state.selectedRes = "best";
  state.selectedFmt = "mp4";
  state.estimateParams = null;

  els.title.textContent = "";
  els.title.title = "";
  els.urlInput.value = "";
  els.startInput.value = "00:00:00.000";
  els.endInput.value = "";
  els.startSlider.value = "0";
  els.endSlider.value = "0";
  els.startLabel.textContent = "00:00:00.000";
  els.endLabel.textContent = "00:00:00.000";
  els.sliderRangeInfo.textContent = "00:00:00.000 → 00:00:00.000";
  els.resStack.innerHTML = "";
  for (const b of els.fmtStack.querySelectorAll(".res-btn")) {
    b.classList.toggle("active", b.dataset.format === "mp4");
  }
  els.modeOptions.classList.add("hidden");
  els.playheadChip.textContent = "00:00:00.000";
  els.setStartBtn.disabled = true;
  els.setEndBtn.disabled = true;
  els.noAudioWarn.classList.add("hidden");

  els.playerSection.classList.add("hidden");
  els.loadingCard.classList.add("hidden");
  els.processingCard.classList.add("hidden");
  hideProgress();
  els.progressStage.textContent = "";
  els.downloadArea.classList.add("hidden");
  els.downloadLink.href = "#";
  const dlSpan = els.downloadLink.querySelector("span");
  if (dlSpan) dlSpan.textContent = "Download Trimmed Video";
  els.downloadMeta.textContent = "";
  els.resResultBadge.textContent = "–";
  els.durResultBadge.textContent = "–";
  els.sizeResultBadge.textContent = "–";
  els.timeResultBadge.textContent = "–";
  jobStartTime = null;
  pipelineFmt = "mp4";
  resetPipeline();

  els.cutBtn.disabled = true;
  els.cutBtn.textContent = "▶ Cut & Download";

  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ---------------- slider & time fields ---------------- */

function startMs() {
  return parseInt(els.startSlider.value, 10);
}

function endMs() {
  return parseInt(els.endSlider.value, 10);
}

function updateSliderConstraints() {
  // Both sliders use the full range [0, durationMs]. The start < end invariant
  // is kept by clamping values on sync — NOT via dynamic min/max. Changing
  // slider min/max mid-drag triggers a silent browser clamp: the other slider
  // "jumps" unnoticed and the text fields get out of sync.
  els.startSlider.min = 0;
  els.startSlider.max = state.durationMs;
  els.endSlider.min = 0;
  els.endSlider.max = state.durationMs;
}

function updateFill() {
  const max = state.durationMs || 1;
  const s = startMs();
  const e = endMs();
  els.fill.style.left = (s / max) * 100 + "%";
  els.fill.style.width = Math.max(0, ((e - s) / max) * 100) + "%";
}

function renderTime() {
  els.startInput.value = msToTime(startMs());
  els.endInput.value = msToTime(endMs());
  els.startLabel.textContent = msToTime(startMs());
  els.endLabel.textContent = msToTime(endMs());
  els.sliderRangeInfo.textContent = `${msToTime(startMs())} → ${msToTime(endMs())}`;
  updateFill();
}

/**
 * Sync both sliders + fields after one of them is moved.
 * `which` = the slider the user dragged ("start"/"end"). Only the dragged
 * one is clamped to the bounds (start < end); the other slider never moves
 * on its own.
 */
function syncFromSliders(which) {
  let s = startMs();
  let e = endMs();
  if (which === "start") {
    s = constrainStart(s, e, state.durationMs);
  } else if (which === "end") {
    e = constrainEnd(e, s, state.durationMs);
  }
  if (s >= e) s = Math.max(0, e - 1); // invariant safety
  els.startSlider.value = String(s);
  els.endSlider.value = String(e);
  renderTime();
  renderEstimate();
  if (which) onTimeAdjusted();
}

function syncFromStartInput() {
  const v = timeToMs(els.startInput.value);
  if (v === null) return;
  const clamped = constrainStart(v, endMs(), state.durationMs);
  els.startSlider.value = String(clamped);
  renderTime();
  renderEstimate();
  onTimeAdjusted();
}

function syncFromEndInput() {
  const v = timeToMs(els.endInput.value);
  if (v === null) return;
  const clamped = constrainEnd(v, startMs(), state.durationMs);
  els.endSlider.value = String(clamped);
  renderTime();
  renderEstimate();
  onTimeAdjusted();
}

/* ---------------- preview: position at start without autoplay (M5) ---------------- */

let timeAdjustTimer = null;

/**
 * Position the preview at the clip start WITHOUT playing. Plyr prevents
 * autoplay on seek before the first play (preventive mute + auto pause), but
 * we use cueVideoById via the YouTube embed (exposed by plyr) so the position
 * sticks with no playback flash at all: if playing → currentTime (jump, keep
 * playing); if not/cued → cueVideoById (position, stay still).
 */
function seekPreviewToStart(videoId) {
  const start = startMs() / 1000;
  if (!plyr) return;
  if (plyr.playing) {
    plyr.currentTime = start;
    return;
  }
  plyr.embed.cueVideoById({ videoId, startSeconds: start });
}

/** After start/end is adjusted, the preview is repositioned to start (no autoplay). */
function onTimeAdjusted() {
  if (!state.videoId) return;
  clearTimeout(timeAdjustTimer);
  timeAdjustTimer = setTimeout(() => seekPreviewToStart(state.videoId), 600); // debounce: don't spam while still dragging
}

/* ---------------- Plyr (YouTube) (M6) ---------------- */
// Plyr wraps the YouTube IFrame API with a uniform API (play/pause/currentTime)
// + custom controls. The playhead is read by polling `plyr.currentTime` (the
// proven old pattern) — no dependency on plyr events. Readiness detection:
// `plyr.embed` exists & `plyr.duration > 0` (duration set at YT onReady).

let plyr = null;
let currentVideoId = null;
let playerReady = false;
let playheadTimer = null;

/**
 * Player host element. Plyr REPLACES the target element on init (official
 * pattern), so #player may be gone — recreate it inside .video-frame if missing.
 */
function playerHost() {
  let host = document.getElementById("player");
  if (!host) {
    host = document.createElement("div");
    host.id = "player";
    const frame = document.querySelector(".video-frame");
    if (frame) frame.appendChild(host);
  }
  return host;
}

/** Create the Plyr YouTube player inside #player. */
function createPlyr(videoId) {
  if (!window.Plyr) return;
  currentVideoId = videoId;
  playerReady = false;
  const host = playerHost();
  host.innerHTML = "";
  host.setAttribute("data-plyr-provider", "youtube");
  host.setAttribute("data-plyr-embed-id", videoId);
  try {
    plyr = new window.Plyr(host, {
      youtube: { noCookie: true, rel: 0 },
      controls: [
        "play-large", "play", "progress", "current-time", "duration",
        "mute", "volume", "settings", "pip", "fullscreen",
      ],
      settings: ["quality", "speed"],
      autoplay: false,
    });
    startPlayheadPolling();
  } catch (e) {
    // Invalid video ID / embed failure → preview won't show, but don't fail
    // loadVideo: /api/info still decides video validity.
    plyr = null;
    console.warn("Plyr failed to create:", e);
  }
}

/** Destroy the player (if any) and empty its container. */
function destroyPlyr() {
  stopPlayheadPolling();
  if (plyr) {
    try {
      plyr.destroy();
    } catch {
      /* ignore — player may not be ready yet */
    }
    plyr = null;
  }
  // destroy() may early-return if the embed isn't ready — drop leftover plyr
  // containers so async youtube.ready doesn't read already-cleared attributes.
  const frame = document.querySelector(".video-frame");
  const container = frame && frame.querySelector(".plyr");
  if (container) container.remove();
  playerReady = false;
  currentVideoId = null;
  const host = playerHost();
  host.innerHTML = "";
  host.removeAttribute("data-plyr-provider");
  host.removeAttribute("data-plyr-embed-id");
}

function updatePlayhead() {
  if (!plyr) return;
  // Ready once: enable Set Start/End + position at the clip start (only if
  // start > 0 — a fresh player is already cued at 0). No play: the preview
  // does not auto-play.
  if (!playerReady && plyr.embed && plyr.duration > 0) {
    playerReady = true;
    els.setStartBtn.disabled = false;
    els.setEndBtn.disabled = false;
    if (startMs() > 0) seekPreviewToStart(currentVideoId);
  }
  const t = plyr.currentTime;
  if (isFinite(t) && t >= 0) {
    els.playheadChip.textContent = msToTime(Math.round(t * 1000));
    checkEndBoundary();
  }
}

function startPlayheadPolling() {
  stopPlayheadPolling();
  playheadTimer = setInterval(updatePlayhead, 250);
}

function stopPlayheadPolling() {
  if (playheadTimer) {
    clearInterval(playheadTimer);
    playheadTimer = null;
  }
}

/**
 * Preview boundary: while playing, when the playhead reaches the clip end →
 * stop, then reset the position to start so it can be played again. Only
 * applies while the player is PLAYING — if the user pauses/scrubs past end
 * manually, it isn't snapped.
 */
function checkEndBoundary() {
  if (!state.videoId || !plyr || !plyr.playing) return;
  const end = endMs();
  if (end <= 0) return;
  const t = plyr.currentTime;
  if (t * 1000 < end) return;
  // Stop & jump back to the clip start (ready to replay from the segment start).
  plyr.currentTime = startMs() / 1000;
  plyr.pause();
}

/** Set Start: take the current playhead position as the clip start. */
function setStartFromPlayhead() {
  if (!plyr) return;
  const t = plyr.currentTime;
  if (!(isFinite(t) && t >= 0)) return;
  els.startSlider.value = String(Math.round(t * 1000));
  syncFromSliders("start");
}

/** Set End: take the current playhead position as the clip end. */
function setEndFromPlayhead() {
  if (!plyr) return;
  const t = plyr.currentTime;
  if (!(isFinite(t) && t >= 0)) return;
  els.endSlider.value = String(Math.round(t * 1000));
  syncFromSliders("end");
}

/* ---------------- load video ---------------- */

function loadEmbed(videoId) {
  // Replace the old player (if any) then create a new Plyr for this video.
  destroyPlyr();
  clearTimeout(timeAdjustTimer);
  els.setStartBtn.disabled = true;
  els.setEndBtn.disabled = true;
  els.playheadChip.textContent = "00:00:00.000";
  createPlyr(videoId);
}

function applyInfo(data) {
  state.durationMs = data.duration_ms;
  state.videoId = data.video_id;
  state.hasAudio = data.has_audio;
  state.estimateParams = data.estimate_params || null;

  els.title.textContent = data.title || data.video_id;
  els.title.title = data.title || "";

  // Default: start 0, end = full duration.
  els.startSlider.min = 0;
  els.startSlider.max = state.durationMs;
  els.endSlider.min = 1;
  els.endSlider.max = state.durationMs;
  els.startSlider.value = 0;
  els.endSlider.value = state.durationMs;
  els.startInput.value = "00:00:00.000";
  els.endInput.value = msToTime(state.durationMs);
  updateSliderConstraints();
  syncFromSliders();

  // Resolution stack: "Best (auto)" + the resolutions available for this video.
  els.resStack.innerHTML = "";
  const addResBtn = (value, label, fps, codec, bitrate) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "res-btn";
    b.dataset.value = value;
    b.dataset.fps = String(fps ?? 30);
    b.dataset.codec = codec || "";
    b.dataset.bitrate = bitrate != null ? String(bitrate) : ""; // kbps
    const sub = codec ? `${fps}fps · ${codec}` : "";
    b.innerHTML =
      `<span class="res-btn-left"><span class="res-dot"></span><span>${label}</span></span>` +
      (sub ? `<span class="res-pixel">${sub}</span>` : "");
    b.addEventListener("click", () => {
      for (const x of els.resStack.querySelectorAll(".res-btn")) {
        x.classList.remove("active");
      }
      b.classList.add("active");
      state.selectedRes = value;
      renderEstimate();
    });
    els.resStack.appendChild(b);
  };
  addResBtn("best", "Best (auto)", null, null, null);
  for (const r of data.resolutions) {
    addResBtn(String(r.height), `${r.height}p`, r.fps, r.codec, r.bitrate);
  }
  els.resStack.querySelector('.res-btn[data-value="best"]').classList.add("active");
  state.selectedRes = "best";
  renderEstimate();

  if (data.has_audio) {
    els.noAudioWarn.classList.add("hidden");
  } else {
    els.noAudioWarn.classList.remove("hidden");
  }

  els.playerSection.classList.remove("hidden");
  els.modeOptions.classList.remove("hidden");
  els.cutBtn.disabled = false;
}

/* ---------------- pipeline steps & processing card ---------------- */

const PIPELINE_STEPS = [
  { id: "step-1", stage: "extract" },
  { id: "step-2", stage: "download" },
  { id: "step-3", stage: "encode" },
  { id: "step-4", stage: "convert" },
  { id: "step-5", stage: "verify" },
];
const ENCODE_STEP = 2; // step-3: re-encode — only happens in precise mode
const CONVERT_STEP = 3; // step-4: format conversion — only when output != mp4
const seenStages = new Set();
let pipelineMode = null; // current job mode: "fast" / "accurate" (null = unknown yet)
let pipelineFmt = "mp4"; // current job output format (drives convert-step visibility)
// Per-step elapsed timers: stage -> { start, running }. Ticks while a step is
// active; freezes to the real duration once it's done.
const stepTimers = {};
const stagePct = {}; // stage -> latest display percent
const stageEstimate = {}; // stage -> backend-estimated ms (from job.estimate_ms)
let stepTickId = null;
let jobStartTime = null; // Date.now() when the user pressed Cut & Download

// Display-percent range per pipeline stage → used to estimate time remaining
// while a step runs (elapsed / progress-rate). Indeterminate stages have an
// empty range (no estimate, just elapsed).
const STAGE_RANGE = {
  extract: [],
  download: [5, 80],
  encode: [80, 95],
  convert: [80, 95],
   verify: [],
};

function startStepTick() {
  if (stepTickId != null) return;
  stepTickId = setInterval(renderStepTimers, 100);
}

function stopStepTick() {
  if (stepTickId != null) {
    clearInterval(stepTickId);
    stepTickId = null;
  }
}

/** Estimate remaining ms from elapsed + display percent within a stage range.
 * `range` is [min, max] display percent; null when there's no progress yet, the
 * stage is indeterminate, or the estimate is meaningless. The last case matters:
 * ffmpeg's out_time reaches the end while muxing still runs, so the display %
 * saturates at the range max before the step truly finishes — a "~0.000 left"
 * while still processing is misleading, so we drop it. */
export function timeLeft(elapsed, pct, range) {
  if (range.length !== 2 || pct == null || pct <= range[0] || range[1] <= range[0]) {
    return null;
  }
  const prog = (pct - range[0]) / (range[1] - range[0]);
  const remaining = (elapsed / prog) * (1 - prog);
  if (remaining < 500) return null;
  return remaining;
}

/** Set a step's two-line time display: elapsed (top) + estimate (bottom). */
function setStepTime(i, elapsedText, estText) {
  const tEl = $(PIPELINE_STEPS[i].id).querySelector(".step-time");
  const el = tEl && tEl.querySelector(".step-elapsed");
  const es = tEl && tEl.querySelector(".step-est");
  if (el) el.textContent = elapsedText || "";
  if (es) es.textContent = estText || "";
}

/** Round a remaining estimate to whole seconds — keeps it from jittering. */
function roundedRemaining(ms) {
  if (ms == null || ms < 500) return null;
  return Math.max(0, Math.round(ms / 1000) * 1000);
}

/** Show per-step time spans. Active steps: elapsed (top) + ~remaining (bottom),
 * from the backend estimate when available, else the progress rate. Waiting
 * steps: the backend estimate so it's visible from the start. */
function renderStepTimers() {
  for (let i = 0; i < PIPELINE_STEPS.length; i++) {
    const stage = PIPELINE_STEPS[i].stage;
    const t = stepTimers[stage];
    if (t && t.running) {
      const elapsed = performance.now() - t.start;
      let remaining = null;
      const est = stageEstimate[stage];
      if (est != null && est > 0 && elapsed < est) {
        remaining = est - elapsed;
      } else {
        // Static estimate exhausted/zero (throttling, heuristic too low) →
        // fall back to the adaptive progress-rate estimate.
        remaining = timeLeft(elapsed, stagePct[stage], STAGE_RANGE[stage]);
      }
      const rem = roundedRemaining(remaining);
      setStepTime(
        i,
        msToTime(elapsed),
        rem != null ? `~${msToTime(rem)} left` : "",
      );
    } else if (!t && stageEstimate[stage] != null && stageEstimate[stage] > 0) {
      setStepTime(i, "", `~${msToTime(stageEstimate[stage])}`);
    }
  }
}

/** Store the backend-computed per-stage estimates and render them immediately
 * (the job's first SSE snapshot carries estimate_ms, so the pipeline shows
 * estimates as soon as the job is submitted). */
function applyStepEstimates(job) {
  const est = job && job.estimate_ms;
  if (!est) return;
  for (const s of PIPELINE_STEPS) {
    if (est[s.stage] != null) stageEstimate[s.stage] = est[s.stage];
  }
  renderStepTimers();
}

function setStepStatus(i, cls) {
  const el = $(PIPELINE_STEPS[i].id);
  el.className = cls ? `step-row ${cls}` : "step-row";
  // Status is conveyed by the row's text color (.active / .done / .skip) —
  // no separate status text.
  const stage = PIPELINE_STEPS[i].stage;
  if (cls === "active") {
    const t = stepTimers[stage] || { start: performance.now(), running: true };
    if (t.start == null) t.start = performance.now();
    t.running = true;
    stepTimers[stage] = t;
    startStepTick();
  } else if (cls === "done") {
    const t = stepTimers[stage];
    if (t && t.running) {
      t.running = false;
      setStepTime(i, msToTime(performance.now() - t.start), "");
    }
  } else {
    delete stepTimers[stage];
    setStepTime(
      i,
      "",
      stageEstimate[stage] != null && stageEstimate[stage] > 0
        ? `~${msToTime(stageEstimate[stage])}`
        : "",
    );
  }
  // Re-encode only exists in precise mode — hide the step in other modes.
  if (i === ENCODE_STEP) {
    el.classList.toggle("hidden", pipelineMode !== "accurate");
  }
  // Format conversion only runs when the output is not mp4.
  if (i === CONVERT_STEP) {
    el.classList.toggle("hidden", pipelineFmt === "mp4");
  }
}

function resetPipeline() {
  seenStages.clear();
  for (const k of Object.keys(stepTimers)) delete stepTimers[k];
  for (const k of Object.keys(stagePct)) delete stagePct[k];
  for (const k of Object.keys(stageEstimate)) delete stageEstimate[k];
  stopStepTick();
  for (let i = 0; i < PIPELINE_STEPS.length; i++) {
    setStepStatus(i, "");
  }
}

/** Backend stage → status of each step (active / done / skipped). */
function updatePipeline(stage) {
  const i = PIPELINE_STEPS.findIndex((s) => s.stage === stage);
  if (i < 0) return;
  seenStages.add(stage);
  for (let j = 0; j < PIPELINE_STEPS.length; j++) {
    if (j < i) {
      const skipped = !seenStages.has(PIPELINE_STEPS[j].stage);
      setStepStatus(j, skipped ? "skip" : "done");
    } else if (j === i) {
      setStepStatus(j, "active");
    } else {
      setStepStatus(j, "");
    }
  }
}

function finishPipeline() {
  for (let j = 0; j < PIPELINE_STEPS.length; j++) {
    if (seenStages.has(PIPELINE_STEPS[j].stage)) {
      setStepStatus(j, "done");
    } else if (!$(PIPELINE_STEPS[j].id).classList.contains("hidden")) {
      // A reused (cached) job arrives already "done" — no stage events were
      // streamed, so mark every applicable (visible) step done too.
      setStepStatus(j, "done");
    }
  }
}

/** Show the processing card (called when a job starts / is followed). */
function showProcessingCard(mode = null, fmt = "mp4") {
  pipelineMode = mode;
  pipelineFmt = fmt;
  els.processingCard.classList.remove("hidden");
  els.processIcon.className = "process-icon";
  els.processIcon.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin"></i>`;
  els.processTitle.textContent = "Processing Video…";
  els.processSubtitle.textContent =
    "Please wait, the server is preparing your video segment.";
  els.processPercent.textContent = "0%";
  els.progressStage.textContent = "";
  resetPipeline();
}

/** Processing card → success state (called when the job is done). */
function finishProcessingCard() {
  els.processIcon.className = "process-icon success";
  els.processIcon.innerHTML = `<i class="fa-solid fa-circle-check"></i>`;
  els.processTitle.textContent = "Cut Complete!";
  els.processSubtitle.textContent =
    "Your video segment was processed successfully and is ready to download.";
  els.processPercent.textContent = "100%";
  els.progressStage.textContent = "Done";
  finishPipeline();
}

function showProgress(message, percent, stage) {
  els.progressArea.classList.remove("hidden");
  els.progressText.textContent = message;
  if (stage) {
    els.progressStage.textContent = stage;
    updatePipeline(stage);
  }
  if (percent === null || percent === undefined) {
    els.progressFill.classList.add("indeterminate");
  } else {
    els.progressFill.classList.remove("indeterminate");
    els.progressFill.style.width = Math.min(100, Math.max(0, percent)) + "%";
    els.processPercent.textContent = `${Math.min(100, Math.floor(percent))}%`;
  }
  if (stage) stagePct[stage] = percent; // for per-step time-remaining estimates
}

function hideProgress() {
  els.progressArea.classList.add("hidden");
  els.progressFill.classList.remove("indeterminate");
  els.progressFill.style.width = "0%";
}

/**
 * Bitrate (kbps) for the result resolution; "best" → the highest available.
 * Returns null when unknown (e.g. no /api/info).
 */
function bitrateForRes(res) {
  const btns = els.resStack.querySelectorAll(".res-btn");
  if (!res || res === "best") {
    let max = 0;
    for (const b of btns) {
      const v = parseFloat(b.dataset.bitrate || "");
      if (isFinite(v) && v > max) max = v;
    }
    return max > 0 ? max : null;
  }
  const b = els.resStack.querySelector(`.res-btn[data-value="${res}"]`);
  const v = b ? parseFloat(b.dataset.bitrate || "") : NaN;
  return isFinite(v) && v > 0 ? v : null;
}

/** Format byte size → KB/MB/GB (decimal). */
function formatSize(bytes) {
  if (!isFinite(bytes) || bytes < 0) return "–";
  if (bytes >= 1e9) return (bytes / 1e9).toFixed(2) + " GB";
  if (bytes >= 1e6) return (bytes / 1e6).toFixed(1) + " MB";
  if (bytes >= 1e3) return (bytes / 1e3).toFixed(1) + " KB";
  return bytes + " B";
}

function showDownload(job) {
  els.downloadLink.href = job.download_url;
  const linkSpan = els.downloadLink.querySelector("span");
  if (linkSpan) {
    linkSpan.textContent = `Download ${job.file ? job.file.split(".").pop() : "mp4"}`;
  }
  els.downloadArea.classList.remove("hidden");
  // Resolution + duration badges in the result card.
  const resLabel =
    job.resolution == null || job.resolution === "best"
      ? "Best (auto)"
      : `${job.resolution}p`;
  els.resResultBadge.textContent = resLabel;
  els.durResultBadge.textContent =
    job.actual_duration_ms != null ? msToTime(job.actual_duration_ms) : "–";
  // Est. size from bitrate: duration × bitrate(kbps) × 1000 / 8 — only as a
  // fallback for old jobs; the server now sends the real file size.
  let durMs = job.actual_duration_ms;
  if (durMs == null && state.durationMs > 0) durMs = endMs() - startMs();
  const bitrate = bitrateForRes(job.resolution);
  els.sizeResultBadge.textContent =
    job.file_size != null
      ? formatSize(job.file_size)
      : durMs != null && bitrate
        ? formatSize((durMs / 1000) * bitrate * 1000 / 8)
        : "–";
  // Total elapsed: server truth (created_at → done). Survives reload / follow /
  // multi-node; falls back to the frontend click time for older jobs.
  const ssa = job.stage_started_at || {};
  const createdMs = job.created_at != null ? job.created_at * 1000 : jobStartTime;
  const doneMs = ssa.done != null ? ssa.done * 1000 : Date.now();
  els.timeResultBadge.textContent =
    createdMs != null ? msToTime(Math.max(0, doneMs - createdMs)) : "–";
  applyStepDurations(job);
  const parts = [];
  if (job.snap_delta_ms != null && job.snap_delta_ms > 300) {
    // Fast is frame-accurate (accurate_seek) — this warning only shows when
    // the result deviates from the request (anomaly).
    parts.push(
      `⚠ Result shifted ±${(job.snap_delta_ms / 1000).toFixed(1)}s from the requested time`
    );
  }
  els.downloadMeta.textContent = parts.join(" · ");
  els.downloadLink.scrollIntoView({ behavior: "smooth", block: "center" });
  finishProcessingCard();
}

/** Fill per-step durations from backend stage timestamps. The live step timer
 * lives in the browser (lost on reload); these server timestamps are the
 * source of truth and also cover a followed/cached job. */
function applyStepDurations(job) {
  const ssa = job.stage_started_at;
  if (!ssa) return;
  const order = PIPELINE_STEPS.map((s) => s.stage).concat("done");
  for (let i = 0; i < PIPELINE_STEPS.length; i++) {
    const st = PIPELINE_STEPS[i].stage;
    if (ssa[st] == null) continue;
    let end = null;
    for (let j = i + 1; j < order.length; j++) {
      if (ssa[order[j]] != null) {
        end = ssa[order[j]];
        break;
      }
    }
    if (end == null) end = job.created_at != null ? Date.now() / 1000 : null;
    if (end != null) {
      setStepTime(i, msToTime(Math.max(0, (end - ssa[st]) * 1000)), "");
    }
  }
}

/** Re-fetch the server's latest calibrated estimate params (after a job
 * completes the calibration may have changed — e.g. a slower-than-expected
 * download) and re-render the pre-cut estimate. */
async function refreshEstimateParams() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    if (data.estimate_params) {
      state.estimateParams = data.estimate_params;
      renderEstimate();
    }
  } catch {
    /* offline — keep the current params */
  }
}

/** Actual mode: "auto" resolves to fast/accurate (fast preferred). */
function currentMode() {
  const sel = document.querySelector('input[name="mode"]:checked').value;
  if (sel !== "auto") return sel;
  // Fast is already frame-accurate at start; short clips still use precise so
  // the tail is also clean. Speed stays the priority.
  const durS = (endMs() - startMs()) / 1000;
  return durS < 10 ? "accurate" : "fast";
}

/** Highlight the cut-mode card matching the selected radio. */
function syncTrimCardActive() {
  const checked = document.querySelector('input[name="mode"]:checked');
  for (const card of document.querySelectorAll(".trim-card")) {
    const radio = card.querySelector('input[name="mode"]');
    card.classList.toggle("active", radio === checked);
  }
}
/* ---------------- live cut-time estimate (heuristic, from user inputs) ---------------- */

// Encode-speed / throughput defaults, used until /api/info delivers the
// server's real benchmark (estimate_params). The server measures its actual
// x264/vp9 encode speed at first use and sends it back — estimates then match
// the machine instead of hardcoded guesses.
const ESTIMATE_DEFAULTS = { x264_fps: 200, vp9_fps: 80, throughput_bps: 6_000_000 };

function estimateParams() {
  return state.estimateParams || ESTIMATE_DEFAULTS;
}

/** Effective resolution button for the current selection (best → highest bitrate). */
function effectiveResButton() {
  if (state.selectedRes !== "best") {
    return els.resStack.querySelector(`.res-btn[data-value="${state.selectedRes}"]`);
  }
  let maxB = 0;
  let btn = null;
  for (const b of els.resStack.querySelectorAll(".res-btn")) {
    const bv = parseFloat(b.dataset.bitrate || "");
    if (isFinite(bv) && bv > maxB) {
      maxB = bv;
      btn = b;
    }
  }
  return btn;
}

/** Rough expected processing seconds for the current inputs, or null. */
export function estimateSeconds() {
  const durS = (endMs() - startMs()) / 1000;
  if (!(durS > 0) || state.durationMs <= 0) return null;
  const btn = effectiveResButton();
  const bitrate = btn ? parseFloat(btn.dataset.bitrate || "") || 0 : 0;
  const fps = btn ? parseFloat(btn.dataset.fps || "30") || 30 : 30;
  const codec = btn ? (btn.dataset.codec || "h264") : "h264";
  const height = btn ? parseInt(btn.dataset.value, 10) : 1080;
  const factor = (height * (height * 16 / 9)) / (1080 * 1920);
  const p = estimateParams();
  const extractS = (p.extract_ms || 2000) / 1000;
  const verifyS = (p.verify_ms || 1000) / 1000;
  const downloadS = bitrate > 0 ? (durS * bitrate * 1000 / 8) / p.throughput_bps : 0;

  let encodeS = 0;
  if (currentMode() === "accurate") encodeS = (durS * fps * factor) / p.x264_fps;

  let convertS = 0;
  const fmt = state.selectedFmt;
  if (fmt === "webm") {
    convertS = (durS * fps * factor) / p.vp9_fps;
  } else if (fmt === "mov") {
    const movSafe = codec === "H.264" || codec === "HEVC";
    convertS = movSafe ? 1 : (durS * fps * factor) / p.x264_fps;
  }

  return extractS + downloadS + encodeS + convertS + verifyS;
}

/** Show "≈ HH:MM:SS.mmm" under the Cut button; hidden when no video is loaded. */
export function renderEstimate() {
  const s = estimateSeconds();
  if (s == null) {
    els.cutEstimate.classList.add("hidden");
    els.cutEstimate.textContent = "";
    return;
  }
  els.cutEstimate.textContent = `≈ ${msToTime(s * 1000)} (estimate)`;
  els.cutEstimate.classList.remove("hidden");
}

async function startCut() {
  const url = els.urlInput.value.trim();
  const startMs = timeToMs(els.startInput.value);
  const endMs = timeToMs(els.endInput.value);
  if (startMs === null || endMs === null) {
    showError("Invalid time format. Example: 00:01:23.456");
    return;
  }
  if (endMs - startMs > MAX_TRIM_MS) {
    showError(`Trim is limited to 5 minutes (got ${msToTime(endMs - startMs)}).`);
    return;
  }
  hideError();
  const mode = currentMode();
  jobStartTime = Date.now();
  showProcessingCard(mode, state.selectedFmt);
  hideProgress();
  els.downloadArea.classList.add("hidden");
  els.cutBtn.disabled = true;
  els.cutBtn.textContent = "Cutting…";
  showProgress("Submitting job…", 0);

  try {
    const resBtn = effectiveResButton();
    const res = await fetch("/api/cut", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        start_ms: startMs,
        end_ms: endMs,
        resolution: state.selectedRes,
        mode,
        format: state.selectedFmt,
        // Hint the backend with the effective stream so it can estimate each
        // pipeline stage's duration (the same data the pre-cut estimate uses).
        hints: resBtn
          ? {
              bitrate_kbps: parseFloat(resBtn.dataset.bitrate || "") || 0,
              fps: parseFloat(resBtn.dataset.fps || "30") || 30,
              codec: resBtn.dataset.codec || "h264",
              height: parseInt(resBtn.dataset.value, 10) || 1080,
            }
          : null,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to create job.");
    saveJobId(data.job_id);
    pollJob(data.job_id);
  } catch (e) {
    resetToInitial();
    showError(e.message || "Failed to create job.");
  }
}

/**
 * Follow a job via Server-Sent Events (replaces long polling). The server
 * pushes every update_job; the connection closes automatically at done/error.
 * If EventSource is unavailable/fails (e.g. via proxy), fall back to polling.
 */
function pollJob(jobId) {
  try {
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    es.onmessage = (ev) => {
        const job = JSON.parse(ev.data);
        if (job.status === "done") {
          es.close();
          clearJobId(); // done — no need to follow it on reload/new tabs anymore
          hideProgress();
          showDownload(job);
          finishCutUi();
          refreshEstimateParams(); // re-calibrate for the next cut
          return;
        }
        if (job.status === "error") {
          es.close();
          resetToInitial();
          showError(job.error || "An error occurred.");
          return;
        }
        applyStepEstimates(job); // show per-step estimates right away
        showProgress(job.message || job.stage, job.percent, job.stage);
      };
      es.onerror = () => {
        // Connection dropped — fall back to polling so the UI stays in sync.
        es.close();
        pollJobPolling(jobId);
      };
  } catch {
    // EventSource unavailable / failed to create — fall back to polling.
    pollJobPolling(jobId);
  }
}

/** Fallback: 1s polling (when SSE is unavailable). */
async function pollJobPolling(jobId) {
  for (;;) {
    const res = await fetch(`/api/jobs/${jobId}`);
    const job = await res.json();
    if (job.status === "done") {
      clearJobId(); // done — no need to follow it on reload/new tabs anymore
      hideProgress();
      showDownload(job);
      finishCutUi();
      refreshEstimateParams(); // re-calibrate for the next cut
      return;
    }
    if (job.status === "error") {
      resetToInitial();
      showError(job.error || "An error occurred.");
      return;
    }
    applyStepEstimates(job); // show per-step estimates right away
    showProgress(job.message || job.stage, job.percent, job.stage);
    await new Promise((r) => setTimeout(r, 1000));
  }
}

function finishCutUi() {
  els.cutBtn.disabled = false;
  els.cutBtn.textContent = "▶ Cut & Download";
}

async function loadVideo() {
  await loadVideoUrl(els.urlInput.value.trim());
}

/**
 * Load a video from URL: embed + /api/info → applyInfo, then (optionally)
 * apply initial params (start/end/resolution/mode) from the followed job.
 * Returns true if the info loaded successfully.
 */
async function loadVideoUrl(url, initial = null) {
  const videoId = parseYouTubeId(url);
  if (!videoId) {
    showError("Invalid YouTube URL. Example: https://www.youtube.com/watch?v=dQw4w9WgXcQ");
    return false;
  }
  hideError();
  els.loadBtn.disabled = true;
  els.loadBtn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin" aria-hidden="true"></i><span>Loading…</span>';
  // Loading state: hide the stale studio card and show a shimmer while the
  // slow /api/info extract runs (resolutions differ per video, so the old
  // resolution stack must not linger).
  els.playerSection.classList.add("hidden");
  els.loadingCard.classList.remove("hidden");
  try {
    loadEmbed(videoId); // embed immediately → instant feedback
  } catch {
    /* embed failed — video info still loads via /api/info */
  }

  try {
    const res = await fetch("/api/info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to load video info.");
    applyInfo(data);
    if (initial) applyJobParams(initial);
    els.loadingCard.classList.add("hidden");
    els.playerSection.classList.remove("hidden");
    return true;
  } catch (e) {
    resetToInitial();
    showError(e.message || "Failed to load video info.");
    return false;
  } finally {
    els.loadBtn.disabled = false;
    els.loadBtn.innerHTML = '<i class="fa-solid fa-circle-play" aria-hidden="true"></i><span>Load Video</span>';
  }
}

/**
 * Apply the followed job's params to the UI after video info loads:
 * start/end (clamped to duration), resolution choice, and cut mode.
 */
function applyJobParams(job) {
  const start = Math.min(Math.max(0, job.start_ms ?? 0), state.durationMs);
  const end = Math.min(Math.max(start + 1, job.end_ms ?? state.durationMs), state.durationMs);
  els.startSlider.value = String(start);
  els.endSlider.value = String(end);
  syncFromSliders();

  if (job.resolution && job.resolution !== "best") {
    const b = els.resStack.querySelector(`.res-btn[data-value="${job.resolution}"]`);
    if (b) {
      for (const x of els.resStack.querySelectorAll(".res-btn")) {
        x.classList.remove("active");
      }
      b.classList.add("active");
      state.selectedRes = job.resolution;
    }
  }

  if (job.mode === "fast" || job.mode === "accurate") {
    const radio = document.querySelector(`input[name="mode"][value="${job.mode}"]`);
    if (radio) {
      radio.checked = true;
      syncTrimCardActive();
    }
  }

  if (job.format && ["mp4", "mov", "webm"].includes(job.format)) {
    for (const b of els.fmtStack.querySelectorAll(".res-btn")) {
      const active = b.dataset.format === job.format;
      b.classList.toggle("active", active);
      if (active) state.selectedFmt = job.format;
    }
  }
}

/* ---------------- restore job from localStorage (M5) ---------------- */

const JOB_KEY = "tubesnip_active_job";

function saveJobId(jobId) {
  try {
    localStorage.setItem(JOB_KEY, jobId);
  } catch {
    /* storage full / private — ignore */
  }
}

function clearJobId() {
  try {
    localStorage.removeItem(JOB_KEY);
  } catch {
    /* ignore */
  }
}

/**
 * On page open (e.g. reload / new tab), check for a saved job. If one exists,
 * offer a dialog: follow the old process or start fresh. localStorage is
 * shared across tabs, so two tabs opening the page both see this dialog.
 */
async function restoreJob() {
  let jobId = null;
  try {
    jobId = localStorage.getItem(JOB_KEY);
  } catch {
    return;
  }
  if (!jobId) return;
  try {
    const res = await fetch(`/api/jobs/${jobId}`);
    if (!res.ok) {
      clearJobId(); // job expired / no longer exists
      return;
    }
    const job = await res.json();
    if (job.status === "done") {
      clearJobId(); // job already finished — clear, no dialog needed
      return;
    }
    const label =
      job.status === "error"
        ? "failed"
        : "still running";
    els.jobDialogText.textContent =
      `A cut process is ${label}${job.title ? `: “${job.title}”` : ""}. ` +
      "Would you like to follow this process, or start over?";
    els.jobDialog.classList.remove("hidden");
    els.jobFollow.onclick = () => {
      els.jobDialog.classList.add("hidden");
      // Player & processing card are hidden on a fresh page — open them first
      // so the follow is visible. Reload video info (URL, embed, duration,
      // options) from the job data, then start following.
      els.playerSection.classList.remove("hidden");
      showProcessingCard(job.mode, job.format || "mp4");
      jobStartTime = Date.now();
      if (job.url) {
        els.urlInput.value = job.url;
        loadVideoUrl(job.url, job).then((ok) => {
          if (ok) pollJob(job.id);
        });
      } else {
        pollJob(job.id);
      }
    };
    els.jobDiscard.onclick = () => {
      clearJobId();
      els.jobDialog.classList.add("hidden");
    };
  } catch {
    // server not ready / offline — leave it; the dialog shows on next reload
  }
}

/* ---------------- events ---------------- */

els.loadBtn.addEventListener("click", loadVideo);
els.demoLink.addEventListener("click", (e) => {
  e.preventDefault();
  const url = "https://www.youtube.com/watch?v=Lcvt7agiBmI";
  els.urlInput.value = url;
  // 01:12:45.000 → 01:13:37.000
  loadVideoUrl(url, { start_ms: 4365000, end_ms: 4417000 });
});
els.urlInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadVideo();
});
els.cutBtn.addEventListener("click", startCut);
els.setStartBtn.addEventListener("click", setStartFromPlayhead);
els.setEndBtn.addEventListener("click", setEndFromPlayhead);

els.startSlider.addEventListener("input", () => syncFromSliders("start"));
els.endSlider.addEventListener("input", () => syncFromSliders("end"));
els.startInput.addEventListener("change", syncFromStartInput);
els.endInput.addEventListener("change", syncFromEndInput);

// Output format stack: static buttons (MP4/MOV/WebM) → highlight + state.
for (const b of els.fmtStack.querySelectorAll(".res-btn")) {
  b.addEventListener("click", () => {
    for (const x of els.fmtStack.querySelectorAll(".res-btn")) {
      x.classList.remove("active");
    }
    b.classList.add("active");
    state.selectedFmt = b.dataset.format;
    renderEstimate();
  });
}

// Mode radios: clicking a card checks the radio + syncs the highlight
// explicitly (no reliance on the change event from label activation, whose
// behavior differs across browsers/tests).
for (const card of document.querySelectorAll(".trim-card")) {
  card.addEventListener("click", () => {
    const radio = card.querySelector('input[name="mode"]');
    if (radio) {
      radio.checked = true;
      syncTrimCardActive();
      renderEstimate();
    }
  });
}
// Radio changes via keyboard/other JS → stay in sync.
for (const r of document.querySelectorAll('input[name="mode"]')) {
  r.addEventListener("change", () => {
    syncTrimCardActive();
    renderEstimate();
  });
}
syncTrimCardActive();

// Another tab starts a new job → this tab also offers the follow dialog (multi-tab).
window.addEventListener("storage", (e) => {
  if (e.key === JOB_KEY) restoreJob();
});

restoreJob();

// Exported for frontend tests.
export { restoreJob, currentMode, syncFromSliders, updatePlayhead };

/** Test hook: reset module state to initial (app.js is imported once). */
export function __resetStateForTest() {
  state.durationMs = 0;
  state.videoId = null;
  state.hasAudio = true;
  state.selectedRes = "best";
  state.selectedFmt = "mp4";
  state.estimateParams = null;
  seenStages.clear();
  pipelineFmt = "mp4";
  jobStartTime = null;
  for (const k of Object.keys(stepTimers)) delete stepTimers[k];
  for (const k of Object.keys(stagePct)) delete stagePct[k];
  for (const k of Object.keys(stageEstimate)) delete stageEstimate[k];
  stopStepTick();
  for (let i = 0; i < PIPELINE_STEPS.length; i++) {
    setStepStatus(i, "");
  }
  destroyPlyr();
}
