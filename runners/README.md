<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# runners/: the Thor runner pool as code

The scripts that provision and change the self-hosted runner pool (a Proxmox LXC container, "CT 144" in the Issues), and three read-only cost reports. The rules the pool runs under are in [POLICY.md](POLICY.md). Issue: [#29](https://github.com/AIRIA-Systems-GmbH/airia-ci/issues/29).

These are **operator scripts**. They run on the operator's Mac or inside the container, never in a consumer's workflow, so they are outside the consumer-shipped coverage gate (`tests/coverage_total.py` does not count `runners/`). They are still linted: `ruff check .` and `shellcheck -x -S warning` in `self-test.yml`'s `lint` job.

The repository is public. No hostname, IP address, e-mail address, SSH target or token belongs in a file here: host values live in the operator's env file, below.

## Scripts

| Script | Runs where | What it does |
| --- | --- | --- |
| `provision.sh` | inside the container, as root | Idempotent base setup: apt packages, Docker (overlay2, log rotation), `gh`, a headless JRE, the `runner` user, shared caches under `/var/cache/ci`, Rust, `sccache`, `uv`, `/etc/profile.d/ci-cache.sh`, clang/cmake, the `en_US.UTF-8` locale, `/opt/hostedtoolcache`. |
| `register.sh <owner/repo> [count]` | operator's Mac | Pushes `add-runner.sh` into the container and registers `count` runners (default 2) for the repository, named `thor-1` … `thor-<count>`. Lists the repository's runners at the end. |
| `add-runner.sh <owner/repo> <index> [labels]` | inside the container, as root (called by `register.sh`) | Installs one persistent (not `--ephemeral`) actions-runner into `/home/runner/r-<slug>-<index>` and starts its service. Skips if the directory exists. Default labels: `thor,linux,x64`. |
| `toolcache-python.sh [series]` | inside the container, as root | Fills `/opt/hostedtoolcache/Python/<x.y.z>/x64` (series default `3.12`) from `uv`, so `actions/setup-python` finds a cache hit on Debian 13. See the script's header for why. |
| `add-concurrency.py <workflow.yml>…` | operator's Mac | Inserts a top-level `concurrency:` block before `jobs:` in each workflow file, after checking the YAML parses and `jobs` is unchanged. Needs PyYAML. |
| `ci-usage-audit.py [days]` | operator's Mac | Read-only. Job minutes per repo/workflow/job over the last N days (default 14), for the repositories found in git remotes under `~/Development`. |
| `ci-impact-report.py [days_after] [days_before]` | operator's Mac | Read-only. Hosted vs self-hosted cost, failure rate and queue time around the cutover. Reads `CI_REPOS`. |

The container's scripts, in order, on a new container: `provision.sh`, then `register.sh` once per repository, then `toolcache-python.sh`. Use `gh` logged in as an org admin for `register.sh` (see below). `provision.sh` and `toolcache-python.sh` are not pushed by any script yet: the operator copies each into the container and runs it as root (the current model is described in [POLICY.md](POLICY.md#how-a-change-reaches-the-pool)).

## The env file

`register.sh` and `ci-impact-report.py` read host values from `~/.config/airia-ci/runners.env`, or from the file named by `$AIRIA_CI_RUNNERS_ENV`. One `KEY=value` per line. A variable already in the environment wins in `ci-impact-report.py`.

```
THOR_SSH=user@proxmox-host
THOR_CT=100
CI_REPOS=owner/repo-one owner/repo-two
```

- `THOR_SSH`: the SSH target of the Proxmox host (`register.sh`).
- `THOR_CT`: the container id on that host (`register.sh`).
- `CI_REPOS`: space-separated `owner/repo` list (`ci-impact-report.py`).

The values above are placeholders. The real file stays on the operator's Mac and is never committed.

## Registration

Registering a runner needs a registration token, and creating one needs an org-admin credential. Run `register.sh` with a `gh` login that has it. The script fetches a fresh single-use token per runner and pipes it over stdin through `ssh` into `add-runner.sh`; it never appears in argv, a file, or the output (`register.sh` also filters lines containing `token` or `secret`).

## The loose originals

The files this directory replaces are still loose in `~/Development` on the operator's Mac: `ct144-provision.sh`, `ct144-add-runner.sh`, `ct144-register.sh`, `ct144-toolcache-python.sh`, `add-concurrency.py`, `ci-usage-audit.py`, `ci-impact-report.py`. They are superseded by the files here. The operator removes them (the last item of Issue #29); this repository cannot.
