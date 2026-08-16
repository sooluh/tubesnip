#!/usr/bin/env -S deno run --allow-run --allow-read --allow-write --allow-env
/**
 * Enforce frontend coverage >= 95% (the frontend analogue of `fail_under = 95`
 * in pyproject).
 *
 * Flow: run `deno test --coverage=<dir>`, convert to lcov via `deno coverage
 * <dir> --lcov`, parse it, then fail (exit 1) if any frontend file is below the
 * threshold.
 *
 * Note: `DENO_DIR` is pointed inside the project (`.deno_cache`) because `deno
 * coverage` in 2.9.x reports nothing if the profile contains npm-cache files
 * outside the cwd (the `--include`/`--exclude` bug also panics).
 *
 * Run: deno run --allow-run --allow-read --allow-write --allow-env scripts/check-frontend-coverage.ts
 */
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const THRESHOLD = 95; // persen baris (line coverage), sama seperti backend
// Frontend files that must be measured (app.js DOM interactions + time.js pure logic).
const TARGET_FILES = ["app.js", "time.js"];
const PROFILE = join(ROOT, "cov_profile");
const DENO_DIR = join(ROOT, ".deno_cache");
const ENV = { ...Deno.env.toObject(), DENO_DIR };

function fail(msg: string): never {
  console.error(msg);
  Deno.exit(1);
}

// 1) Run tests + coverage. If tests fail, the coverage can't be trusted.
const r1 = new Deno.Command("deno", {
  args: [
    "test",
    "--allow-read",
    "--allow-env",
    `--coverage=${PROFILE}`,
    "tests/",
  ],
  cwd: ROOT,
  env: ENV,
  stdout: "inherit",
  stderr: "inherit",
}).outputSync();
if (!r1.success) fail("✗ deno test failed — coverage cannot be verified.");

// 2) Profile → lcov.
const r2 = new Deno.Command("deno", {
  args: ["coverage", PROFILE, "--lcov"],
  cwd: ROOT,
  env: ENV,
  stdout: "piped",
  stderr: "inherit",
}).outputSync();
if (!r2.success) fail("✗ failed to produce lcov from the coverage profile.");
const lcov = new TextDecoder().decode(r2.stdout);

// 3) Parse lcov (SF/LF/LH per file).
type FileCov = { path: string; found: number; hit: number };
const files: FileCov[] = [];
let cur: FileCov | null = null;
for (const line of lcov.split("\n")) {
  if (line.startsWith("SF:")) {
    cur = { path: line.slice(3), found: 0, hit: 0 };
    files.push(cur);
  } else if (cur && line.startsWith("LF:")) {
    cur.found = Number(line.slice(3));
  } else if (cur && line.startsWith("LH:")) {
    cur.hit = Number(line.slice(3));
  }
}

// 4) Clean up the profile (so it doesn't dirty the repo).
try {
  Deno.removeSync(PROFILE, { recursive: true });
} catch {
  /* ignore */
}

// 5) Check only the frontend files we target.
const targets = files.filter((f) =>
  TARGET_FILES.some((t) => f.path.replace(/\\/g, "/").endsWith(`/${t}`))
);

let failed = false;
console.log(`Frontend coverage (target >= ${THRESHOLD}%):`);
for (const f of targets) {
  const pct = f.found > 0 ? (f.hit / f.found) * 100 : 100;
  const ok = pct >= THRESHOLD;
  if (!ok) failed = true;
  console.log(`  ${ok ? "✓" : "✗"} ${f.path}: ${pct.toFixed(2)}% (${f.hit}/${f.found})`);
}
if (targets.length === 0) fail("✗ no frontend files were measured for coverage.");
if (failed) fail(`\n✗ Frontend coverage below ${THRESHOLD}% — add tests first.`);
console.log(`\n✓ All frontend files >= ${THRESHOLD}%.`);
