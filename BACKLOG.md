---
roadmap: Roadmap-airia-ci
updated: 2026-10-22
---
<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# airia-ci backlog

The order in which to do the work. Milestones are those of the Roadmap note `Roadmap-airia-ci`, kept in its order. Issues are GitHub issues on AIRIA-Systems-GmbH/airia-ci. Each Issue sits above the milestone it blocks. An Issue that breaks something already released goes at the very top.

2026-10-22: no Issue was filed since 2026-10-21, and none of the eight Issues other than #54 has a new comment. Three PRs merged on 2026-10-21: #66 closed #62 (at most three automatic reviews per PR), #67 shipped #54's `--failed` re-run part, and #68 fixed a test with no Issue. Nine Issues are open. The runner Issues are now in Andrea's 2026-10-21 order (#53, #50, #35, #27); they used to be in filing order. Two checks still fail. M2: #54 has no labels, and #50 and #53 lack `runner`. M3: six `ct144-*.sh` are still loose under `~/Development` (four at the top level, two in `.ci-tmp/`), and `runners/POLICY.md` does not exist yet. From today (UTC) the ruff floor and the `# noqa` ratchet fail instead of warn (#49); that is the one released behaviour that changes tonight.

## Breaks something released

1. #54: `reusable-claude-review.yml` failed without reviewing when it found no gate results or could not read them. On 2026-10-19 that hit three PRs in two consumers (AIRIA-framework#969, AIRIA-process#197 and #198). Every part that fails a review on `main` is fixed. (a) A transient HTTP 5xx while `ci-results` lists the run's jobs is retried (#55, 2026-10-20). (c) A `gh run rerun --failed` re-run reads the carried-over gates: `ci-results` keeps each job's newest execution across attempts (#67, c96fbfe, 2026-10-21). (b) Early start (process#198): question 1 is answered, because both callers order the review with `needs:` on their gates, and a caller on `reusable-python-gate.yml` gets the ordering from the standard. Question 4 is moot under the `@main` rule. Only question 3 is left: when there are no results, should the review fail the check or post a "could not review, re-run" comment? That is Andrea's behaviour choice. The Roadmap counts every part as shipped, and closing the issue waits for Andrea's typed go. CookieGusto and Portfolio-Dashboard, still on the frozen `@ci-v5` (ci-v5.4), lack both fixes; moving them to `@main` is their own repositories' change (M5). It stays first because Andrea's 2026-10-21 order names it first. Nothing in it is night work.
2. #53: AIRIA-process's `daemon-shard (1, …)` job failed because its port-lane helper found no free run of 101 consecutive ports inside its 390-port lane (42620..43009). This was on PR #192 (2026-10-18), which did not touch that path, and the other shards on the same run passed. A consumer gate went red with no code cause. AIRIA-process has seen this "daemon port-range flake" since before its shard split, and works around it by re-running the failed job once. It is the second report pointing at port collisions between jobs on one runner host, after #50's hypothesis, and from a different consumer, so the port-isolation reading of #50 is now stronger. Per the Roadmap's open-question default, M3's `runners/POLICY.md` (#29) covers per-job port isolation as well as concurrency. Nothing in airia-ci's YAML is implicated, so the fix is #29 / the operator's. Not night work.
3. #50: AIRIA-framework's "AIRIA Control Tests" job failed a two-daemon test with `ReadinessTimeoutError` (30 s) on the pool. The PR did not touch that path, the test passes locally, and the re-run was green. A consumer gate went red with no code cause; one occurrence so far. The filer suspects load during the night burst (the same class as #27) or a port collision between jobs that share a host's network. Either way it feeds M3's `runners/POLICY.md` (#29): capacity/concurrency, or per-job isolation of ports. Nothing in airia-ci's YAML is implicated, so the fix is #29 / the operator's. If it recurs outside a burst, the evidence points at port isolation rather than load. No recurrence reported by 2026-10-22.
4. #35: `uv` in the shared `/opt/hostedtoolcache` fails with `Text file busy` mid-job on thor. A consumer gate (DevTools) went red with no code cause; one occurrence so far. Feeds the cache rule in M3's `runners/POLICY.md` (#29). The proposed fixes are host-side (per-runner `AGENT_TOOLSDIRECTORY`, or uv pre-seeded on the host) or a consumer-side workaround. One airia-ci-side route exists (checked 2026-10-19): setup-uv v10.3.0 reads `RUNNER_TOOL_CACHE` from the environment when it caches uv, so a step `env:` in `python-toolchain` could give each job its own uv. That route decides the cache rule that POLICY.md (#29) owns, and it would not reach DevTools, which calls `setup-uv@v7` directly. So it waits for #29. Since ci-v5, `python-toolchain` runs `setup-uv` (v10.3.0, which has no install-dir input), so every consumer that adopts the standard on thor reads the shared cache too.
5. #27: load-induced timeouts on the orlando runners when about 15 PR runs start together. Consumer gates (framework, SiteBuilder) go red with no code cause. This is systemic: two consumers are hit, and it fires on every burst. Feeds the capacity/concurrency limits in M3's `runners/POLICY.md` (#29). The fix is pool capacity, so it is #29 / the operator's.

## Roadmap

- M2 Owned: airia-agile process + BACKLOG.md + triage loop. Waits on airia-agile running headless; the night owns it in the interim.
6. #29: Thor runner pool (CT 144) as code: `runners/` + `POLICY.md`. This is M3's carrier and blocks M3. Andrea is working it himself; not night work.
- M3 The runner pool is code (NEXT).
7. #30: Python 3.12–3.14 matrix as a reusable job. Blocks M4 and goes first in M4 (ruling 2026-10-13). It is the CI carrier of framework M3, whose Requirement is still being elicited.
8. #31: per-job git global config isolation (donor AIRIA-process #159). Blocks M4's job-isolation action. Needs the per-job state list in `runners/POLICY.md` (#29).
- M4 Harvest wave 1.
9. #37: an unpinned `ty` release turned SiteBuilder's "Code quality" job red on files the PR never touched. airia-ci's half shipped in ci-v5 (#39): `python-toolchain` installs the gate tools at `pins.toml` and refuses `uv run --with ty`, and HOWTO.md says to pin. It closes when SiteBuilder's job calls the gate, which is M5 adoption (`init.py protect` with Andrea's `gh` login), and the standard does not accept `ty` (`pins.toml`: pyright or mypy-strict), so SiteBuilder also changes type checker. On 2026-10-19 SiteBuilder's review calls `@ci-v5`, but its "Code quality" job still runs `uv run --with ty` unpinned and not through the gate. SiteBuilder#59 merged green on 10-14 without a `# upstream:` marker, so the red cleared on SiteBuilder's side (route not traced), and the drift risk is still there. Andrea's order ranks it second, but it blocks M5, so it sits before M5. Not night work.
- M5 No local workarounds.

## Off-roadmap

Andrea's order (2026-10-21) of the open Issues: #54, #37, #53, #50, #35, #27. #63 and #62 came first and are done: #65 deleted `release.yml`, the newer-major step and `HARNESS_MAJOR` (06f0c8a), and #66 caps the automatic reviews at three per PR, after which a newer head fails the check with "escalate to a human" (4059de8). No open Issue is off-roadmap.
