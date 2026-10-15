#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# The `lint` and `test` jobs of reusable-python-gate.yml, on this machine: the
# same commands, with the tools your uv.lock holds, which are the ones the gate
# runs when a tool is locked (templates/python/pyproject.toml locks them all).
# From the root of the repository's checkout, logged in with `gh`:
#
#   bash <(gh api repos/AIRIA-Systems-GmbH/airia-ci/contents/templates/python/ci-local.sh \
#     -H 'Accept: application/vnd.github.raw')
#
# The gate's inputs, when your ci.yml sets them: AIRIA_COVERAGE_FLOOR (90),
# AIRIA_TYPE_CHECKER (pyright, or mypy-strict), AIRIA_PATHS (.),
# AIRIA_DURATIONS (25). AIRIA_XDIST_WORKERS (4) is what `-n auto` resolves to
# here; `auto` gives every CPU, as in CI. The `security` job (reuse,
# pip-audit, gitleaks, zizmor) is not run: HOWTO.md lists its commands.
set -euo pipefail

floor="${AIRIA_COVERAGE_FLOOR:-90}"
checker="${AIRIA_TYPE_CHECKER:-pyright}"
durations="${AIRIA_DURATIONS:-25}"
workers="${AIRIA_XDIST_WORKERS:-4}"
read -ra paths <<< "${AIRIA_PATHS:-.}"
if [ "$workers" != auto ]; then
  export PYTEST_XDIST_AUTO_NUM_WORKERS="$workers"
fi

uv sync --locked

need=(ruff pytest)
case "$checker" in
  pyright) need+=(pyright) ;;
  mypy-strict) need+=(mypy) ;;
  *) echo "ci-local: no type checker $checker (pyright or mypy-strict)" >&2; exit 2 ;;
esac
for tool in "${need[@]}"; do
  if [ ! -x ".venv/bin/$tool" ]; then
    echo "ci-local: $tool is not in the project environment; add it to the dev group at its pin" \
      "(airia-ci's pins.toml): uv add --dev $tool==<pin>" >&2
    exit 2
  fi
done

status=0
echo "== lint"
uv run ruff check "${paths[@]}" || status=1
uv run ruff format --check "${paths[@]}" || status=1
if [ "$checker" = pyright ]; then
  uv run pyright --pythonpath "$PWD/.venv/bin/python" "${paths[@]}" || status=1
else
  uv run mypy --strict "${paths[@]}" || status=1
fi
if [ -f .importlinter ] || grep -q '^\[tool\.importlinter\]' pyproject.toml; then
  uv run lint-imports || status=1
fi

echo "== test"
report="$(mktemp)"
trap 'rm -f "$report"' EXIT
uv run pytest "--durations=$durations" --cov-report=term-missing "--cov-report=json:$report" \
  "--cov-fail-under=$floor" || status=1
if [ ! -s "$report" ]; then
  echo "ci-local: pytest wrote no coverage report, so the ${floor}% floor cannot be held: add" \
    "addopts = \"--cov=<package>\" under [tool.pytest.ini_options] and remove any --no-cov" >&2
  status=1
fi

if [ "$status" -eq 0 ]; then
  echo "ci-local: lint and test pass as the gate runs them"
fi
exit "$status"
