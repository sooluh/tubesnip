// Pure utilities (no DOM) for time parsing/formatting and slider constraints.
// Imported by app.js and tested by tests/time.test.js.

export function pad(n, len) {
  return String(n).padStart(len, "0");
}

/** ms → "HH:MM:SS.mmm" (negative values clamped to 0, milliseconds rounded). */
export function msToTime(ms) {
  const t = Math.max(0, Math.round(ms));
  const h = Math.floor(t / 3600000);
  const m = Math.floor((t % 3600000) / 60000);
  const s = Math.floor((t % 60000) / 1000);
  const mm = t % 1000;
  return `${pad(h, 2)}:${pad(m, 2)}:${pad(s, 2)}.${pad(mm, 3)}`;
}

/**
 * "HH:MM:SS.mmm" | "MM:SS.mmm" | "SS.mmm" → ms.
 * A comma is also accepted as decimal; fraction of 1-3 digits.
 * Returns null for invalid input.
 */
export function timeToMs(str) {
  const parts = String(str).trim().replace(",", ".").split(":");
  if (parts.length < 1 || parts.length > 3) return null;
  const secMatch = parts[parts.length - 1].match(/^(\d+)(?:\.(\d{1,3}))?$/);
  if (!secMatch) return null;
  let totalMs = parseInt(secMatch[1], 10) * 1000;
  if (secMatch[2] !== undefined) {
    totalMs += parseInt(secMatch[2].padEnd(3, "0"), 10);
  }
  if (parts.length >= 2) {
    if (!/^\d+$/.test(parts[parts.length - 2])) return null;
    totalMs += parseInt(parts[parts.length - 2], 10) * 60000;
  }
  if (parts.length === 3) {
    if (!/^\d+$/.test(parts[0])) return null;
    totalMs += parseInt(parts[0], 10) * 3600000;
  }
  return totalMs;
}

export function clamp(v, lo, hi) {
  return Math.min(Math.max(v, lo), hi);
}

// Slider invariant: 0 <= start < end <= durationMs.
// Used by app.js when syncing text fields ⇄ sliders.

/** Clamp start so it stays < end and >= 0. */
export function constrainStart(startMs, endMs, _durationMs) {
  return clamp(startMs, 0, Math.max(0, endMs - 1));
}

/** Clamp end so it stays > start and <= durationMs. */
export function constrainEnd(endMs, startMs, durationMs) {
  return clamp(endMs, startMs + 1, durationMs);
}
