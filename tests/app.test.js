// DOM interaction tests for app.js (happy-dom + mocked fetch).
// Run: deno test --allow-read --allow-env
import { afterEach, describe, it } from "@std/testing/bdd";
import { expect } from "@std/expect";
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { Window } from "happy-dom";

const __dirname = dirname(fileURLToPath(import.meta.url));
const INDEX_HTML = readFileSync(
  join(__dirname, "..", "src", "tubesnip", "static", "index.html"),
  "utf8"
);
const BODY_HTML = INDEX_HTML.match(/<body>([\s\S]*)<\/body>/)[1];

let win;
let fetchLog = [];
let appBooted = false;
let scrollToCalls = 0;
let appApi = null; // app.js module namespace (for calling functions directly in tests)

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const INFO_OK = {
  video_id: "dQw4w9WgXcQ",
  title: "Rick Astley - Never Gonna Give You Up",
  duration_ms: 212000,
  is_live: false,
  has_audio: true,
  resolutions: [
    { height: 144, fps: 30, codec: "H.264", ext: "mp4", bitrate: 100 },
    { height: 720, fps: 60, codec: "VP9", ext: "webm", bitrate: 2000 },
  ],
};

/** Restore the whole DOM to its initial state (app.js is imported once). */
function resetDom() {
  const add = (id, cls) => byId(id).classList.add(cls);
  byId("error-msg").textContent = "";
  add("error-msg", "hidden");
  byId("url-input").value = "";
  add("player-section", "hidden");
  add("loading-card", "hidden");
  byId("player").innerHTML = "";
  byId("video-title").textContent = "";
  byId("video-title").title = "";
  byId("start-input").value = "00:00:00.000";
  byId("end-input").value = "";
  byId("start-slider").value = "0";
  byId("end-slider").value = "0";
  byId("start-slider").min = "0";
  byId("start-slider").max = "0";
  byId("end-slider").min = "0";
  byId("end-slider").max = "0";
  byId("slider-start-label").textContent = "00:00:00.000";
  byId("slider-end-label").textContent = "00:00:00.000";
  byId("slider-fill").style.left = "0%";
  byId("slider-fill").style.width = "0%";
  byId("slider-range-info").textContent = "00:00:00.000 → 00:00:00.000";
  byId("res-stack").innerHTML = "";
  for (const b of byId("fmt-stack").querySelectorAll(".res-btn")) {
    b.classList.toggle("active", b.dataset.format === "mp4");
  }
  add("no-audio-warn", "hidden");
  byId("cut-btn").disabled = true;
  byId("cut-btn").textContent = "▶ Cut & Download";
  add("processing-card", "hidden");
  byId("process-title").textContent = "Processing Video…";
  byId("process-subtitle").textContent = "";
  byId("process-percent").textContent = "0%";
  byId("process-icon").className = "process-icon";
  byId("progress-stage").textContent = "";
  for (const id of ["step-1", "step-2", "step-3", "step-4", "step-5"]) {
    byId(id).className = "step-row";
    byId(id).querySelector(".step-status-text").textContent = "Waiting…";
  }
  add("progress-area", "hidden");
  byId("progress-fill").classList.remove("indeterminate");
  byId("progress-fill").style.width = "0%";
  byId("progress-text").textContent = "";
  add("download-area", "hidden");
  byId("download-link").href = "#";
  const dlSpan = byId("download-link").querySelector("span");
  if (dlSpan) dlSpan.textContent = "Download Trimmed Video";
  byId("download-meta").textContent = "";
  byId("res-result-badge").textContent = "–";
  byId("dur-result-badge").textContent = "–";
  byId("size-result-badge").textContent = "–";
  byId("load-btn").disabled = false;
  byId("load-btn").textContent = "Load Video";
  add("mode-options", "hidden");
  add("job-dialog", "hidden");
  byId("job-dialog-text").textContent = "";
  win.document.querySelector('input[name="mode"][value="auto"]').checked = true;
  win.document.querySelector('input[name="mode"][value="fast"]').checked = false;
  win.document.querySelector('input[name="mode"][value="accurate"]').checked = false;
  for (const c of win.document.querySelectorAll(".trim-card")) {
    c.classList.remove("active");
  }
  win.document.querySelector('.trim-card input[value="auto"]').closest(".trim-card").classList.add("active");
  try {
    win.localStorage.removeItem("tubesnip_active_job");
  } catch {
    /* ignore */
  }
  FakeEventSource.reset();
  FakePlyr.reset();
  scrollToCalls = 0;
  byId("current-playhead-time").textContent = "00:00:00.000";
  byId("btn-set-start").disabled = true;
  byId("btn-set-end").disabled = true;
  appApi.__resetStateForTest();
}

/**
 * Boot: app.js is imported ONCE (coverage accumulates). Between tests it's
 * enough to reset the DOM and swap the mocked fetch.
 */
async function bootApp(scriptedResponses = [], { hangWhenEmpty = false } = {}) {
  if (!appBooted) {
    win = new Window({ url: "http://localhost/" });
    win.document.body.innerHTML = BODY_HTML;
    win.HTMLElement.prototype.scrollIntoView ??= () => {};
    win.Plyr = FakePlyr; // fake Plyr always available
    win.scrollTo = () => {
      scrollToCalls += 1;
    };

    globalThis.window = win;
    globalThis.document = win.document;
    globalThis.navigator = win.navigator;
    globalThis.HTMLElement = win.HTMLElement;
    globalThis.Node = win.Node;
    globalThis.Event = win.Event;
    globalThis.CustomEvent = win.CustomEvent;
    globalThis.getComputedStyle = win.getComputedStyle.bind(win);
    // deno has a localStorage getter — assignment silently fails, use defineProperty
    Object.defineProperty(globalThis, "localStorage", {
      value: win.localStorage,
      configurable: true,
    });

    appApi = await import("../src/tubesnip/static/app.js");
    appBooted = true;
  }

  resetDom();
  const queue = [...scriptedResponses];
  fetchLog = [];
  globalThis.fetch = async (url, opts) => {
    fetchLog.push({ url: String(url), opts });
    if (queue.length === 0) {
      if (hangWhenEmpty) return new Promise(() => {}); // polling hangs
      throw new Error("unexpected fetch: " + url);
    }
    return queue.shift();
  };
}

/**
 * Fake Plyr. Mimics the real behavior app.js relies on: creates an embed
 * iframe inside the target element (id from data-plyr-embed-id), exposes
 * embed.cueVideoById (positioning without play) and currentTime/playing/destroy.
 * Readiness is detected by the app via `embed` + `duration > 0` on each
 * updatePlayhead tick.
 */
class FakePlyr {
  static instances = [];

  constructor(element, opts = {}) {
    this.opts = opts;
    this._currentTime = 0;
    this._paused = true;
    this._destroyed = false;
    this.duration = 88000; // > 0 → treated as ready by updatePlayhead
    this.embed = {
      cueVideoById: (cue) => {
        this._currentTime = (cue && cue.startSeconds) || 0;
        this._paused = true;
      },
    };
    const el = element;
    if (el) {
      el.innerHTML =
        `<iframe src="https://www.youtube.com/embed/${el.getAttribute("data-plyr-embed-id") || ""}?enablejsapi=1&rel=0"` +
        ` title="YouTube video player" frameborder="0" allowfullscreen></iframe>`;
    }
    FakePlyr.instances.push(this);
  }

  get currentTime() {
    return this._currentTime;
  }

  set currentTime(v) {
    this._currentTime = v;
  }

  get playing() {
    return !this._paused;
  }

  get destroyed() {
    return this._destroyed;
  }

  play() {
    this._paused = false;
  }

  pause() {
    this._paused = true;
  }

  destroy() {
    this._destroyed = true;
  }

  static reset() {
    FakePlyr.instances = [];
  }
}

/** Last FakePlyr instance created. */
function lastPlyr() {
  return FakePlyr.instances[FakePlyr.instances.length - 1];
}

const originalEventSource = globalThis.EventSource;

function restoreEventSource() {
  if (originalEventSource === undefined) {
    delete globalThis.EventSource;
  } else {
    globalThis.EventSource = originalEventSource;
  }
}

afterEach(() => {
  globalThis.setTimeout = originalSetTimeout;
  restoreEventSource();
});

const originalSetTimeout = globalThis.setTimeout;

/** setTimeout fires immediately (no 1s wait) for polling. */
function useSyncTimers() {
  globalThis.setTimeout = (fn) => {
    fn();
    return 0;
  };
}

/**
 * Fake EventSource: tests can dispatch SSE messages & trigger errors.
 * Instances are tracked so tests can send events to the active connection.
 */
class FakeEventSource {
  static instances = [];

  constructor(url) {
    this.url = String(url);
    this.onmessage = null;
    this.onerror = null;
    this.closed = false;
    FakeEventSource.instances.push(this);
  }

  dispatch(job) {
    if (this.onmessage) {
      this.onmessage({ data: JSON.stringify(job) });
    }
  }

  fail() {
    if (this.onerror) this.onerror({});
  }

  close() {
    this.closed = true;
  }

  static reset() {
    FakeEventSource.instances = [];
  }
}

/** Enable the SSE path in tests (EventSource available). */
function useFakeEventSource() {
  globalThis.EventSource = FakeEventSource;
}

function lastES() {
  return FakeEventSource.instances[FakeEventSource.instances.length - 1];
}

function byId(id) {
  return win.document.getElementById(id);
}

/** Flush the microtask chain (fetch → res.json → applyInfo / pollJob). */
async function flushAsync() {
  for (let i = 0; i < 50; i++) await Promise.resolve();
}

function click(id) {
  byId(id).dispatchEvent(new win.Event("click", { bubbles: true }));
}

function input(id, value) {
  const el = byId(id);
  el.value = value;
  el.dispatchEvent(new win.Event("input", { bubbles: true }));
}

function change(id, value) {
  const el = byId(id);
  el.value = value;
  el.dispatchEvent(new win.Event("change", { bubbles: true }));
}

async function loadVideoViaUi() {
  byId("url-input").value = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";
  click("load-btn");
  await flushAsync();
}

describe("loadVideo", () => {
  it("invalid URL → error, fetch not called", async () => {
    await bootApp();
    byId("url-input").value = "https://example.com/not-youtube";
    click("load-btn");
    await flushAsync();
    const err = byId("error-msg");
    expect(err.classList.contains("hidden")).toBe(false);
    expect(err.textContent).toContain("Invalid YouTube URL");
    expect(fetchLog.length).toBe(0);
    expect(byId("player-section").classList.contains("hidden")).toBe(true);
    expect(byId("mode-options").classList.contains("hidden")).toBe(true);
  });

  it("success: embed, title, default time, options, active button", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();

    const iframe = byId("player").querySelector("iframe");
    expect(iframe).not.toBeNull();
    expect(iframe.getAttribute("src")).toContain("dQw4w9WgXcQ");

    expect(byId("video-title").textContent).toBe(INFO_OK.title);
    expect(byId("player-section").classList.contains("hidden")).toBe(false);
    expect(byId("mode-options").classList.contains("hidden")).toBe(false); // only visible after /api/info
    expect(byId("cut-btn").disabled).toBe(false);

    // Default start 0, end = full duration.
    expect(byId("start-input").value).toBe("00:00:00.000");
    expect(byId("end-input").value).toBe("00:03:32.000");
    expect(byId("start-slider").value).toBe("0");
    expect(byId("end-slider").value).toBe(String(INFO_OK.duration_ms));

    // Resolution stack: "best" + available resolutions.
    const resVals = [...byId("res-stack").querySelectorAll(".res-btn")].map((b) => b.dataset.value);
    expect(resVals).toEqual(["best", "144", "720"]);
    expect(byId("res-stack").querySelector('.res-btn[data-value="best"]').classList.contains("active")).toBe(true);
    expect(byId("res-stack").querySelector('.res-btn[data-value="144"]').textContent).toContain("144p");

    // Audio present → banner hidden.
    expect(byId("no-audio-warn").classList.contains("hidden")).toBe(true);

    // Player ready (polling tick) → Set Start/End buttons enabled.
    appApi.updatePlayhead();
    expect(byId("btn-set-start").disabled).toBe(false);
    expect(byId("btn-set-end").disabled).toBe(false);
  });

  it("video without audio → warning banner shown", async () => {
    await bootApp([jsonResponse({ ...INFO_OK, has_audio: false })]);
    await loadVideoViaUi();
    expect(byId("no-audio-warn").classList.contains("hidden")).toBe(false);
  });

  it("API error → message shown, button restored", async () => {
    await bootApp([jsonResponse({ detail: "Video not available (private / removed / blocked)." }, 400)]);
    await loadVideoViaUi();
    const err = byId("error-msg");
    expect(err.classList.contains("hidden")).toBe(false);
    expect(err.textContent).toContain("not available");
    expect(byId("load-btn").disabled).toBe(false);
    expect(byId("load-btn").textContent).toBe("Load Video");
    expect(byId("mode-options").classList.contains("hidden")).toBe(true); // stays hidden on failure
  });

  it("fetch fails (network) → error shown", async () => {
    await bootApp(); // empty queue → fetch throws
    await loadVideoViaUi();
    expect(byId("error-msg").classList.contains("hidden")).toBe(false);
    expect(byId("load-btn").disabled).toBe(false);
  });

  it("Enter in URL input triggers load", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    byId("url-input").value = "https://youtu.be/dQw4w9WgXcQ";
    byId("url-input").dispatchEvent(
      new win.KeyboardEvent("keydown", { key: "Enter", bubbles: true })
    );
    await flushAsync();
    expect(fetchLog.length).toBe(1);
    expect(fetchLog[0].url).toBe("/api/info");
  });

  it("demo link fills the URL and applies the start/end range", async () => {
    useSyncTimers();
    const DEMO_INFO = { ...INFO_OK, duration_ms: 8_000_000 }; // longer than the demo end
    await bootApp([jsonResponse(DEMO_INFO)]);
    click("demo-link");
    await flushAsync();
    expect(byId("url-input").value).toBe("https://www.youtube.com/watch?v=Lcvt7agiBmI");
    expect(fetchLog[0].url).toBe("/api/info");
    expect(JSON.parse(fetchLog[0].opts.body).url).toContain("Lcvt7agiBmI");
    expect(byId("start-input").value).toBe("01:12:45.000");
    expect(byId("end-input").value).toBe("01:13:37.000");
    expect(byId("player-section").classList.contains("hidden")).toBe(false);
    expect(byId("loading-card").classList.contains("hidden")).toBe(true);
  });

  it("switching URL shows the loading skeleton and hides the stale card", async () => {
    await bootApp([jsonResponse(INFO_OK)], { hangWhenEmpty: true });
    await loadVideoViaUi(); // first video loaded
    expect(byId("player-section").classList.contains("hidden")).toBe(false);
    // Swap to a different video → /api/info hangs → shimmer replaces the old card.
    byId("url-input").value = "https://youtu.be/abc123";
    click("load-btn");
    await flushAsync();
    expect(byId("loading-card").classList.contains("hidden")).toBe(false);
    expect(byId("player-section").classList.contains("hidden")).toBe(true);
  });
});

describe("slider & time inputs (two-way sync)", () => {
  it("dragging start slider → input & label follow", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    input("start-slider", "5000");
    expect(byId("start-input").value).toBe("00:00:05.000");
    expect(byId("slider-start-label").textContent).toBe("00:00:05.000");
    // Slider keeps the full range (no dynamic min/max)
    expect(byId("end-slider").min).toBe("0");
    expect(byId("start-slider").max).toBe(String(INFO_OK.duration_ms));
    expect(parseFloat(byId("slider-fill").style.left)).toBeCloseTo(2.35849, 4);
  });

  it("dragging end slider → input & label follow", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    input("end-slider", "10000");
    expect(byId("end-input").value).toBe("00:00:10.000");
    expect(byId("slider-end-label").textContent).toBe("00:00:10.000");
  });

  it("start & end both dragged: no mutual jumping, invariant holds", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    // 1) end dragged down
    input("end-slider", "100000");
    expect(byId("end-input").value).toBe("00:01:40.000");
    expect(byId("start-slider").value).toBe("0"); // start doesn't move
    // 2) start dragged up
    input("start-slider", "80000");
    expect(byId("start-input").value).toBe("00:01:20.000");
    expect(byId("end-slider").value).toBe("100000"); // end doesn't move
    // 3) end dragged past start → end clamped to start+1, start stays
    input("end-slider", "70000");
    expect(byId("end-slider").value).toBe("80001");
    expect(byId("start-slider").value).toBe("80000");
    expect(byId("end-input").value).toBe("00:01:20.001");
    // 4) start dragged past end → start clamped to end-1, end stays
    input("start-slider", "90000");
    expect(byId("start-slider").value).toBe("80000");
    expect(byId("end-slider").value).toBe("80001");
    // text inputs always stay in sync with the sliders
    expect(byId("start-input").value).toBe("00:01:20.000");
  });

  it("typing start in input → slider follows", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    change("start-input", "00:00:08.000");
    expect(byId("start-slider").value).toBe("8000");
  });

  it("start >= end clamped to end-1", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    change("start-input", "00:00:15.000"); // end is still 00:03:32
    expect(byId("start-slider").value).toBe("15000");
    // End set below start → clamped up to start+1 (start < end invariant).
    change("end-input", "00:00:10.000");
    expect(byId("end-slider").value).toBe("15001");
    // Start set above end → clamped down to end-1.
    change("start-input", "00:00:20.000");
    expect(byId("start-slider").value).toBe("15000");
    expect(byId("start-input").value).toBe("00:00:15.000");
  });

  it("end > duration clamped to duration", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    change("end-input", "00:10:00.000");
    expect(byId("end-slider").value).toBe(String(INFO_OK.duration_ms));
    expect(byId("end-input").value).toBe("00:03:32.000");
  });

  it("invalid time input → ignored", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    change("start-input", "not-a-time");
    expect(byId("start-slider").value).toBe("0");
    expect(byId("start-input").value).toBe("not-a-time"); // user text untouched
  });

  it("invalid end input → ignored (slider stays)", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    change("end-input", "not-a-time");
    expect(byId("end-slider").value).toBe(String(INFO_OK.duration_ms));
    expect(byId("end-input").value).toBe("not-a-time");
  });

  it("slider dragged before video loads → no crash (start<end invariant)", async () => {
    await bootApp(); // no video yet: duration 0, start=end=0
    input("start-slider", "5000");
    expect(byId("start-slider").value).toBe("0"); // clamped to 0
    expect(byId("start-input").value).toBe("00:00:00.000");
  });
});

describe("preview positioned at start when start/end adjusted (no autoplay)", () => {
  it("dragging start → preview seeks to start, no autoplay (debounce)", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    useSyncTimers();
    input("start-slider", "12000");
    // debounce (600ms) fires immediately thanks to useSyncTimers → seek only
    const p = lastPlyr();
    expect(p.currentTime).toBeCloseTo(12, 5);
    expect(p.playing).toBe(false); // not autoplay
  });

  it("changing end input → preview moves (seek, no play)", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    useSyncTimers();
    change("end-input", "00:00:30.000");
    const p = lastPlyr();
    expect(p.currentTime).toBe(0); // seek to clip start
    expect(p.playing).toBe(false);
  });

  it("without a ready player → time adjustments don't error", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    useSyncTimers();
    appApi.__resetStateForTest(); // player = null (as if not created yet)
    input("start-slider", "5000");
    expect(byId("start-input").value).toBe("00:00:05.000");
  });

  it("before a video loads, time adjustments don't touch the player", async () => {
    await bootApp();
    appApi.__resetStateForTest(); // module state is persistent → zero it first
    useSyncTimers();
    input("start-slider", "5000");
    expect(byId("start-input").value).toBe("00:00:00.000"); // clamped to 0 (duration 0)
  });
});

describe("preview stops at end then returns to start (ready to replay)", () => {
  it("playing & passing end → stops & position returns to start", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    change("end-input", "00:00:30.000"); // end = 30s
    const p = lastPlyr();
    p.play(); // PLAYING state
    p.currentTime = 30.5; // past the end
    appApi.updatePlayhead();
    expect(p.currentTime).toBe(0); // back to clip start
    expect(p.playing).toBe(false); // stopped (not looping)
  });

  it("playing exactly at end → stops & returns to start", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    change("end-input", "00:00:30.000");
    const p = lastPlyr();
    p.play();
    p.currentTime = 30.0;
    appApi.updatePlayhead();
    expect(p.currentTime).toBe(0);
    expect(p.playing).toBe(false);
  });

  it("after stopping, next poll doesn't re-trigger (playing guard)", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    change("end-input", "00:00:30.000");
    const p = lastPlyr();
    p.play();
    p.currentTime = 30.5;
    appApi.updatePlayhead(); // stopped → playing false, position at start
    appApi.updatePlayhead(); // next poll: no-op
    expect(p.currentTime).toBe(0); // stays at start
  });

  it("paused past end → not snapped (manual scrub safe)", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    change("end-input", "00:00:30.000");
    const p = lastPlyr();
    p.pause(); // PAUSED state
    p.currentTime = 40;
    appApi.updatePlayhead();
    expect(p.currentTime).toBe(40); // unchanged
    expect(p.playing).toBe(false);
  });

  it("not past the end → no action", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    change("end-input", "00:00:30.000");
    const p = lastPlyr();
    p.play();
    p.currentTime = 10;
    appApi.updatePlayhead();
    expect(p.currentTime).toBe(10);
  });

  it("before a video loads (duration 0) → no action", async () => {
    await bootApp();
    appApi.__resetStateForTest();
    const p = lastPlyr();
    if (p) p.play(); // if an old player exists
    appApi.updatePlayhead();
    // doesn't throw, doesn't seek (state.videoId null)
  });
});

describe("Set Start / Set End (playhead from plyr)", () => {
  it("buttons enabled once the player is ready; playhead chip follows currentTime", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    appApi.updatePlayhead(); // polling tick → ready detection (embed + duration)
    expect(byId("btn-set-start").disabled).toBe(false);
    expect(byId("btn-set-end").disabled).toBe(false);
    const p = lastPlyr();
    p.currentTime = 65.25;
    appApi.updatePlayhead();
    expect(byId("current-playhead-time").textContent).toBe("00:01:05.250");
  });

  it("click Set Start → clip start follows the playhead", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    lastPlyr().currentTime = 15.5;
    click("btn-set-start");
    expect(byId("start-slider").value).toBe("15500");
    expect(byId("start-input").value).toBe("00:00:15.500");
  });

  it("click Set End → clip end follows the playhead", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    lastPlyr().currentTime = 200.75;
    click("btn-set-end");
    expect(byId("end-slider").value).toBe("200750");
    expect(byId("end-input").value).toBe("00:03:20.750");
  });

  it("playhead past end → Set Start clamped to end-1 (invariant)", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    lastPlyr().currentTime = 300; // > duration (212s)
    click("btn-set-start");
    expect(byId("start-slider").value).toBe(String(INFO_OK.duration_ms - 1));
    expect(byId("start-input").value).toBe("00:03:31.999");
  });

  it("playhead before start → Set End clamped to start+1", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    lastPlyr().currentTime = 0.0001; // < 1 ms → rounds to 0 → clamped up
    click("btn-set-end");
    expect(byId("end-slider").value).toBe("1"); // start 0 → end clamped to 1
    expect(byId("end-input").value).toBe("00:00:00.001");
  });

  it("player not ready → clicks change nothing", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    appApi.__resetStateForTest(); // player null → playhead null
    click("btn-set-start");
    expect(byId("start-slider").value).toBe("0");
    expect(byId("start-input").value).toBe("00:00:00.000");
    // Set End is also a no-op without a playhead.
    click("btn-set-end");
    expect(byId("end-input").value).toBe("00:03:32.000");
  });
});

describe("load Plyr (YouTube preview player)", () => {
  it("Plyr not loaded yet (empty window.Plyr) → no embed; reloaded once available", async () => {
    await bootApp([jsonResponse(INFO_OK), jsonResponse(INFO_OK)]);
    const savedPlyr = win.Plyr;
    win.Plyr = undefined; // simulate the plyr script not being loaded yet
    try {
      byId("url-input").value = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";
      click("load-btn");
      await flushAsync();

      // Without Plyr: no embed, but /api/info still runs.
      expect(FakePlyr.instances.length).toBe(0);
      expect(byId("player").querySelector("iframe")).toBeNull();
      expect(byId("btn-set-start").disabled).toBe(true);

      // Plyr now available → reload → player is created right away.
      win.Plyr = FakePlyr;
      click("load-btn");
      await flushAsync();
      expect(FakePlyr.instances.length).toBe(1);
      expect(byId("player").querySelector("iframe")).not.toBeNull();
      appApi.updatePlayhead(); // polling tick → ready detection
      expect(byId("btn-set-start").disabled).toBe(false);
    } finally {
      win.Plyr = savedPlyr;
    }
  });

  it("Plyr fails to create (invalid video id) → load continues via /api/info", async () => {
    const origPlyr = win.Plyr;
    win.Plyr = class ThrowingPlyr {
      constructor() {
        throw new Error("Invalid video id");
      }
    };
    try {
      await bootApp([jsonResponse(INFO_OK)]);
      await loadVideoViaUi();
      // Embed fails but /api/info succeeds → section stays visible & cuttable.
      expect(byId("player-section").classList.contains("hidden")).toBe(false);
      expect(byId("video-title").textContent).toBe(INFO_OK.title);
      expect(byId("cut-btn").disabled).toBe(false);
      expect(byId("load-btn").disabled).toBe(false);
    } finally {
      win.Plyr = origPlyr;
    }
  });

  it("loading a second video → old player destroyed, polling swapped", async () => {
    await bootApp([jsonResponse(INFO_OK), jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    const first = lastPlyr();
    expect(first.destroyed).toBe(false);

    byId("url-input").value = "https://youtu.be/abc123";
    click("load-btn");
    await flushAsync();
    expect(first.destroyed).toBe(true); // loadEmbed destroys the old player
    expect(FakePlyr.instances.length).toBe(2);
    expect(byId("player").querySelector("iframe").getAttribute("src")).toContain("abc123");
  });
});

describe("cut mode: auto (fast preferred)", () => {
  async function boot() {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
  }

  it("default auto; long clip → fast", async () => {
    await boot();
    expect(appApi.currentMode()).toBe("fast");
  });

  it("short clip (< 10s) → accurate", async () => {
    await boot();
    input("start-slider", "1000");
    input("end-slider", "6000");
    expect(appApi.currentMode()).toBe("accurate");
  });

  it("manual fast selection in options is honored", async () => {
    await boot();
    const fastRadio = win.document.querySelector('input[name="mode"][value="fast"]');
    fastRadio.checked = true;
    fastRadio.dispatchEvent(new win.Event("change", { bubbles: true }));
    input("start-slider", "1000");
    input("end-slider", "6000"); // short clip, but user forces fast
    expect(appApi.currentMode()).toBe("fast");
  });

  it("clicking a mode card → highlight moves + radio follows (UI fix)", async () => {
    await boot();
    const cardAuto = win.document.querySelector('.trim-card input[value="auto"]').closest(".trim-card");
    const cardFast = win.document.querySelector('.trim-card input[value="fast"]').closest(".trim-card");
    const cardAcc = win.document.querySelector('.trim-card input[value="accurate"]').closest(".trim-card");
    expect(cardAuto.classList.contains("active")).toBe(true);
    expect(cardFast.classList.contains("active")).toBe(false);

    // Click the "Fast" card (label → radio): highlight + description move along.
    cardFast.click();
    expect(cardFast.classList.contains("active")).toBe(true);
    expect(cardAuto.classList.contains("active")).toBe(false);
    expect(cardAcc.classList.contains("active")).toBe(false);
    expect(win.document.querySelector('input[name="mode"]:checked').value).toBe("fast");

    // Click the "Accurate" card → highlight moves again.
    cardAcc.click();
    expect(cardAcc.classList.contains("active")).toBe(true);
    expect(cardFast.classList.contains("active")).toBe(false);
    expect(win.document.querySelector('input[name="mode"]:checked').value).toBe("accurate");
  });
});

describe("restore job from localStorage (reload / multi-tab)", () => {
  it("saved job → follow dialog appears", async () => {
    await bootApp([jsonResponse({ id: "abc123", status: "running", title: "Video X" })]);
    win.localStorage.setItem("tubesnip_active_job", "abc123");
    await appApi.restoreJob();
    await flushAsync();
    expect(byId("job-dialog").classList.contains("hidden")).toBe(false);
    expect(byId("job-dialog-text").textContent).toContain("Video X");
    expect(byId("job-dialog-text").textContent).toContain("still running");
  });

  it("click 'Follow' → UI fully filled (URL, embed, duration, options) + job polling", async () => {
    await bootApp([
      jsonResponse({
        id: "abc123",
        status: "running",
        title: "Video X",
        url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        start_ms: 5000,
        end_ms: 9000,
        resolution: "720",
        mode: "fast",
      }),
      jsonResponse(INFO_OK),
      jsonResponse({ id: "abc123", status: "done", download_url: "/api/download/abc123", file: "out.mp4", actual_duration_ms: 4000, snap_delta_ms: 5 }),
    ]);
    win.localStorage.setItem("tubesnip_active_job", "abc123");
    await appApi.restoreJob();
    await flushAsync();
    useSyncTimers();
    click("job-follow");
    await flushAsync();

    // Dialog closed + job polling runs until done (result card appears).
    expect(byId("job-dialog").classList.contains("hidden")).toBe(true);
    expect(byId("download-area").classList.contains("hidden")).toBe(false);
    // Job finished → localStorage cleared (no need to keep following).
    expect(win.localStorage.getItem("tubesnip_active_job")).toBeNull();

    // URL filled + embed created from the job's video id.
    expect(byId("url-input").value).toBe("https://www.youtube.com/watch?v=dQw4w9WgXcQ");
    expect(FakePlyr.instances.length).toBeGreaterThan(0);
    expect(byId("video-title").textContent).toBe(INFO_OK.title);

    // Duration & start/end filled from the job.
    expect(byId("start-slider").value).toBe("5000");
    expect(byId("end-slider").value).toBe("9000");
    expect(byId("start-input").value).toBe("00:00:05.000");
    expect(byId("end-input").value).toBe("00:00:09.000");

    // Options filled: job resolution active + job mode checked.
    const res720 = win.document.querySelector('.res-btn[data-value="720"]');
    expect(res720).not.toBeNull();
    expect(res720.classList.contains("active")).toBe(true);
    expect(win.document.querySelector('.res-btn[data-value="best"]').classList.contains("active")).toBe(false);
    expect(win.document.querySelector('input[name="mode"]:checked').value).toBe("fast");
  });

  it("click 'Follow' without a url in the job → keeps polling without loading info", async () => {
    await bootApp([
      jsonResponse({ id: "abc123", status: "running", title: "Video X" }),
      jsonResponse({ id: "abc123", status: "done", download_url: "/api/download/abc123", file: "out.mp4", actual_duration_ms: 4000, snap_delta_ms: 5 }),
    ]);
    win.localStorage.setItem("tubesnip_active_job", "abc123");
    await appApi.restoreJob();
    await flushAsync();
    useSyncTimers();
    click("job-follow");
    await flushAsync();
    expect(byId("job-dialog").classList.contains("hidden")).toBe(true);
    expect(byId("download-area").classList.contains("hidden")).toBe(false);
    expect(byId("url-input").value).toBe("");
  });

  it("click 'Start over' → localStorage cleared", async () => {
    await bootApp([jsonResponse({ id: "abc123", status: "running", title: "Video X" })]);
    win.localStorage.setItem("tubesnip_active_job", "abc123");
    await appApi.restoreJob();
    await flushAsync();
    click("job-discard");
    expect(byId("job-dialog").classList.contains("hidden")).toBe(true);
    expect(win.localStorage.getItem("tubesnip_active_job")).toBeNull();
  });

  it("job no longer exists (404) → no dialog, storage cleared", async () => {
    await bootApp();
    win.localStorage.setItem("tubesnip_active_job", "hilang123");
    globalThis.fetch = async () => new Response("not found", { status: 404 });
    await appApi.restoreJob();
    await flushAsync();
    expect(byId("job-dialog").classList.contains("hidden")).toBe(true);
    expect(win.localStorage.getItem("tubesnip_active_job")).toBeNull();
  });

  it("job with error status → 'failed' label in dialog", async () => {
    await bootApp([jsonResponse({ id: "abc123", status: "error", title: "Video X" })]);
    win.localStorage.setItem("tubesnip_active_job", "abc123");
    await appApi.restoreJob();
    await flushAsync();
    expect(byId("job-dialog-text").textContent).toContain("failed");
  });

  it("job already done → storage cleared, no dialog", async () => {
    await bootApp([jsonResponse({ id: "abc123", status: "done", title: "Video X" })]);
    win.localStorage.setItem("tubesnip_active_job", "abc123");
    await appApi.restoreJob();
    await flushAsync();
    expect(byId("job-dialog").classList.contains("hidden")).toBe(true);
    expect(win.localStorage.getItem("tubesnip_active_job")).toBeNull();
  });

  it("server unresponsive (fetch throws) → no crash, storage kept", async () => {
    await bootApp();
    win.localStorage.setItem("tubesnip_active_job", "abc123");
    globalThis.fetch = async () => {
      throw new Error("network down");
    };
    await appApi.restoreJob();
    await flushAsync();
    expect(byId("job-dialog").classList.contains("hidden")).toBe(true);
    expect(win.localStorage.getItem("tubesnip_active_job")).toBe("abc123"); // don't delete — the job may still exist
  });

  it("localStorage.getItem throws → restoreJob silently aborts", async () => {
    await bootApp();
    const origGet = win.localStorage.getItem.bind(win.localStorage);
    win.localStorage.getItem = () => {
      throw new Error("storage blocked");
    };
    await appApi.restoreJob();
    await flushAsync();
    win.localStorage.getItem = origGet;
    expect(byId("job-dialog").classList.contains("hidden")).toBe(true);
    expect(fetchLog.length).toBe(0);
  });

  it("localStorage.setItem throws while saving job id → no crash", async () => {
    useSyncTimers();
    await bootApp([
      jsonResponse(INFO_OK),
      jsonResponse({ job_id: "job123" }),
      jsonResponse({ status: "done", download_url: "/api/download/job123", file: "x.mp4", actual_duration_ms: 4000, snap_delta_ms: 100 }),
    ]);
    await loadVideoViaUi();
    const origSet = win.localStorage.setItem.bind(win.localStorage);
    win.localStorage.setItem = () => {
      throw new Error("quota exceeded");
    };
    click("cut-btn");
    await flushAsync();
    win.localStorage.setItem = origSet;
    expect(byId("download-area").classList.contains("hidden")).toBe(false);
  });

  it("localStorage.removeItem throws while clearing job id → no crash", async () => {
    await bootApp();
    win.localStorage.setItem("tubesnip_active_job", "hilang123");
    const origRemove = win.localStorage.removeItem.bind(win.localStorage);
    win.localStorage.removeItem = () => {
      throw new Error("quota exceeded");
    };
    globalThis.fetch = async () => new Response("not found", { status: 404 });
    await appApi.restoreJob();
    await flushAsync();
    win.localStorage.removeItem = origRemove;
    expect(byId("job-dialog").classList.contains("hidden")).toBe(true);
  });

  it("storage event from another tab → follow dialog appears", async () => {
    await bootApp([jsonResponse({ id: "abc123", status: "running", title: "Video X" })]);
    win.localStorage.setItem("tubesnip_active_job", "abc123");
    const ev = new win.Event("storage");
    Object.defineProperty(ev, "key", { value: "tubesnip_active_job" });
    win.dispatchEvent(ev);
    await flushAsync();
    expect(byId("job-dialog").classList.contains("hidden")).toBe(false);
  });

  it("storage event for another key → ignored", async () => {
    await bootApp();
    win.dispatchEvent(new win.Event("storage")); // key null ≠ JOB_KEY
    await flushAsync();
    expect(fetchLog.length).toBe(0);
  });
});

describe("startCut (cut & download)", () => {
  it("invalid time format → error without fetch", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    change("start-input", "abc");
    click("cut-btn");
    await flushAsync();
    expect(byId("error-msg").textContent).toContain("Invalid time format");
    expect(fetchLog.length).toBe(1); // only /api/info from load
  });

  it("success → poll until done → download link + duration meta", async () => {
    useSyncTimers();
    await bootApp([
      jsonResponse(INFO_OK),
      jsonResponse({ job_id: "job123" }),
      jsonResponse({
        status: "done",
        download_url: "/api/download/job123",
        file: "hasil.mp4",
        actual_duration_ms: 4500,
        snap_delta_ms: 500,
      }),
    ]);
    await loadVideoViaUi();

    // Pick 144p via the stack + precise mode so the body matches.
    byId("res-stack").querySelector('.res-btn[data-value="144"]').click();
    win.document.querySelector('input[name="mode"][value="accurate"]').checked = true;

    click("cut-btn");
    await flushAsync();

    const cutReq = fetchLog.find((f) => f.url === "/api/cut");
    expect(cutReq).toBeDefined();
    const body = JSON.parse(cutReq.opts.body);
    expect(body.start_ms).toBe(0);
    expect(body.end_ms).toBe(INFO_OK.duration_ms);
    expect(body.resolution).toBe("144");
    expect(body.mode).toBe("accurate");

    expect(byId("download-area").classList.contains("hidden")).toBe(false);
    expect(byId("download-link").getAttribute("href")).toBe("/api/download/job123");
    expect(byId("download-link").textContent).toContain("Download");
    expect(byId("download-meta").textContent).toContain("00:00:04.500");
    expect(byId("download-meta").textContent).toContain("Result shifted");
    expect(byId("cut-btn").disabled).toBe(false);
    expect(byId("cut-btn").textContent).toBe("▶ Cut & Download");
  });

  it("job error → error message shown", async () => {
    useSyncTimers();
    await bootApp([
      jsonResponse(INFO_OK),
      jsonResponse({ job_id: "job123" }),
      jsonResponse({ status: "error", error: "End exceeds video duration (0:03:32)." }),
    ]);
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();
    expect(byId("error-msg").textContent).toContain("End exceeds");
    // Error → UI reset to initial state (button disabled).
    expect(byId("cut-btn").disabled).toBe(true);
  });

  it("running → progress bar shown (polling hangs on running status)", async () => {
    await bootApp(
      [
        jsonResponse(INFO_OK),
        jsonResponse({ job_id: "job123" }),
        jsonResponse({ status: "running", stage: "download", percent: 42, message: "Downloading…" }),
      ],
      { hangWhenEmpty: true }
    );
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();

    expect(byId("progress-text").textContent).toContain("Downloading");
    expect(byId("progress-fill").style.width).toBe("42%");
    expect(byId("progress-area").classList.contains("hidden")).toBe(false);
    expect(byId("download-area").classList.contains("hidden")).toBe(true);
  });

  it("running with null percent → indeterminate", async () => {
    await bootApp(
      [
        jsonResponse(INFO_OK),
        jsonResponse({ job_id: "job123" }),
        jsonResponse({ status: "running", stage: "extract", percent: null, message: "Fetching video info…" }),
      ],
      { hangWhenEmpty: true }
    );
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();
    expect(byId("progress-fill").classList.contains("indeterminate")).toBe(true);
  });

  it("cut fetch fails (network) → error", async () => {
    await bootApp([jsonResponse(INFO_OK)]); // queue exhausted after /api/info
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();
    expect(byId("error-msg").classList.contains("hidden")).toBe(false);
    expect(byId("cut-btn").disabled).toBe(true); // reset to initial state
  });

  it("POST /api/cut replies non-OK → detail shown", async () => {
    await bootApp([
      jsonResponse(INFO_OK),
      jsonResponse({ detail: "End exceeds video duration." }, 400),
    ]);
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();
    expect(byId("error-msg").textContent).toContain("End exceeds video duration.");
    // Error → reset: button back to disabled (no video loaded yet).
    expect(byId("cut-btn").disabled).toBe(true);
    expect(byId("cut-btn").textContent).toBe("▶ Cut & Download");
  });

  it("repeated polling: running → done", async () => {
    useSyncTimers();
    await bootApp([
      jsonResponse(INFO_OK),
      jsonResponse({ job_id: "job123" }),
      jsonResponse({ status: "running", stage: "download", percent: 50, message: "Downloading…" }),
      jsonResponse({ status: "done", download_url: "/api/download/job123", file: "x.mp4", actual_duration_ms: 4000, snap_delta_ms: 100 }),
    ]);
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();
    expect(byId("download-area").classList.contains("hidden")).toBe(false);
  });  it("small snap → no shift warning", async () => {
    useSyncTimers();
    await bootApp([
      jsonResponse(INFO_OK),
      jsonResponse({ job_id: "job123" }),
      jsonResponse({ status: "done", download_url: "/api/download/job123", file: "x.mp4", actual_duration_ms: 4000, snap_delta_ms: 100 }),
    ]);
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();

    expect(byId("download-meta").textContent).not.toContain("shifted");
    expect(byId("download-meta").textContent).toContain("00:00:04.000");
  });
});

describe("follow job via SSE (EventSource)", () => {
  it("startCut → EventSource opened, running → progress, done → download", async () => {
    useFakeEventSource();
    await bootApp([jsonResponse(INFO_OK), jsonResponse({ job_id: "job123" })]);
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();

    const es = lastES();
    expect(es).toBeDefined();
    expect(es.url).toBe("/api/jobs/job123/events");
    expect(es.closed).toBe(false);

    es.dispatch({ status: "running", stage: "download", percent: 42, message: "Downloading…" });
    expect(byId("progress-text").textContent).toContain("Downloading");
    expect(byId("progress-fill").style.width).toBe("42%");
    expect(byId("progress-area").classList.contains("hidden")).toBe(false);

    es.dispatch({
      status: "done",
      download_url: "/api/download/job123",
      file: "hasil.mp4",
      actual_duration_ms: 4000,
      snap_delta_ms: 100,
    });
    expect(byId("download-area").classList.contains("hidden")).toBe(false);
    expect(byId("download-link").getAttribute("href")).toBe("/api/download/job123");
    expect(es.closed).toBe(true);
    expect(byId("cut-btn").disabled).toBe(false);
  });

  it("error event from SSE → error message + connection closed", async () => {
    useFakeEventSource();
    await bootApp([jsonResponse(INFO_OK), jsonResponse({ job_id: "job123" })]);
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();

    const es = lastES();
    es.dispatch({ status: "error", error: "Audio and video are out of sync." });
    expect(byId("error-msg").textContent).toContain("out of sync");
    expect(es.closed).toBe(true);
    expect(byId("cut-btn").disabled).toBe(true); // reset to initial state
  });

  it("error → full UI reset: sections hidden, inputs cleared, scroll to top", async () => {
    useFakeEventSource();
    await bootApp([jsonResponse(INFO_OK), jsonResponse({ job_id: "job123" })]);
    await loadVideoViaUi();
    byId("url-input").value = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";
    byId("start-input").value = "00:00:10.000";
    click("cut-btn");
    await flushAsync();

    // Before the error: sections visible.
    expect(byId("player-section").classList.contains("hidden")).toBe(false);
    expect(byId("processing-card").classList.contains("hidden")).toBe(false);
    expect(scrollToCalls).toBe(0);

    lastES().dispatch({ status: "error", error: "Stream merge failed." });

    // Sections hidden, inputs cleared, scroll to top.
    expect(byId("processing-card").classList.contains("hidden")).toBe(true);
    expect(byId("player-section").classList.contains("hidden")).toBe(true);
    expect(byId("url-input").value).toBe("");
    expect(byId("start-input").value).toBe("00:00:00.000");
    expect(byId("end-input").value).toBe("");
    expect(byId("player").innerHTML).toBe("");
    expect(byId("error-msg").classList.contains("hidden")).toBe(false);
    expect(byId("error-msg").textContent).toContain("Stream merge failed");
    expect(scrollToCalls).toBe(1);
  });

  it("SSE connection dropped (onerror) → polling fallback", async () => {
    useFakeEventSource();
    useSyncTimers();
    await bootApp([
      jsonResponse(INFO_OK),
      jsonResponse({ job_id: "job123" }),
      jsonResponse({ status: "done", download_url: "/api/download/job123", file: "x.mp4", actual_duration_ms: 4000, snap_delta_ms: 100 }),
    ]);
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();

    lastES().fail(); // SSE fails → pollJobPolling takes over
    await flushAsync();
    expect(lastES().closed).toBe(true);
    expect(byId("download-area").classList.contains("hidden")).toBe(false);
  });
});

describe("restore job: 'Follow' opens player-section (reload)", () => {
  it("click Follow → player-section visible + SSE opened", async () => {
    useFakeEventSource();
    await bootApp([jsonResponse({ id: "abc123", status: "running", title: "Video X" })]);
    win.localStorage.setItem("tubesnip_active_job", "abc123");
    await appApi.restoreJob();
    await flushAsync();
    expect(byId("job-dialog").classList.contains("hidden")).toBe(false);

    click("job-follow");
    await flushAsync();
    // Previously: player-section + processing card hidden → progress invisible.
    // Now both are opened so the follow-up is visible.
    expect(byId("player-section").classList.contains("hidden")).toBe(false);
    expect(byId("processing-card").classList.contains("hidden")).toBe(false);
    expect(lastES()).toBeDefined();
    expect(lastES().url).toBe("/api/jobs/abc123/events");

    lastES().dispatch({ status: "running", stage: "download", percent: 7, message: "Downloading…" });
    expect(byId("progress-text").textContent).toContain("Downloading");
    expect(byId("progress-fill").style.width).toBe("7%");
  });
});

describe("resolution stack (replaces dropdown)", () => {
  it("clicking a resolution → active + used in /api/cut payload", async () => {
    useSyncTimers();
    await bootApp([
      jsonResponse(INFO_OK),
      jsonResponse({ job_id: "job123" }),
      jsonResponse({ status: "done", download_url: "/api/download/job123", file: "x.mp4", actual_duration_ms: 4000, snap_delta_ms: 100 }),
    ]);
    await loadVideoViaUi();

    const btn720 = byId("res-stack").querySelector('.res-btn[data-value="720"]');
    btn720.click();
    expect(btn720.classList.contains("active")).toBe(true);
    expect(byId("res-stack").querySelector('.res-btn[data-value="best"]').classList.contains("active")).toBe(false);

    click("cut-btn");
    await flushAsync();
    const cutReq = fetchLog.find((f) => f.url === "/api/cut");
    expect(JSON.parse(cutReq.opts.body).resolution).toBe("720");
  });

  it("default = best (active without clicking)", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    expect(byId("res-stack").querySelector('.res-btn[data-value="best"]').classList.contains("active")).toBe(true);
  });
});

describe("output format stack (mp4/mov/webm)", () => {
  it("default mp4 is active and sent in /api/cut payload", async () => {
    useSyncTimers();
    await bootApp([
      jsonResponse(INFO_OK),
      jsonResponse({ job_id: "job123" }),
      jsonResponse({ status: "done", download_url: "/api/download/job123", file: "x.mp4", actual_duration_ms: 4000, snap_delta_ms: 100 }),
    ]);
    await loadVideoViaUi();
    expect(byId("fmt-stack").querySelector('.res-btn[data-format="mp4"]').classList.contains("active")).toBe(true);
    click("cut-btn");
    await flushAsync();
    const cutReq = fetchLog.find((f) => f.url === "/api/cut");
    expect(JSON.parse(cutReq.opts.body).format).toBe("mp4");
  });

  it("clicking WebM → active state + format webm in payload", async () => {
    useSyncTimers();
    await bootApp([
      jsonResponse(INFO_OK),
      jsonResponse({ job_id: "job123" }),
      jsonResponse({ status: "done", download_url: "/api/download/job123", file: "final.webm", actual_duration_ms: 4000, snap_delta_ms: 100 }),
    ]);
    await loadVideoViaUi();
    const webmBtn = byId("fmt-stack").querySelector('.res-btn[data-format="webm"]');
    webmBtn.click();
    expect(webmBtn.classList.contains("active")).toBe(true);
    expect(byId("fmt-stack").querySelector('.res-btn[data-format="mp4"]').classList.contains("active")).toBe(false);
    click("cut-btn");
    await flushAsync();
    const cutReq = fetchLog.find((f) => f.url === "/api/cut");
    expect(JSON.parse(cutReq.opts.body).format).toBe("webm");
  });

  it("MOV stays independent of resolution selection", async () => {
    await bootApp([jsonResponse(INFO_OK)]);
    await loadVideoViaUi();
    const movBtn = byId("fmt-stack").querySelector('.res-btn[data-format="mov"]');
    movBtn.click();
    // Clicking a resolution must not disturb the format choice.
    byId("res-stack").querySelector('.res-btn[data-value="720"]').click();
    expect(movBtn.classList.contains("active")).toBe(true);
  });
});

describe("pipeline steps & result card (new design)", () => {
  it("stage from SSE → step status (active/done/skipped) + percent", async () => {
    useFakeEventSource();
    await bootApp([jsonResponse(INFO_OK), jsonResponse({ job_id: "job123" })]);
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();
    expect(byId("processing-card").classList.contains("hidden")).toBe(false);

    const es = lastES();
    es.dispatch({ status: "running", stage: "extract", percent: null, message: "Fetching video info…" });
    expect(byId("step-1").className).toContain("active");
    expect(byId("step-2").className).not.toContain("active");
    expect(byId("progress-fill").classList.contains("indeterminate")).toBe(true);

    es.dispatch({ status: "running", stage: "download", percent: 40, message: "Downloading…" });
    expect(byId("step-1").className).toContain("done");
    expect(byId("step-1").querySelector(".step-status-text").textContent).toContain("Done");
    expect(byId("step-2").className).toContain("active");
    expect(byId("process-percent").textContent).toBe("40%");

    // Fast: no re-encode → step 3 (precise mode only) is hidden; output is mp4
    // → step 4 (format conversion) is hidden too.
    es.dispatch({ status: "running", stage: "verify", percent: 97, message: "Verifying…" });
    expect(byId("step-2").className).toContain("done");
    expect(byId("step-3").className).toContain("hidden");
    expect(byId("step-4").className).toContain("hidden");
    expect(byId("step-5").className).toContain("active");
    expect(byId("progress-stage").textContent).toBe("verify");
  });

  it("done → all steps complete + result card badges + success state", async () => {
    useFakeEventSource();
    await bootApp([jsonResponse(INFO_OK), jsonResponse({ job_id: "job123" })]);
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();
    const es = lastES();

    es.dispatch({ status: "running", stage: "extract", percent: null, message: "Fetching video info…" });
    es.dispatch({ status: "running", stage: "download", percent: 50, message: "Downloading…" });
    es.dispatch({
      status: "done",
      download_url: "/api/download/job123",
      file: "hasil.mp4",
      actual_duration_ms: 4000,
      snap_delta_ms: 100,
      resolution: "720",
    });

    expect(byId("step-1").className).toContain("done");
    expect(byId("step-2").className).toContain("done");
    expect(byId("process-title").textContent).toContain("Cut Complete");
    expect(byId("process-icon").className).toContain("success");
    expect(byId("process-percent").textContent).toBe("100%");
    expect(byId("res-result-badge").textContent).toBe("720p");
    expect(byId("dur-result-badge").textContent).toBe("00:00:04.000");
    // 4s × 2000 kbps × 1000 / 8 = 1.000.000 byte → 1.0 MB
    expect(byId("size-result-badge").textContent).toBe("1.0 MB");
    expect(byId("download-area").classList.contains("hidden")).toBe(false);
  });

  it("convert step is visible and active when output is webm", async () => {
    useFakeEventSource();
    await bootApp([jsonResponse(INFO_OK), jsonResponse({ job_id: "job123" })]);
    await loadVideoViaUi();
    byId("fmt-stack").querySelector('.res-btn[data-format="webm"]').click();
    click("cut-btn");
    await flushAsync();
    const es = lastES();
    // WebM output → step-4 (format conversion) is shown (not hidden like mp4).
    expect(byId("step-4").className).not.toContain("hidden");
    expect(byId("step-3").className).toContain("hidden"); // fast, no re-encode
    es.dispatch({ status: "running", stage: "download", percent: 50, message: "Downloading…" });
    es.dispatch({ status: "running", stage: "convert", percent: 85, message: "Converting…" });
    expect(byId("step-4").className).toContain("active");
    expect(byId("step-5").className).not.toContain("active");
  });

  it("cached job arriving done → all visible steps marked done", async () => {
    useFakeEventSource();
    await bootApp([jsonResponse(INFO_OK), jsonResponse({ job_id: "job123" })]);
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();
    // No stage events streamed — the reused job is already "done".
    lastES().dispatch({
      status: "done",
      download_url: "/api/download/job123",
      file: "x.mp4",
      actual_duration_ms: 4000,
      snap_delta_ms: 100,
    });
    expect(byId("step-1").className).toContain("done");
    expect(byId("step-2").className).toContain("done");
    expect(byId("step-5").className).toContain("done");
    expect(byId("step-3").className).toContain("hidden"); // fast + mp4
    expect(byId("step-4").className).toContain("hidden");
    expect(byId("download-area").classList.contains("hidden")).toBe(false);
  });

  it("est. size follows the bitrate of the selected resolution (best = highest)", async () => {
    useFakeEventSource();
    await bootApp([jsonResponse(INFO_OK), jsonResponse({ job_id: "job123" })]);
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();
    const es = lastES();

    // 144p: 4s × 100 kbps × 1000 / 8 = 50,000 bytes → 50.0 KB
    es.dispatch({
      status: "done",
      download_url: "/api/download/job123",
      file: "x.mp4",
      actual_duration_ms: 4000,
      snap_delta_ms: 0,
      resolution: "144",
    });
    expect(byId("size-result-badge").textContent).toBe("50.0 KB");

    // best → highest bitrate (2000 kbps): 4s → 1.0 MB
    es.dispatch({
      status: "done",
      download_url: "/api/download/job123",
      file: "x.mp4",
      actual_duration_ms: 4000,
      snap_delta_ms: 0,
      resolution: "best",
    });
    expect(byId("size-result-badge").textContent).toBe("1.0 MB");
  });

  it("without /api/info (restore) → est. size '–'", async () => {
    useFakeEventSource();
    await bootApp([jsonResponse({ id: "abc123", status: "running", title: "Video X" })]);
    win.localStorage.setItem("tubesnip_active_job", "abc123");
    await appApi.restoreJob();
    await flushAsync();
    click("job-follow");
    await flushAsync();
    lastES().dispatch({
      status: "done",
      download_url: "/api/download/abc123",
      file: "x.mp4",
      actual_duration_ms: 4000,
      snap_delta_ms: 0,
    });
    expect(byId("size-result-badge").textContent).toBe("–");
  });

  it("done without resolution → badge 'Best (auto)'", async () => {
    useFakeEventSource();
    await bootApp([jsonResponse(INFO_OK), jsonResponse({ job_id: "job123" })]);
    await loadVideoViaUi();
    click("cut-btn");
    await flushAsync();
    lastES().dispatch({
      status: "done",
      download_url: "/api/download/job123",
      file: "x.mp4",
      actual_duration_ms: null,
      snap_delta_ms: 0,
    });
    expect(byId("res-result-badge").textContent).toBe("Best (auto)");
    expect(byId("dur-result-badge").textContent).toBe("–");
  });

  it("precise mode → encode stage present (not skipped)", async () => {
    useFakeEventSource();
    await bootApp([jsonResponse(INFO_OK), jsonResponse({ job_id: "job123" })]);
    await loadVideoViaUi();
    win.document.querySelector('input[name="mode"][value="accurate"]').checked = true;
    click("cut-btn");
    await flushAsync();
    const es = lastES();

    es.dispatch({ status: "running", stage: "extract", percent: null, message: "…" });
    es.dispatch({ status: "running", stage: "download", percent: 50, message: "…" });
    es.dispatch({ status: "running", stage: "encode", percent: 60, message: "Re-encode…" });
    expect(byId("step-2").className).toContain("done");
    expect(byId("step-3").className).not.toContain("hidden"); // visible in precise mode
    expect(byId("step-3").className).toContain("active");
    expect(byId("step-3").querySelector(".step-status-text").textContent).toBe("Processing…");
    expect(byId("process-percent").textContent).toBe("60%");
  });
});
