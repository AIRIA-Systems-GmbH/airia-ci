#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Record a jobs-API fixture from a real, PUBLIC workflow run. This repository
# is public: never record a run of a private consumer repository here.
#
#   ./record-fixture.sh actions/checkout 36514614158
#
# Logs are cut to their last 40 lines (the failed step is at the end).
set -euo pipefail
repo="$1"; run="$2"
here="$(cd "$(dirname "$0")" && pwd)"
out="$here/fixture"
rm -rf "$out"; mkdir -p "$out/logs"
[ "$(gh api "repos/$repo" --jq .private)" = false ] || { echo "refusing: $repo is not public" >&2; exit 1; }
gh api "repos/$repo/actions/runs/$run/jobs?filter=latest" \
  --jq '{total_count, jobs: [.jobs[] | {id, name, status, conclusion, steps: [.steps[]? | {name, conclusion}]}]}' \
  > "$out/jobs.json"
gh api "repos/$repo/actions/runs/$run" --jq '{head_sha, html_url}' > "$out/run.json"
for id in $(jq -r '.jobs[] | select(.status == "completed") | .id' "$out/jobs.json"); do
  gh api --allow-escape-sequences "repos/$repo/actions/jobs/$id/logs" | tail -n 40 > "$out/logs/$id.log"
done
printf 'repo=%s\nrun=%s\n' "$repo" "$run" > "$out/SOURCE"
echo "recorded $(jq '.jobs | length' "$out/jobs.json") jobs of $repo run $run into $out"
