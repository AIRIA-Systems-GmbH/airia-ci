---
roadmap: Roadmap-airia-ci
updated: 2026-10-24
---
<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# airia-ci backlog

The order in which to do the work. Milestones are those of the Roadmap note `Roadmap-airia-ci`, kept in its order. Issues are GitHub issues on AIRIA-Systems-GmbH/airia-ci. Each Issue sits above the milestone it blocks. An Issue that breaks something already released goes at the very top.

2026-10-24 (re-cut from `main` at 818feac; it replaces the stale #78): three Issues closed since 2026-10-23, and the pool work merged. #54 closed on 2026-10-23 on Andrea's go (Q3 is "fail closed", which `main` already does), so it leaves the top. #73 (each review finding is its own PR comment, plus a deterministic check for unanswered findings) is closed: #74, #75, #80 and #81 are merged. #79 (should the main ruleset exempt web-flow merge commits?) is closed with a ruling: keep the rule. On this repository coordinators never run `gh pr update-branch`; a stale PR is brought up to date by a local signed merge of `origin/main`, or re-cut from `origin/main`. Merged since 2026-10-23: #77 (the pool scripts, README and `runners/POLICY.md`, for #29), #82 (a tool cache per runner, for #35) and #83 (the job-isolation action, for #31). `runners/` now exists on `main`, so the M3 check that it did not is met. Every open Issue is labelled.

## Breaks something released

1. #53: AIRIA-process's `daemon-shard (1, …)` job failed because its port-lane helper found no free run of 101 consecutive ports inside its 390-port lane (42620..43009), on PR #192 (2026-10-18), which did not touch that path. A consumer gate went red with no code cause. It is the second report of port collisions between jobs on one runner host, after #50. Ruled on #53, see the Issue. Nothing in airia-ci's YAML is implicated; the fix is the operator's, under `runners/POLICY.md`.
2. #50: AIRIA-framework's "AIRIA Control Tests" job failed a two-daemon test with `ReadinessTimeoutError` (30 s) on the pool; the re-run was green. One occurrence. The analysis on the Issue (2026-10-24) finds a host-wide stall weakly supported and a port collision plausible but not shown; the cause cannot be diagnosed from what exists, and the framework's side is filed as AIRIA-framework#1036. No pool change from this evidence; the port lane is #53. No recurrence reported by 2026-10-24.
3. #35: `uv` in the shared `/opt/hostedtoolcache` fails with `Text file busy` mid-job on thor (DevTools, one occurrence). The repository fix merged in #82 (56b49a6): each runner gets its own tool cache through `runners/set-toolcache.sh`. The Issue stays open until the operator steps in #82's description have run on CT 144 (`set-toolcache.sh` per runner, restart the idle runners, then `toolcache-python.sh` to seed every cache). Until then the live pool still shares one cache. Operator work, not night work.
4. #27: load-induced timeouts on the orlando runners when about 15 PR runs start together (framework, SiteBuilder). Systemic: it fires on every burst. Andrea's ruling (2026-10-24): Step 0 first, which measures CT 144 under load, then option D, more Portfolio-Dashboard runners. Option E, moving Portfolio-Dashboard into the org, is excluded: it stays on tumma72. Step 0 is in progress on branch `feat/27-step0-measure-ct144`. After Step 0, the Issue's analysis says: if the container is CPU- or IO-bound at 6+ jobs, C before A; if not, treat the timeouts as isolation faults (#53, #35). The fix is pool capacity, so it is the operator's.

## Roadmap

- M2 Owned: airia-agile process + BACKLOG.md + triage loop. Waits on airia-agile running headless; the night owns it in the interim.
- M3 The runner pool is code (NEXT).
5. #29: Thor runner pool (CT 144) as code. The pool-as-code part merged in #77 (53ad490): the scripts, README and `runners/POLICY.md` are under `runners/`. Left open: PUSH versus PULL for applying the pool's config is Andrea's decision (PUSH is documented, PULL is an open proposal), and moving the originals out of `~/Development` is the operator's step. Not night work.
6. #31: per-job git global config isolation (donor AIRIA-process #159). The git-config part merged in #83 (818feac): `.github/actions/job-isolation` points `GIT_CONFIG_GLOBAL` at a private file under `RUNNER_TEMP`. Left open: the other per-job state in `runners/POLICY.md` (ports, Docker names, `ANSIBLE_HOME`), which would join the action once decided (ports wait on #53), and AIRIA-process adopting the action and deleting its local step. Blocks M4's job-isolation action.
- M4 Harvest wave 1.
7. #30: Python 3.12–3.14 matrix as a reusable job. Blocks M4 and goes first in M4 (ruling 2026-10-13). It is the CI carrier of framework M3, whose Requirement is still being elicited. No comment on the Issue yet.
8. #37: an unpinned `ty` release turned SiteBuilder's "Code quality" job red on files the PR never touched. airia-ci's half shipped in ci-v5 (#39): `python-toolchain` installs the gate tools at `pins.toml` and refuses `uv run --with ty`, and HOWTO.md says to pin. It closes when SiteBuilder's job calls the gate, which is M5 adoption (`init.py protect` with Andrea's `gh` login), and the standard does not accept `ty` (`pins.toml`: pyright or mypy-strict), so SiteBuilder also changes type checker. As of 2026-10-19 SiteBuilder's "Code quality" job still ran `uv run --with ty` unpinned; the Issue has had no activity since 2026-10-14. The drift risk remains. It blocks M5, so it sits before M5. Not night work.
- M5 No local workarounds. CookieGusto and Portfolio-Dashboard are still on the frozen `@ci-v5` (ci-v5.4); moving them to `@main` is their own repositories' change.

## Off-roadmap

Andrea's order (2026-10-21) of the open Issues: #54 (closed 2026-10-23), #37, #53, #50, #35, #27. #63 and #62 came first and are done: #65 deleted `release.yml`, the newer-major step and `HARNESS_MAJOR` (06f0c8a), and #66 caps the automatic reviews at three per PR (4059de8); since #71 (closes #70, 5fd1f79) a head past the cap passes the check with a cap comment instead of failing it. No open Issue is off-roadmap.

## Pick

None tonight. Every open Issue is operator work, waits on an Andrea decision, or is in someone's hands:
- #53, #50 and #35 are operator work on CT 144 (the port lane; the tool-cache steps from #82).
- #27 is in progress: Step 0 is on `feat/27-step0-measure-ct144`.
- #29 waits on Andrea's PUSH versus PULL decision.
- #31's remaining state waits on #53 and on AIRIA-process adopting the action.
- #30's Requirement is still being elicited.
- #37's remaining part is SiteBuilder's adoption (M5).
