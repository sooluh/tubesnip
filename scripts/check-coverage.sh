#!/usr/bin/env bash
# Combined check: backend coverage (pytest, fail_under=95 in pyproject) +
# frontend coverage (deno, threshold 95 in scripts/check-frontend-coverage.ts)
# + anti-slop lint (deno task lint, oxlint 0 errors). Exits non-zero if any fails.
# For CI / local verification.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== Backend (uv run pytest, fail_under=95) ==="
uv run pytest

echo
echo "=== Frontend (deno, threshold 95) ==="
deno run --allow-run --allow-read --allow-write --allow-env \
  scripts/check-frontend-coverage.ts

echo
echo "=== Anti-slop (deno task lint, oxlint 0 errors) ==="
deno task lint

echo
echo "✅ All checks passed: backend coverage >= 95%, frontend coverage >= 95%, anti-slop lint 0 errors."
