// Unit tests for time parsing & slider sync logic (pure, no DOM).
// Run: deno test
import { describe, it } from "@std/testing/bdd";
import { expect } from "@std/expect";
import {
  msToTime,
  timeToMs,
  clamp,
  constrainStart,
  constrainEnd,
} from "../src/tubesnip/static/time.js";

describe("msToTime", () => {
  it("basic HH:MM:SS.mmm format", () => {
    expect(msToTime(0)).toBe("00:00:00.000");
    expect(msToTime(1000)).toBe("00:00:01.000");
    expect(msToTime(61000)).toBe("00:01:01.000");
    expect(msToTime(3661000)).toBe("01:01:01.000");
    expect(msToTime(123456)).toBe("00:02:03.456");
    expect(msToTime(3599999)).toBe("00:59:59.999");
    expect(msToTime(86399999)).toBe("23:59:59.999");
  });

  it("negative values clamped to 0", () => {
    expect(msToTime(-500)).toBe("00:00:00.000");
  });

  it("milliseconds are rounded", () => {
    expect(msToTime(999.6)).toBe("00:00:01.000");
    expect(msToTime(1234.5)).toBe("00:00:01.235");
  });

  it("roundtrip msToTime → timeToMs stays the same", () => {
    for (const ms of [0, 1, 999, 1000, 19000, 123456, 3600000, 86399999]) {
      expect(timeToMs(msToTime(ms))).toBe(ms);
    }
  });
});

describe("timeToMs", () => {
  it("full HH:MM:SS.mmm format", () => {
    expect(timeToMs("00:00:00.000")).toBe(0);
    expect(timeToMs("00:00:01.000")).toBe(1000);
    expect(timeToMs("00:02:03.456")).toBe(123456);
    expect(timeToMs("1:02:03.456")).toBe(3723456);
    expect(timeToMs("12:34:56")).toBe(45296000);
  });

  it("short MM:SS and SS formats", () => {
    expect(timeToMs("02:03")).toBe(123000);
    expect(timeToMs("90")).toBe(90000);
    expect(timeToMs("00:00")).toBe(0);
  });

  it("millisecond fractions with 1–3 digits & decimal comma", () => {
    expect(timeToMs("00:00:01.5")).toBe(1500);
    expect(timeToMs("00:00:01.05")).toBe(1050);
    expect(timeToMs("00:00:01,5")).toBe(1500);
  });

  it("surrounding whitespace is ignored", () => {
    expect(timeToMs("  00:00:01.000  ")).toBe(1000);
  });

  it("invalid strings → null", () => {
    expect(timeToMs("")).toBeNull();
    expect(timeToMs("abc")).toBeNull();
    expect(timeToMs("1:2:3:4")).toBeNull();
    expect(timeToMs("00:00:00.1234")).toBeNull();
    expect(timeToMs("-00:00:01")).toBeNull();
    expect(timeToMs("00:00:01.")).toBeNull();
    expect(timeToMs("00:00:01.abc")).toBeNull();
    expect(timeToMs("ab:01")).toBeNull(); // minutes not a number
    expect(timeToMs("01:ab")).toBeNull(); // seconds not a number
  });
});

describe("clamp", () => {
  it("values inside the range stay, outside are clamped", () => {
    expect(clamp(5, 0, 10)).toBe(5);
    expect(clamp(-3, 0, 10)).toBe(0);
    expect(clamp(15, 0, 10)).toBe(10);
  });
});

describe("constrainStart / constrainEnd (slider sync logic)", () => {
  const DUR = 20000;

  it("start inside the range stays", () => {
    expect(constrainStart(5000, 10000, DUR)).toBe(5000);
  });

  it("start must be < end → forced to end-1", () => {
    expect(constrainStart(10000, 10000, DUR)).toBe(9999);
    expect(constrainStart(15000, 10000, DUR)).toBe(9999);
  });

  it("negative start → 0", () => {
    expect(constrainStart(-1, 10000, DUR)).toBe(0);
  });

  it("start when end = 0 → 0 (empty range)", () => {
    expect(constrainStart(500, 0, 0)).toBe(0);
  });

  it("end inside the range stays", () => {
    expect(constrainEnd(15000, 5000, DUR)).toBe(15000);
  });

  it("end must be > start → forced to start+1", () => {
    expect(constrainEnd(5000, 5000, DUR)).toBe(5001);
    expect(constrainEnd(3000, 5000, DUR)).toBe(5001);
  });

  it("end must be <= duration → forced to duration", () => {
    expect(constrainEnd(25000, 5000, DUR)).toBe(DUR);
  });

  it("start and end both at duration → start forced to duration-1", () => {
    expect(constrainEnd(DUR, DUR, DUR)).toBe(DUR);
    expect(constrainStart(DUR, DUR, DUR)).toBe(DUR - 1);
  });
});
