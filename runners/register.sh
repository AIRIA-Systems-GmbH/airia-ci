#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Register N self-hosted runners in the pool's container for a repo.
# The registration token is piped over stdin and never printed or stored.
# Usage: register.sh <owner/repo> <count>
# Reads THOR_SSH and THOR_CT from $AIRIA_CI_RUNNERS_ENV (default
# ~/.config/airia-ci/runners.env); see runners/README.md.
set -euo pipefail
REPO="$1"; COUNT="${2:-2}"
CONF="${AIRIA_CI_RUNNERS_ENV:-$HOME/.config/airia-ci/runners.env}"
# shellcheck source=/dev/null
[ -r "$CONF" ] && . "$CONF"
THOR="${THOR_SSH:?set THOR_SSH (user@host of the Proxmox host) in $CONF}"
CT="${THOR_CT:?set THOR_CT (the runner container id) in $CONF}"

scp -q -o BatchMode=yes "$(dirname "$0")/add-runner.sh" "$(dirname "$0")/set-toolcache.sh" "$THOR:/tmp/"
ssh -o BatchMode=yes "$THOR" "pct push '${CT}' /tmp/add-runner.sh /root/add-runner.sh && pct push '${CT}' /tmp/set-toolcache.sh /root/set-toolcache.sh && pct exec '${CT}' -- chmod +x /root/add-runner.sh /root/set-toolcache.sh"

for i in $(seq 1 "$COUNT"); do
  echo "--- registering ${REPO} runner thor-${i}"
  # fresh token per runner (they are single-use); stays in a var, piped to stdin
  gh api -X POST "/repos/${REPO}/actions/runners/registration-token" --jq .token \
    | ssh -o BatchMode=yes "$THOR" "pct exec '${CT}' -- /root/add-runner.sh '${REPO}' '${i}'" \
    | { grep -v -i -E 'token|secret' || [ $? -eq 1 ]; }
done

echo "--- registered runners for ${REPO}:"
gh api "/repos/${REPO}/actions/runners" --jq '.runners[] | "\(.name)  \(.status)  busy=\(.busy)  labels=\([.labels[].name]|join(","))"'
