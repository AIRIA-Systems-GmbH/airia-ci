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
# It also holds the project to the ruff floor and its `# noqa` baseline, with
# the gate's own check (toolchain.py, fetched from airia-ci's main, as this
# script is). Once,
# when adopting the standard (fix what ruff can, format, mark the rest
# `# noqa`, record the baseline):
#
#   bash <(gh api …/ci-local.sh -H 'Accept: application/vnd.github.raw') --onboard
#
# and `--write-baseline` to lower the baseline after removing suppressions.
#
# The gate's inputs, when your ci.yml sets them: AIRIA_COVERAGE_FLOOR (90),
# AIRIA_TYPE_CHECKER (pyright, or mypy-strict), AIRIA_PATHS (.),
# AIRIA_DURATIONS (25). AIRIA_XDIST_WORKERS (4) is what `-n auto` resolves to
# here; `auto` gives every CPU, as in CI. The `security` job (reuse,
# pip-audit, gitleaks, zizmor) is not run: HOWTO.md lists its commands.
set -euo pipefail

REF=main
mode="${1:-}"
case "$mode" in
  "" | --onboard | --write-baseline) ;;
  *) echo "ci-local: unknown option $mode (--onboard or --write-baseline)" >&2; exit 2 ;;
esac

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

# The gate's own ruff check, from airia-ci's main like this script, laid out as in the repository.
harness="$(mktemp -d)"
trap 'rm -rf "$harness"' EXIT
for f in .github/actions/python-toolchain/toolchain.py templates/python/pyproject.toml pins.toml; do
  mkdir -p "$harness/$(dirname "$f")"
  gh api "repos/AIRIA-Systems-GmbH/airia-ci/contents/$f?ref=$REF" -H 'Accept: application/vnd.github.raw' > "$harness/$f"
done
baseline=()
if [ "$mode" = --onboard ]; then
  uv run ruff check --fix "${paths[@]}" || true
  uv run ruff format "${paths[@]}"
  uv run ruff check --add-noqa "${paths[@]}"
fi
if [ -n "$mode" ]; then
  baseline=(--write-baseline)
fi

status=0
echo "== lint"
uv run ruff check "${paths[@]}" || status=1
uv run ruff format --check "${paths[@]}" || status=1
PATHS="${paths[*]}" uv run python3 "$harness/.github/actions/python-toolchain/toolchain.py" ruff ${baseline[@]+"${baseline[@]}"} \
  || status=1
if [ "$checker" = pyright ]; then
  uv run pyright --pythonpath "$PWD/.venv/bin/python" "${paths[@]}" || status=1
else
  uv run mypy --strict "${paths[@]}" || status=1
fi
if [ -f .importlinter ] || grep -q '^\[tool\.importlinter\]' pyproject.toml; then
  uv run lint-imports || status=1
fi

echo "== test"
report="$harness/coverage.json"
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
