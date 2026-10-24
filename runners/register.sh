#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Register N self-hosted runners in the pool's container for a repo.
# The registration token is piped over stdin and never printed or stored.
# Usage: register.sh <owner/repo> <count> [first-lane]
# [first-lane] is the pool-wide port lane index (0..11) of runner 1; runner i gets first-lane+i-1.
# Without it no lane is set and the operator runs set-portlane.sh (runners/POLICY.md, "Ports").
# Reads THOR_SSH and THOR_CT from $AIRIA_CI_RUNNERS_ENV (default
# ~/.config/airia-ci/runners.env); see runners/README.md.
set -euo pipefail
REPO="$1"; COUNT="${2:-2}"; FIRST_LANE="${3:-}"
# Refuse a bad lane range before the first runner is registered (lanes are 0..11, #53).
if [ -n "$FIRST_LANE" ]; then
  case "$FIRST_LANE$COUNT" in
    *[!0-9]*) echo "first-lane and count must be numbers: $FIRST_LANE $COUNT" >&2; exit 2 ;;
  esac
  if [ $((10#$FIRST_LANE + 10#$COUNT - 1)) -gt 11 ]; then
    echo "lanes $FIRST_LANE to $((10#$FIRST_LANE + 10#$COUNT - 1)) go past lane 11" >&2
    exit 2
  fi
fi
CONF="${AIRIA_CI_RUNNERS_ENV:-$HOME/.config/airia-ci/runners.env}"
# shellcheck source=/dev/null
[ -r "$CONF" ] && . "$CONF"
THOR="${THOR_SSH:?set THOR_SSH (user@host of the Proxmox host) in $CONF}"
CT="${THOR_CT:?set THOR_CT (the runner container id) in $CONF}"

scp -q -o BatchMode=yes "$(dirname "$0")/add-runner.sh" "$(dirname "$0")/set-toolcache.sh" "$(dirname "$0")/set-portlane.sh" "$THOR:/tmp/"
ssh -o BatchMode=yes "$THOR" "pct push '${CT}' /tmp/add-runner.sh /root/add-runner.sh && pct push '${CT}' /tmp/set-toolcache.sh /root/set-toolcache.sh && pct push '${CT}' /tmp/set-portlane.sh /root/set-portlane.sh && pct exec '${CT}' -- chmod +x /root/add-runner.sh /root/set-toolcache.sh /root/set-portlane.sh"

for i in $(seq 1 "$COUNT"); do
  echo "--- registering ${REPO} runner thor-${i}"
  lane=""
  if [ -n "$FIRST_LANE" ]; then lane=$((10#$FIRST_LANE + i - 1)); fi
  # fresh token per runner (they are single-use); stays in a var, piped to stdin
  gh api -X POST "/repos/${REPO}/actions/runners/registration-token" --jq .token \
    | ssh -o BatchMode=yes "$THOR" "pct exec '${CT}' -- /root/add-runner.sh '${REPO}' '${i}' '' '${lane}'" \
    | { grep -v -i -E 'token|secret' || [ $? -eq 1 ]; }
done

echo "--- registered runners for ${REPO}:"
gh api "/repos/${REPO}/actions/runners" --jq '.runners[] | "\(.name)  \(.status)  busy=\(.busy)  labels=\([.labels[].name]|join(","))"'
