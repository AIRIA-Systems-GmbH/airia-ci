<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# Thor runner pool policy

What the pool is, how it is shared, and how it changes. Scripts: [README.md](README.md). Issue: [#29](https://github.com/AIRIA-Systems-GmbH/airia-ci/issues/29).

Read this first: **the pool is one container shared by every job from every consumer repository.** Persistent runners ([`add-runner.sh`](add-runner.sh) does not pass `--ephemeral`) execute jobs side by side on the same kernel, network namespace, Docker daemon, home directories and caches. Isolation between jobs is therefore a property the jobs have to keep, not one the pool enforces. The sections below say where that is written down, and where it is not.

Each rule is marked **in the scripts** (verifiable in this directory), **from an Issue** (evidence only), or **to define** (no script or Issue settles it; a decision for the pool's owners, to be announced as a `runner` Issue).

## Isolation

### Ports (#53, #50)

- **State today.** Nothing in `runners/` allocates ports. Jobs bind on the container's shared network namespace. Two collisions are on record: a daemon on a free port that did not become ready within 30 s ([#50](https://github.com/AIRIA-Systems-GmbH/airia-ci/issues/50)), and a job's port lane (`42620..43009`, 390 ports) holding no run of 101 consecutive free ports on a busy host ([#53](https://github.com/AIRIA-Systems-GmbH/airia-ci/issues/53)). Neither Issue establishes which other job held the ports: **to measure** (a job timeline of the host at the failure time).
- **Rule (in the scripts, [#53 ruling](https://github.com/AIRIA-Systems-GmbH/airia-ci/issues/53#issuecomment-6385250232) as amended).** The lane belongs to the runner, not the job. A runner runs one job at a time, so fixed lanes that never overlap between runners never overlap between concurrent jobs, with no allocator and no lock. Runner index *i* (0 to 11) gets ports `10000+1000·i … 10999+1000·i`, 1000 ports each, 10000–21999 in all. That span is below the ephemeral range (`ip_local_port_range` 32768–60999 on CT 144, `ip_local_reserved_ports` empty, measured 2026-10-24) and holds none of the listeners of the 2026-10-24 snapshot. [`set-portlane.sh`](set-portlane.sh) writes `CI_PORT_LANE_START` and `CI_PORT_LANE_END` into the runner's `.env`, as `set-toolcache.sh` does for `AGENT_TOOLSDIRECTORY`; `add-runner.sh` calls it when given a lane. A consumer's port helper reads the two variables and falls back to its own lane when they are absent, so a consumer that ignores them is not affected.
- **The index is explicit.** It is passed to the script, never derived from directory order: `/home/runner` holds 20 runner directories but 8 run, and an index by order would pass 11. The script refuses an index of 12 or more and a lane another runner's `.env` already holds. The index of each runner is **assigned by the operator**; this file lists none yet, because the 8 active runners are assigned in the operator step below.
- **Applying it to the live pool (operator, on CT 144).** Choose an index 0 to 11 for each of the 8 active runners, run `set-portlane.sh /home/runner/<dir> <index>` for each, and restart each runner it lists as "restart needed" while that runner is idle. Until then the variables are absent in every job. Record the assignment in this section.
- **Still open.** Whether 1000 ports hold the largest consumer run (101 consecutive ports in #53) is not in doubt for the width, but the two collisions above are not proven to be the ephemeral range: **to measure** after the lanes are live.

### Docker names and networks (in the actions; #31)

Docker is installed for `services:` and `container:` jobs ([`provision.sh`](provision.sh)), with one shared daemon and the default `daemon.json` (overlay2, json-file logs capped at 10 MB x 3). The scripts set no naming rule; the [`job-isolation`](../.github/actions/job-isolation/action.yml) action does.

- **Rule.** A job that creates containers, networks or volumes uses the `job-isolation` action, names every one of them with `$CI_JOB_PREFIX`, and removes them in the job's own cleanup. The action exports `CI_JOB_PREFIX` as `ci-<run id>-<attempt>-<job>-<hash of the runner name>`: lowercase `[a-z0-9_-]`, starting with a letter, so it is valid as a container, network or volume name and as a compose project name. Matrix legs share `GITHUB_JOB`; a runner runs one job at a time, so the runner-name hash is what separates concurrent legs. Fixed names (`postgres`, a compose project named after the repository) collide as soon as two jobs overlap.
- **What the action does not do.** It does not set `COMPOSE_PROJECT_NAME` (the consumer passes `-p "$CI_JOB_PREFIX"` or sets it) and it removes no containers.

### `ANSIBLE_HOME` and other home directories (in the actions; #35, #31)

All runner services run as the single `runner` user, so `~/.ansible`, `~/.cache`, `~/.cargo` and `~/.local` are shared by every job. `provision.sh` does not set `ANSIBLE_HOME`.

- **Rule.** A job that runs Ansible uses the `job-isolation` action first: it creates an empty `$RUNNER_TEMP/ansible` and `$RUNNER_TEMP/ansible/tmp` and exports `ANSIBLE_HOME` and `ANSIBLE_LOCAL_TEMP` to them through `$GITHUB_ENV`.
- **Other tools (to define).** #35 names "uv, `~/.ansible`, …" as the shared-home class of failure; `~/.cache`, `~/.cargo` and `~/.local` have no rule yet.

### Git global config (in the actions; #31)

Jobs share one `~/.gitconfig`, and two jobs writing `git config --global` at the same time race for `~/.gitconfig.lock` (`could not lock config file`; donor: AIRIA-process #159).

- **Rule.** A job that writes global git config uses the [`job-isolation`](../.github/actions/job-isolation/action.yml) action first: it creates an empty `$RUNNER_TEMP/gitconfig` and exports it as `GIT_CONFIG_GLOBAL` through `$GITHUB_ENV`, so every later step of the job reads and writes that file and never the shared one. The file starts empty, so a job does not inherit what an earlier job left in `~/.gitconfig`.
- **Opt-in.** The action has no inputs; a consumer adds `uses: AIRIA-Systems-GmbH/airia-ci/.github/actions/job-isolation@main` as a step before its first `git config --global`. The reusable workflows write no global git config, so they do not call it. It also carries the Ansible and Docker-name rules above; ports come from the runner's `.env`, not from the action.

### Shared caches (in the scripts)

[`provision.sh`](provision.sh) creates them, owned by `runner`, and writes `/etc/profile.d/ci-cache.sh`:

| Path | Variable | Limit |
| --- | --- | --- |
| `/var/cache/ci/sccache` | `SCCACHE_DIR` | `SCCACHE_CACHE_SIZE=40G` |
| `/var/cache/ci/cargo` | `CARGO_HOME` | none set |
| `/var/cache/ci/uv` | `UV_CACHE_DIR` | none set |
| `/build` | | none set |

`CARGO_INCREMENTAL=0` and `RUSTC_WRAPPER=sccache` are set for the same shell profile. Only sccache has a size limit; the free disk of the container and the growth of the other three are **to measure**. These caches are content-addressed or lock-protected by their tools, which is why they may be shared; anything else that writes into them is not safe to share.

### Tool cache: one per runner (in the scripts; #35)

`provision.sh` creates `/opt/hostedtoolcache`, owned by `runner`. Under it each runner has its own directory, named after the runner's directory (`/opt/hostedtoolcache/r-<slug>-<index>`), and the runner's `.env` carries `AGENT_TOOLSDIRECTORY=<that directory>`. [`set-toolcache.sh`](set-toolcache.sh) sets both; `add-runner.sh` calls it for a new runner. `toolcache-python.sh` copies the interpreter into every runner's cache. `setup-uv` and `setup-python` read and write only their own runner's cache.

- **Rule.** Two runners never share a tool cache. A job runs on one runner at a time, so within a cache the writer of `uv/<ver>/x86_64/uv` and its executor are the same job's steps, never two jobs. The shared `/opt/hostedtoolcache/<tool>/…` layout is retired; a runner whose `.env` still points at `/opt/hostedtoolcache` itself is not covered.
- **Fault this answers ([#35](https://github.com/AIRIA-Systems-GmbH/airia-ci/issues/35)).** A job executed `/opt/hostedtoolcache/uv/0.13.0/x86_64/uv` and failed with `Text file busy`: another process on the host had that file open for writing. Which job wrote it was not recorded. The likeliest writer is a second job's `setup-uv` copying the same version into the shared directory at the same time (the pull request that added this section explains why); a per-runner cache removes every cross-job writer of that path, whichever it was.
- **Cost.** Each runner downloads and keeps its own copy of each tool (uv, Python from `setup-python`), so disk grows with the runner count and the first job per runner and version is slower. Free disk is **to measure**.
- **Not covered.** Within one runner a persistent cache still holds whatever the last job left; a job that kills `setup-uv` mid-copy leaves a partial directory without the `.complete` marker, which the next `setup-uv` run overwrites. `/home/runner/.local/bin/uv` (installed by `provision.sh`) and `UV_CACHE_DIR` are shared and are not changed here: the first is written only by `provision.sh`, the second is the lock-protected content-addressed cache listed above.
- **Applying it to the live pool.** Run `set-toolcache.sh` in the container (it changes `.env` files and restarts nothing), restart each runner it lists as "restart needed" while that runner is idle, then run `toolcache-python.sh`. Steps are in the pull request that added this.

## Capacity and concurrency (#27)

What the scripts and Issues show, and nothing more:

- **Runners per repository.** `register.sh <repo> [count]` registers `count` runners per repository, default 2 (`thor-1`, `thor-2`, …), one service each, persistent. Each runner executes one job at a time. The pool's concurrent job capacity is the sum over registered repositories; the live total is read with `gh api /repos/<repo>/actions/runners` per repository (`register.sh` prints it) and is not recorded here.
- **Names in the Issues differ from the script's.** #27 and #35 report runners named `org-orlando-2`, `org-orlando-4` and `thor-org-5`, which `add-runner.sh` does not generate (`thor-<index>`). The pool therefore has runners registered some other way (an organisation-level registration). Which scripts or steps created them is **to confirm**; until then this directory does not describe the whole pool.
- **Container resources.** No script here sets them. Measured on 2026-10-24 (read-only snapshot, [#27](https://github.com/AIRIA-Systems-GmbH/airia-ci/issues/27) Step 0), at an idle moment, not a burst:
  - **CT 144 limits (the Proxmox host's config):** 12 cores (`cpulimit` 12; the container's own description says "capped at 8/16", which the config no longer matches), 16384 MB memory, 8192 MB swap, 200 GB root disk (197G visible, 118G used, 63%).
  - **State at the snapshot:** `nproc` 12; memory 3139 MB used, 13598 MB buff/cache, 13244 MB available, 471 MB swap used; load average 6.38 / 2.36 / 0.92 (a short spike); pressure `some avg10` cpu 0.28, io 0.17, memory 0.00 (io avg60 2.66, avg300 1.52); 8 distinct listening TCP ports (22, 25, 4226, 6556, 24577, 31720, 31893, 34857; `ss -ltn` shows 10 sockets because 25 and 34857 listen on IPv4 and IPv6), the unit `measure.sh` counts as `listen=`, so the burst log compares like with like.
  - **Runners in CT 144:** 8 running services: `thor-org-1` to `thor-org-5` (AIRIA-Systems-GmbH, organisation level), and `thor-1` of CryptoStudio, Personal-Trainer and Portfolio-Dashboard. `/home/runner` holds 20 runner directories; the other 12 (airia-framework 1-6, airia-process 1-2, parlaiment 1, cryptostudio 1-4 under the `r-<slug>-<index>` names) have no running `actions.runner*` service. So `thor-org-*` share CT 144 with the three repository runners (up to 8 concurrent jobs).
  - **Not in CT 144:** no `orlando-org-*` service. Where `orlando-org-1..4` run is **unknown** (not on the Proxmox host that holds CT 144 either; see open item 2).
  - **Load during a burst: to measure** with [`measure.sh`](measure.sh), started before the next burst. The idle numbers above say nothing about saturation.
  - **Open items (CT 144, as of 2026-10-24):**
    1. **Cores: 8/16 or 12?** CT 144's own description reads "Cores capped at 8/16 so CI cannot starve production.", but its Proxmox config has 12 cores (`cpulimit` 12). So the live limit is higher than the guard the description states for production. Whether 8 or 12 is intended is the operator's call. Until then, this file uses the measured value, 12.
    2. **Where `orlando-org-1..4` run is unknown.** They are not in CT 144 (the 2026-10-24 snapshot found `thor-org-1..5` and three repository `thor-1` runners there), and they are not on the Proxmox host that holds CT 144 either: no other container there (CT 135, 139-143, 145) has a runner directory or an `actions.runner` unit (read-only lookup, 2026-10-24). Which host they run on is the operator's to state.
    3. **The burst measurement (#27, left to do)** waits on `measure.sh` being started in CT 144, which is the operator's step.
- **A burst of about 15 PR runs.** On 2026-10-13, four night runs' PRs plus four backlog PRs opened within one minute started roughly 15 workflow runs together. Two jobs on two runners timed out (a 124 exit from the job's timeout wrapper; two `pytest-timeout` 300 s setup errors) in PRs that changed only `BACKLOG.md` ([#27](https://github.com/AIRIA-Systems-GmbH/airia-ci/issues/27)). #27's hypothesis, not a measurement: the burst saturates CPU or IO. The pool has no queue limit, no admission control and no per-consumer share; nothing in this directory limits how many jobs run at once beyond the number of runners.
- **Superseding in-flight runs.** [`add-concurrency.py`](add-concurrency.py) adds a `concurrency:` group per workflow and ref that cancels the previous run on a PR branch (`main` is exempt). It reduces the burst a push causes; it does not limit a burst of different PRs.
- **To measure before any number is set:** CPU and IO load during a 15-run burst; the largest runner count the container carries without timeouts; the queue time per runner count (`ci-impact-report.py` prints median, p90 and max queue time). No runner count or concurrency cap is stated here until those exist.

## Changes to the pool are announced first

A change to the pool, such as a new interpreter in the toolcache, a new runner, or a change to concurrency or caches, is announced as a `runner` Issue **before** it lands. On 2026-10-09 CPython 3.14.8 reached the pool unannounced and AIRIA-process went red the same day. The rule is in [CONTRIBUTING.md](../CONTRIBUTING.md#runner-pool-changes); this policy links it and adds nothing to it. A change to a script here is a PR like any other, and the Issue is linked in its description.

## How a change reaches the pool

### PUSH: the current model

The operator's Mac pushes. `register.sh` copies `add-runner.sh` to the Proxmox host with `scp`, then `ssh` runs `pct push` and `pct exec` to put it into the container and run it. The other scripts reach the container the same way, by the operator's hand (a VM would use `qm guest exec`). Host values come from `~/.config/airia-ci/runners.env` (or `$AIRIA_CI_RUNNERS_ENV`): `THOR_SSH`, `THOR_CT`, `CI_REPOS`. Merging a change here changes nothing on the pool until the operator runs the script.

- Strengths: nothing on the pool reaches out to this repository; the operator sees every change land; the registration credential never leaves the operator's `gh` login.
- Weaknesses: the pool's state is what the last operator run left; a second operator machine needs the same env file and access; there is no check that the pool matches `main`.

### PULL: an open proposal, not decided (for Andrea)

Each runner host would fetch airia-ci over HTTPS on a systemd timer and run the idempotent scripts, with host values in `/etc/airia-ci/runner.env` on the host. Changes would reach the pool only when a pool tag moves. Registration would stay a manual operator step, because it needs an org-admin credential. **Nothing of this is implemented.**

- For: the pool converges on a reviewed revision without an operator in the loop; the host values live on the host; works for more than one host.
- Against: the host needs outbound access to GitHub and a timer to maintain; the scripts must be safe to re-run on a live pool, with jobs in flight (`provision.sh` restarts nothing, but `apt-get` and `locale-gen` run under the jobs); an unannounced move of the pool tag is the same failure as the 2026-10-09 toolcache change, so the tag must move only after the `runner` Issue.
- **A pool tag is a new decision.** This repository's CLAUDE.md forbids cutting new `ci-vN` tags (they are frozen history), so a pool tag would be a different kind of tag, or a pinned commit file, and either needs Andrea's decision first.

Until decided, PUSH stays the model and this section is the whole record.
