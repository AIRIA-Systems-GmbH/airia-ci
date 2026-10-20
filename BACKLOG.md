---
roadmap: Roadmap-airia-ci
updated: 2026-10-21
---
<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# airia-ci backlog

The order in which to do the work. Milestones are those of the Roadmap note `Roadmap-airia-ci`, kept in its order. Issues are GitHub issues on AIRIA-Systems-GmbH/airia-ci. Each Issue sits above the milestone it blocks. An Issue that breaks something already released goes at the very top.

2026-10-21: no new Issue, comment or report since 2026-10-19, and none closed. The order is unchanged. Only #54's entry changed: its retry part (#55) is now on `main`.

## Breaks something released

1. #54: `reusable-claude-review.yml` fails without reviewing when it finds no gate results or cannot read them. On 2026-10-19 it hit three PRs in two consumers (AIRIA-framework#969, AIRIA-process#197 and #198), and each needed a manual re-run or an `@claude` summon. That is one extra CI round per merge. It is first because it is the only Issue in this section whose fault is in airia-ci's own YAML, so the fix is airia-ci's. The "Require the merge gate's results" step is the same in ci-v3, ci-v4 and ci-v5, so ci-v5.4 ships the defect too. The issue mixes three causes, and they need different handling. (a) A transient HTTP 5xx while `ci-results/action.yml` lists the run's jobs (framework#969, attempt 1). Nothing retries it, and a caller's `needs:` cannot prevent it. A bounded retry is a plain defect fix and settles none of the issue's open questions. That retry merged to `main` on 2026-10-20 (#55), but no tag carries it: ci-v5.4 still ships (a), and the fix reaches consumers only when Andrea dispatches `release.yml`. (The issue's comment still says #55 is not merged; that is stale.) (b) A review that started before its gates finished (process#198). Whether the reusable workflow waits for the gates or the caller orders the jobs with `needs:` is the issue's question 1. (c) A `--failed` re-run that sees no finished gate job (framework#969, attempt 2). Question 2 asks for a probe of the jobs API first. Question 3 (fail the check, or comment "could not review") is a behaviour choice. (b), (c) and question 3 are Andrea's. How the fix reaches the two callers (framework, process): both pin `@ci-v4`, and CLAUDE.md says older tags are frozen. A fix merged to `main` reaches them only once `ci-v5` moves (a `release.yml` dispatch, Andrea's) and they move their `claude-review` call to `@ci-v5` (M5 adoption). A ci-v4 backport would break the frozen-tag rule, so that is Andrea's call. It is not decided here. As of 2026-10-21 nothing left in #54 is night work: (b), (c), question 3 and question 4 are all Andrea's. It stays first because every released tag still carries the defect.
2. #27: load-induced timeouts on the orlando runners when about 15 PR runs start together. Consumer gates (framework, SiteBuilder) go red with no code cause. This is systemic: two consumers are hit, and it fires on every burst. Feeds the capacity/concurrency limits in M3's `runners/POLICY.md` (#29). The fix is pool capacity, so it is #29 / the operator's.
3. #35: `uv` in the shared `/opt/hostedtoolcache` fails with `Text file busy` mid-job on thor. A consumer gate (DevTools) went red with no code cause; one occurrence so far. Feeds the cache rule in M3's `runners/POLICY.md` (#29). The proposed fixes are host-side (per-runner `AGENT_TOOLSDIRECTORY`, or uv pre-seeded on the host) or a consumer-side workaround. One airia-ci-side route exists (checked 2026-10-19): setup-uv v10.3.0 reads `RUNNER_TOOL_CACHE` from the environment when it caches uv, so a step `env:` in `python-toolchain` could give each job its own uv. That route decides the cache rule that POLICY.md (#29) owns, and it would not reach DevTools, which calls `setup-uv@v7` directly. So it waits for #29. Since ci-v5, `python-toolchain` runs `setup-uv` (v10.3.0, which has no install-dir input), so every consumer that adopts the standard on thor reads the shared cache too.
4. #50: AIRIA-framework's "AIRIA Control Tests" job failed a two-daemon test with `ReadinessTimeoutError` (30 s) on the pool. The PR did not touch that path, the test passes locally, and the re-run was green. A consumer gate went red with no code cause; one occurrence so far. The filer suspects load during the night burst (the same class as #27) or a port collision between jobs that share a host's network. Either way it feeds M3's `runners/POLICY.md` (#29): capacity/concurrency, or per-job isolation of ports. Nothing in airia-ci's YAML is implicated, so the fix is #29 / the operator's. If it recurs outside a burst, the evidence points at port isolation rather than load. No recurrence by 2026-10-19: no new comment or report, and none of framework's failed runs since 10-16 shows a `ReadinessTimeoutError`.
5. #53: AIRIA-process's `daemon-shard (1, …)` job failed because its port-lane helper found no free run of 101 consecutive ports inside its 390-port lane (42620..43009). This was on PR #192 (2026-10-18), which did not touch that path, and the other shards on the same run passed. A consumer gate went red with no code cause. AIRIA-process has seen this "daemon port-range flake" since before its shard split, and works around it by re-running the failed job once. It is the second report pointing at port collisions between jobs on one runner host, after #50's hypothesis, and from a different consumer, so the port-isolation reading of #50 is now stronger. Per the Roadmap's open-question default, M3's `runners/POLICY.md` (#29) covers per-job port isolation as well as concurrency. Nothing in airia-ci's YAML is implicated, so the fix is #29 / the operator's. Not night work.

## Roadmap

- M2 Owned: airia-agile process + BACKLOG.md + triage loop. Waits on airia-agile running headless; the night owns it in the interim.
6. #29: Thor runner pool (CT 144) as code: `runners/` + `POLICY.md`. This is M3's carrier and blocks M3. Andrea is working it himself; not night work.
- M3 The runner pool is code (NEXT).
7. #30: Python 3.12–3.14 matrix as a reusable job. Blocks M4 and goes first in M4 (ruling 2026-10-13). It is the CI carrier of framework M3, whose Requirement is still being elicited.
8. #31: per-job git global config isolation (donor AIRIA-process #159). Blocks M4's job-isolation action. Needs the per-job state list in `runners/POLICY.md` (#29).
- M4 Harvest wave 1.
9. #37: an unpinned `ty` release turned SiteBuilder's "Code quality" job red on files the PR never touched. airia-ci's half shipped in ci-v5 (#39): `python-toolchain` installs the gate tools at `pins.toml` and refuses `uv run --with ty`, and HOWTO.md says to pin. It closes when SiteBuilder's job calls the gate, which is M5 adoption (`init.py protect` with Andrea's `gh` login), and the standard does not accept `ty` (`pins.toml`: pyright or mypy-strict), so SiteBuilder also changes type checker. ci-v5.4 changes nothing here. On 2026-10-19 SiteBuilder's review calls `@ci-v5`, but its "Code quality" job still runs `uv run --with ty` unpinned and not through the gate. SiteBuilder#59 merged green on 10-14 without a `# upstream:` marker, so the red cleared on SiteBuilder's side (route not traced), and the drift risk is still there. Not night work.
- M5 No local workarounds.

## Off-roadmap

None.
