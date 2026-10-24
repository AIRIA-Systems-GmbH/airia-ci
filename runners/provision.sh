#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# The runner container — GitHub Actions self-hosted runner provisioning. Idempotent.
# Run as root inside the container.
set -euo pipefail

echo "=== base packages ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl git jq sudo gnupg lsb-release \
  build-essential pkg-config libssl-dev \
  python3 python3-venv unzip zstd rsync postgresql-client >/dev/null

echo "=== docker (needed for GH Actions services:/container: jobs) ==="
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin >/dev/null
fi
# unprivileged LXC: overlay2 on ext4 works with nesting=1; force it explicitly
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{
  "storage-driver": "overlay2",
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
systemctl enable --now docker
sleep 3
docker version --format '{{.Server.Version}}' || { echo "DOCKER FAILED"; exit 1; }

echo "=== gh (GitHub CLI — required by airia-ci review/CI steps and register.sh) ==="
# The claude-code-review workflow shells out to `gh pr view/diff/comment` and `gh api`;
# without gh every such step dies with `gh: command not found` (exit 127) and the job fails.
# Install from the official apt repo (update-safe) rather than dropping a loose binary in /bin.
if ! command -v gh >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] \
https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
  apt-get update -qq
  apt-get install -y -qq gh >/dev/null
fi
gh --version | head -1

echo "=== java (headless OpenJDK JRE — owlready2 runs the bundled HermiT reasoner via the java binary) ==="
# AIRIA-process's correctness gate calls owlready2.sync_reasoner_hermit(), which shells
# out to `java` to run HermiT.jar; without a JRE the gate fails with
# `Reasoner error: [Errno 2] No such file or directory: 'java'`. owlready2 only RUNS the
# jar (no javac, no GUI), so the minimal headless JRE is the right package — not a full
# JDK. Temporary: this whole dependency goes away when AIRIA-process migrates off
# owlready2/HermiT to SurrealDB.
if ! command -v java >/dev/null 2>&1; then
  apt-get install -y -qq default-jre-headless >/dev/null
fi
java -version

echo "=== runner user ==="
id runner >/dev/null 2>&1 || useradd -m -s /bin/bash runner
usermod -aG docker runner

echo "=== shared caches (persistent across ephemeral jobs) ==="
mkdir -p /var/cache/ci/sccache /var/cache/ci/cargo /var/cache/ci/uv /build
chown -R runner:runner /var/cache/ci /build

echo "=== rust toolchain (as runner) ==="
sudo -u runner bash -lc '
  set -e
  if [ ! -x "$HOME/.cargo/bin/rustc" ]; then
    curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
  fi
  . "$HOME/.cargo/env"
  rustup component add clippy rustfmt >/dev/null 2>&1 || true
  rustc --version
'

echo "=== sccache ==="
if [ ! -x /usr/local/bin/sccache ]; then
  SCV=$(curl -fsSL https://api.github.com/repos/mozilla/sccache/releases/latest | jq -r .tag_name)
  curl -fsSL "https://github.com/mozilla/sccache/releases/download/${SCV}/sccache-${SCV}-x86_64-unknown-linux-musl.tar.gz" \
    | tar xz -C /tmp
  install -m 0755 /tmp/sccache-${SCV}-x86_64-unknown-linux-musl/sccache /usr/local/bin/sccache
fi
sccache --version

echo "=== uv (python) ==="
sudo -u runner bash -lc '
  set -e
  [ -x "$HOME/.local/bin/uv" ] || curl -LsSf https://astral.sh/uv/install.sh | sh
  $HOME/.local/bin/uv --version
'

echo "=== runner env defaults ==="
cat > /etc/profile.d/ci-cache.sh <<'ENVS'
export SCCACHE_DIR=/var/cache/ci/sccache
export SCCACHE_CACHE_SIZE=40G
export CARGO_HOME=/var/cache/ci/cargo
export UV_CACHE_DIR=/var/cache/ci/uv
export RUSTC_WRAPPER=sccache
export CARGO_INCREMENTAL=0
export PATH="/var/cache/ci/cargo/bin:/home/runner/.local/bin:$PATH"
ENVS
chmod 0644 /etc/profile.d/ci-cache.sh

echo "=== PROVISION OK ==="

# ── Added 2026-08-26 after the AIRIA-framework migration surfaced these ──
# clang/libclang + cmake: surrealdb-librocksdb-sys runs a bindgen build script
# and build-essential alone is not enough.
apt-get install -y -qq --no-install-recommends clang libclang-dev llvm-dev cmake >/dev/null
# Locale: the runner's own .env sets LANG=en_US.UTF-8 but the locale must exist
# or every step emits setlocale warnings.
sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
apt-get install -y -qq locales >/dev/null && locale-gen >/dev/null && update-locale LANG=en_US.UTF-8
# Tool cache must be writable by the runner: setup-uv creates /opt/hostedtoolcache/uv.
mkdir -p /opt/hostedtoolcache && chown -R runner:runner /opt/hostedtoolcache
# NOTE: each runner's .env MUST carry its own AGENT_TOOLSDIRECTORY below /opt/hostedtoolcache
# (set-toolcache.sh writes it), never the shared directory itself: two runners writing the same
# uv binary fail a job with "Text file busy" (#35). Without it RUNNER_TOOL_CACHE defaults to
# _work/_tool and setup-python misses the seeded interpreter.
