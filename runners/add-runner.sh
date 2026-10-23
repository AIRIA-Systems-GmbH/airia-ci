#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Usage: add-runner.sh <owner/repo> <index> [labels]   (the token on stdin)
# Run as root inside the container; register.sh pushes it there and calls it.
# Installs one actions-runner instance into /home/runner/r-<slug>-<index>.
# Persistent (NOT --ephemeral): see decision note in the design doc.
set -euo pipefail
REPO="$1"; IDX="$2"; LABELS="${3:-thor,linux,x64}"
# Token is read from stdin so it never appears in argv / ps / shell history.
read -r TOKEN
SLUG="$(echo "$REPO" | tr '/' '-' | tr '[:upper:]' '[:lower:]')"
DIR="/home/runner/r-${SLUG}-${IDX}"

if [ -d "$DIR" ]; then echo "SKIP: $DIR exists"; exit 0; fi

mkdir -p "$DIR" && cd "$DIR"
if [ ! -f /root/actions-runner.tar.gz ]; then
  URL=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
        | jq -r '.assets[] | select(.name | test("actions-runner-linux-x64-[0-9.]+\\.tar\\.gz$")) | .browser_download_url' | head -1)
  echo "downloading $URL"
  curl -fsSL -o /root/actions-runner.tar.gz "$URL"
fi
tar xzf /root/actions-runner.tar.gz -C "$DIR"
chown -R runner:runner "$DIR"

sudo -u runner "$DIR/config.sh" \
  --url "https://github.com/${REPO}" \
  --token "$TOKEN" \
  --name "thor-${IDX}" \
  --labels "$LABELS" \
  --work "_work" \
  --unattended --replace

cd "$DIR" && ./svc.sh install runner
./svc.sh start
echo "INSTALLED $DIR"
