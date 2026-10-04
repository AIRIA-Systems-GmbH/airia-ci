#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Proves the reviewer can read the gate results, and that the old layouts
# cannot, by running the SHIPPED code (extracted from the YAML) and a real
# headless Claude Code under the composed settings.
#
#   ./run.sh                 # offline: the recorded public fixture
#   LIVE=actions/checkout:36514614158 ./run.sh   # a real run, via the real gh
#   SKIP_CLAUDE=1 ./run.sh   # the deterministic checks only
#
# Claude is run with --setting-sources project from an empty workspace, so the
# operator's ~/.claude settings (allow rules, defaultMode) cannot leak in; only
# the composed settings file applies, and nothing that would prompt is approved
# (--permission-prompts none), as in a headless Action.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$here/../.." && pwd)"
lib="$here/lib.py"
fail=0
check() { # check <description> <command...>
  local what="$1"; shift
  if "$@" >/dev/null; then echo "PASS  $what"; else echo "FAIL  $what"; fail=1; fi
}

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
RT="$tmp/runner-temp"; WS="$tmp/workspace"; mkdir -p "$RT" "$WS"
git -C "$WS" init -q && git -C "$WS" -c commit.gpgsign=false commit -q --allow-empty -m init

# ── 1. settings, from the shipped claude-settings action ──────────────────
compose() { # compose <action.yml> <out-name>
  local script; script="$(python3 "$lib" "$1" "Compose the Claude settings")"
  ( cd "$tmp" && RUNNER_TEMP="$RT" PROFILE=review EXTRA_ALLOW="" EXTRA_DENY="" \
      GITHUB_OUTPUT="$tmp/out.$2" bash -euo pipefail -c "$script" >/dev/null )
  mv "$RT/claude-settings-review.json" "$tmp/$2.json"
}
compose "$repo_root/.github/actions/claude-settings/action.yml" settings-new
git -C "$repo_root" show 666a54a:.github/actions/claude-settings/action.yml > "$tmp/old-settings-action.yml"
compose "$tmp/old-settings-action.yml" settings-old
check "new settings grant no directory outside the checkout" \
  jq -e '.permissions | has("additionalDirectories") | not' "$tmp/settings-new.json"
check "new settings no longer allow 'gh pr checks'" \
  jq -e '[.permissions.allow[] | select(startswith("Bash(gh pr checks"))] | length == 0' "$tmp/settings-new.json"
check "old settings (666a54a, before #9) DID grant \$RUNNER_TEMP/ci-results" \
  jq -e --arg d "$RT/ci-results" '.permissions.additionalDirectories == [$d]' "$tmp/settings-old.json"

# ── 2. git exclude, from the shipped workflow steps ───────────────────────
for wf in reusable-claude-review reusable-claude-respond; do
  script="$(python3 "$lib" "$repo_root/.github/workflows/$wf.yml" "Keep ci-results/ out of git")"
  ( cd "$WS" && bash -euo pipefail -c "$script" )
done

# ── 3. collect, with the shipped ci-results action ────────────────────────
collect="$(python3 "$lib" "$repo_root/.github/actions/ci-results/action.yml" "Collect the run's job results")"
if [ -n "${LIVE:-}" ]; then
  gh_repo="${LIVE%%:*}"; run_id="${LIVE##*:}"; path_prefix=""
  head_sha="$(gh api "repos/$gh_repo/actions/runs/$run_id" --jq .head_sha)"
  echo "source: LIVE run $gh_repo#$run_id via the real gh"
else
  # shellcheck source=validation/ci-results-staging/fixture/SOURCE
  . "$here/fixture/SOURCE"; gh_repo="$repo"; run_id="$run"
  mkdir -p "$tmp/bin"; cp "$here/fake-gh" "$tmp/bin/gh"; chmod +x "$tmp/bin/gh"
  path_prefix="$tmp/bin:"; export FAKE_GH_FIXTURE="$here/fixture"
  head_sha="$(jq -r .head_sha "$here/fixture/run.json")"
  echo "source: recorded fixture of $gh_repo#$run_id (public)"
fi
token="${GH_TOKEN:-$(gh auth token)}"
( cd "$WS" && PATH="$path_prefix$PATH" GH_TOKEN="$token" RUN_ID="$run_id" \
    OUT="$WS/ci-results" GITHUB_REPOSITORY="$gh_repo" GITHUB_SERVER_URL=https://github.com \
    GITHUB_OUTPUT="$tmp/collect.out" GITHUB_STEP_SUMMARY="$tmp/summary.md" \
    bash -euo pipefail -c "$collect" )
jobs_md="$WS/ci-results/jobs.md"
expected_jobs="$(gh_jobs() { if [ -n "${LIVE:-}" ]; then gh api --paginate "repos/$gh_repo/actions/runs/$run_id/jobs?filter=latest" --jq '.jobs[] | select(.status=="completed") | .id'; else jq -r '.jobs[] | select(.status=="completed") | .id' "$here/fixture/jobs.json"; fi; }; gh_jobs | wc -l | tr -d ' ')"
echo "--- jobs.md as collected ---"; cat "$jobs_md"; echo "---"
check "collected=$expected_jobs reported in GITHUB_OUTPUT" grep -qx "collected=$expected_jobs" "$tmp/collect.out"
check "jobs.md names the run's head commit" grep -q "Commit: $head_sha" "$jobs_md"
check "jobs.md has one row per completed job" \
  test "$(grep -c '| logs/' "$jobs_md")" -eq "$expected_jobs"
check "every log was fetched (no 'log unavailable')" bash -c "! grep -l 'log unavailable' '$WS'/ci-results/logs/*.log"
check "ci-results/ is invisible to git status" test -z "$(git -C "$WS" status --porcelain)"
( cd "$WS" && git add -A )
check "git add -A stages nothing from ci-results/" test -z "$(git -C "$WS" diff --cached --name-only)"

# the OLD layout, for the negative cases
cp -R "$WS/ci-results" "$RT/ci-results"

[ -n "${SKIP_CLAUDE:-}" ] && { echo "SKIP_CLAUDE set: headless-reader cases not run"; exit "$fail"; }

# ── 4. a real headless Claude Code reads (or fails to read) the figures ────
claude_version="$(command claude --version | head -1)"
echo "claude: $claude_version (the Thor pool runs the claude-cli action's cached build)"
ask() { # ask <case> <settings> <command-text>
  local out="$tmp/claude-$1.json"
  ( cd "$WS" && RUNNER_TEMP="$RT" command claude -p \
      "Use the Bash tool exactly once to run this exact command, character for character, and no other tool: $3
Then reply with the line of its output that starts with 'Commit:', verbatim. If the command is refused or fails, do not try anything else; reply with the single word REFUSED." \
      --settings "$2" --setting-sources project --permission-mode manual --permission-prompts none \
      --strict-mcp-config --no-session-persistence --no-chrome \
      --model haiku --max-turns 4 --output-format json < /dev/null > "$out" ) || true
  local denials result
  denials="$(jq '.permission_denials | length' "$out")"
  result="$(jq -r '.result // ""' "$out" | tr '\n' ' ')"
  echo "  [$1] command: $3"
  echo "  [$1] permission_denials=$denials result=${result:0:160}"
  jq -c '.permission_denials[]? | {tool_name, command: .tool_input.command}' "$out" | sed "s/^/  [$1] denied: /"
  printf '%s\n' "$denials" > "$tmp/$1.denials"; printf '%s' "$result" > "$tmp/$1.result"
}
read_ok() { [ "$(cat "$tmp/$1.denials")" -eq 0 ] && grep -q "$head_sha" "$tmp/$1.result"; }
refused() { [ "$(cat "$tmp/$1.denials")" -ge 1 ] && ! grep -q "$head_sha" "$tmp/$1.result"; }

ask A "$tmp/settings-new.json" 'cat ci-results/jobs.md'
check "A  NEW: relative ci-results/jobs.md, no grant -> figures read" read_ok A
ask B "$tmp/settings-old.json" 'cat $RUNNER_TEMP/ci-results/jobs.md'
check "B  OLD var form (\$RUNNER_TEMP), even WITH the grant -> refused" refused B
ask C "$tmp/settings-new.json" "cat $RT/ci-results/jobs.md"
check "C  absolute path outside the checkout, no grant -> refused" refused C
ask D "$tmp/settings-old.json" "cat $RT/ci-results/jobs.md"
check "D  control: same absolute path WITH the old grant -> read (the grant was load-bearing)" read_ok D

exit "$fail"
