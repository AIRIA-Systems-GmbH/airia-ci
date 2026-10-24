#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Usage: set-portlane.sh <runner-dir> <index>
# Run as root inside the container (add-runner.sh calls it when it is given a lane index; the
# operator runs it once per runner already registered). Gives the runner its own port lane: sets
# CI_PORT_LANE_START and CI_PORT_LANE_END in the runner's .env to 10000+1000*index and
# 10999+1000*index. The index is explicit (0..11), never derived from directory order, and no two
# runners may hold the same one. Idempotent. It restarts nothing: a runner reads its .env at start,
# so the line "restart needed" names the runner to restart once idle.
# Why: runners/POLICY.md, "Ports" (#53). The lanes stay below 22000, clear of the listeners seen on
# the container and of the ephemeral range (32768-60999).
set -euo pipefail
LANE_BASE=10000
LANE_SIZE=1000
LANE_COUNT=12

[ "$#" -eq 2 ] || { echo "usage: set-portlane.sh <runner-dir> <index>" >&2; exit 2; }
dir="${1%/}"
index="$2"

[ -d "$dir" ] || { echo "no such runner directory: $dir" >&2; exit 1; }
case "$index" in
  '' | *[!0-9]*) echo "index must be a number from 0 to $((LANE_COUNT - 1)): $index" >&2; exit 2 ;;
esac
index=$((10#$index))
if [ "$index" -ge "$LANE_COUNT" ]; then
  echo "index $index is out of range: lanes 0 to $((LANE_COUNT - 1)) end below 22000" >&2
  exit 2
fi
start=$((LANE_BASE + LANE_SIZE * index))
end=$((start + LANE_SIZE - 1))

for other in "$(dirname "$dir")"/*/.env; do
  [ -f "$other" ] || continue
  [ "$(dirname "$other")" = "$dir" ] && continue
  if grep -q "^CI_PORT_LANE_START=$start\$" "$other"; then
    echo "lane $index ($start-$end) is already held by $(dirname "$other")" >&2
    exit 1
  fi
done

touch "$dir/.env"
{ grep -v -e '^CI_PORT_LANE_START=' -e '^CI_PORT_LANE_END=' "$dir/.env" || [ $? -eq 1 ]; echo "CI_PORT_LANE_START=$start"; echo "CI_PORT_LANE_END=$end"; } > "$dir/.env.new"
if cmp -s "$dir/.env" "$dir/.env.new"; then
  rm "$dir/.env.new"
  echo "unchanged: $dir -> $start-$end"
else
  mv "$dir/.env.new" "$dir/.env"
  echo "restart needed: $dir -> $start-$end"
fi
if [ "$(id -u)" -eq 0 ]; then
  chown runner:runner "$dir/.env"
fi
