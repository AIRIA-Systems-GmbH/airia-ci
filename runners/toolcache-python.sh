#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Populate the Actions tool cache with Python 3.12 so actions/setup-python@v5 works.
#
# Why this is needed: setup-python downloads prebuilt interpreters from
# actions/python-versions, which publishes no build for Debian 13 (trixie) --
# "The version '3.12' with architecture 'x64' was not found for Debian 13".
# Rather than editing 12 jobs to drop setup-python (which would make the
# workflows non-portable back to hosted runners), we satisfy the action's local
# cache lookup. setup-python finds a hit and skips the download entirely.
#
# Layout it expects:  $RUNNER_TOOL_CACHE/Python/<x.y.z>/x64  + a sibling
#                     <x.y.z>/x64.complete marker file.
#
# Each runner has its own tool cache (set-toolcache.sh, #35), so the interpreter is copied into
# every cache named by AGENT_TOOLSDIRECTORY in a runner's .env under /home/runner. Run
# set-toolcache.sh first; a runner that still points at the shared /opt/hostedtoolcache is seeded
# there too.
set -euo pipefail
SERIES="${1:-3.12}"
RUNNER_HOME="${RUNNER_HOME:-/home/runner}"

export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/opt/uv-python}"
mkdir -p "$UV_PYTHON_INSTALL_DIR"

UV="${UV:-/home/runner/.local/bin/uv}"
"$UV" python install "$SERIES" >/dev/null 2>&1 || true
SRC="$("$UV" python find "$SERIES")"           # .../bin/python3
SRC_ROOT="$(dirname "$(dirname "$SRC")")"      # strip /bin/python3
FULL="$("$SRC" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"

CACHES="$(for envf in "$RUNNER_HOME"/*/.env; do
  if [ -f "$envf" ]; then sed -n 's/^AGENT_TOOLSDIRECTORY=//p' "$envf"; fi
done | sort -u)"
[ -n "$CACHES" ] || { echo "no runner .env sets AGENT_TOOLSDIRECTORY; run set-toolcache.sh first" >&2; exit 1; }

while IFS= read -r CACHE; do
  DEST="$CACHE/Python/$FULL/x64"
  if [ -e "$DEST/bin/python3" ]; then
    echo "already present: $DEST ($FULL)"
  else
    mkdir -p "$DEST"
    cp -a "$SRC_ROOT"/. "$DEST"/
    # setup-python puts <dest>/bin on PATH and expects a `python` alias too.
    ln -sf python3 "$DEST/bin/python" 2>/dev/null || true
    touch "$CACHE/Python/$FULL/x64.complete"
    echo "installed $FULL -> $DEST"
  fi
  chmod -R a+rX "$CACHE"
  "$DEST/bin/python3" --version
done <<< "$CACHES"
