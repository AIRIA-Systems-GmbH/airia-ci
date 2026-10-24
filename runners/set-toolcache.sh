#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Usage: set-toolcache.sh [runner-dir…]
# Run as root inside the container (add-runner.sh calls it for a new runner; the operator runs it
# once for the runners already registered). Gives each runner its own tool cache: creates
# /opt/hostedtoolcache/<runner dir name> and sets AGENT_TOOLSDIRECTORY to it in the runner's .env.
# Without arguments it takes every directory under /home/runner that holds a `.runner` file, so it
# also reaches runners that add-runner.sh did not create. Idempotent. It restarts nothing: a runner
# reads its .env at start, so the line "restart needed" names the runners to restart once idle.
# Why: runners/POLICY.md, "Shared tool cache" (#35).
set -euo pipefail
RUNNER_HOME="${RUNNER_HOME:-/home/runner}"
TOOLCACHE_ROOT="${TOOLCACHE_ROOT:-/opt/hostedtoolcache}"

if [ "$#" -eq 0 ]; then
  for marker in "$RUNNER_HOME"/*/.runner; do
    [ -f "$marker" ] || continue
    set -- "$@" "$(dirname "$marker")"
  done
fi

for dir in "$@"; do
  dir="${dir%/}"
  [ -d "$dir" ] || { echo "no such runner directory: $dir" >&2; exit 1; }
  cache="$TOOLCACHE_ROOT/$(basename "$dir")"
  mkdir -p "$cache"
  touch "$dir/.env"
  { grep -v '^AGENT_TOOLSDIRECTORY=' "$dir/.env" || [ $? -eq 1 ]; echo "AGENT_TOOLSDIRECTORY=$cache"; } > "$dir/.env.new"
  if cmp -s "$dir/.env" "$dir/.env.new"; then
    rm "$dir/.env.new"
    echo "unchanged: $dir -> $cache"
  else
    mv "$dir/.env.new" "$dir/.env"
    echo "restart needed: $dir -> $cache"
  fi
  if [ "$(id -u)" -eq 0 ]; then
    chown runner:runner "$dir/.env"
    chown -R runner:runner "$cache"
  fi
done
