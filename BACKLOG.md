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

2026-10-24: one Issue closed and one was filed since 2026-10-23. #54 was closed on 2026-10-23 on Andrea's go: Q3 is "fail closed", which `main` already does, so it leaves the top. #73 (each review finding is its own PR comment, plus a deterministic check for unanswered findings) was filed on 10-23. Slices 1a (#74) and 1b (#75) are merged, and its own text places it in M2. #29 now has an open PR, #77 (`feat/29-runner-pool-recut`, adds `runners/` and `runners/POLICY.md`), which replaces the closed #76. #50 and #53 now carry `runner`, so every open Issue is labelled and the M2 label gap is closed. No other open Issue has a new comment. The M3 check is still open: `origin/main` has no `runners/` until #77 merges.

## Breaks something released

1. #53: AIRIA-process's `daemon-shard (1, …)` job failed because its port-lane helper found no free run of 101 consecutive ports inside its 390-port lane (42620..43009). This was on PR #192 (2026-10-18), which did not touch that path, and the other shards on the same run passed. A consumer gate went red with no code cause. AIRIA-process has seen this "daemon port-range flake" since before its shard split, and works around it by re-running the failed job once. It is the second report pointing at port collisions between jobs on one runner host, after #50's hypothesis, and from a different consumer, so the port-isolation reading of #50 is now stronger. Per the Roadmap's open-question default, M3's `runners/POLICY.md` (#29) covers per-job port isolation as well as concurrency. Nothing in airia-ci's YAML is implicated, so the fix is #29 / the operator's. Not night work.
2. #50: AIRIA-framework's "AIRIA Control Tests" job failed a two-daemon test with `ReadinessTimeoutError` (30 s) on the pool. The PR did not touch that path, the test passes locally, and the re-run was green. A consumer gate went red with no code cause; one occurrence so far. The filer suspects load during the night burst (the same class as #27) or a port collision between jobs that share a host's network. Either way it feeds M3's `runners/POLICY.md` (#29): capacity/concurrency, or per-job isolation of ports. Nothing in airia-ci's YAML is implicated, so the fix is #29 / the operator's. If it recurs outside a burst, the evidence points at port isolation rather than load. No recurrence reported by 2026-10-24.
3. #35: `uv` in the shared `/opt/hostedtoolcache` fails with `Text file busy` mid-job on thor. A consumer gate (DevTools) went red with no code cause; one occurrence so far. Feeds the cache rule in M3's `runners/POLICY.md` (#29). The proposed fixes are host-side (per-runner `AGENT_TOOLSDIRECTORY`, or uv pre-seeded on the host) or a consumer-side workaround. One airia-ci-side route exists (checked 2026-10-19): setup-uv v10.3.0 reads `RUNNER_TOOL_CACHE` from the environment when it caches uv, so a step `env:` in `python-toolchain` could give each job its own uv. That route decides the cache rule that POLICY.md (#29) owns, and it would not reach DevTools, which calls `setup-uv@v7` directly. So it waits for #29. Since ci-v5, `python-toolchain` runs `setup-uv` (v10.3.0, which has no install-dir input), so every consumer that adopts the standard on thor reads the shared cache too.
4. #27: load-induced timeouts on the orlando runners when about 15 PR runs start together. Consumer gates (framework, SiteBuilder) go red with no code cause. This is systemic: two consumers are hit, and it fires on every burst. Feeds the capacity/concurrency limits in M3's `runners/POLICY.md` (#29). The fix is pool capacity, so it is #29 / the operator's.

## Roadmap

5. #73: each automatic review finding becomes its own PR comment, and a deterministic check lists the findings that have no reply. Without that check, the merge rule's "every bot finding has a reply" cannot be checked. The Issue's own Order places it in M2. Slices 1a (#74, the review-post action) and 1b (#75, the workflow wiring and the airia-findings block) merged on 2026-10-23. On `main` (checked 2026-10-24) README and HOWTO already describe the new review shape. Three parts are left: the no-reply check (AC5; `scripts/` has no findings script), the unanswered findings in the cap comment (AC6), and the docs for that check (the rest of AC7). One night cannot bring #73 to resolved, so a night slice would end as a `wip/` branch, not a PR. The coordinator is cutting its slices by dispatch, and a night slice would compete with the next one. Not night work until Andrea says the night may take a slice.
- M2 Owned: airia-agile process + BACKLOG.md + triage loop. Waits on airia-agile running headless; the night owns it in the interim.
6. #29: Thor runner pool (CT 144) as code: `runners/` + `POLICY.md`. This is M3's carrier and blocks M3. Open PR #77 (`feat/29-runner-pool-recut`, 2026-10-23, author tumma72) adds the scripts, README and POLICY.md under `runners/`, and replaces the closed #76. Not night work: it is the coordinator's open PR.
- M3 The runner pool is code (NEXT).
7. #30: Python 3.12–3.14 matrix as a reusable job. Blocks M4 and goes first in M4 (ruling 2026-10-13). It is the CI carrier of framework M3, whose Requirement is still being elicited.
8. #31: per-job git global config isolation (donor AIRIA-process #159). Blocks M4's job-isolation action. Needs the per-job state list in `runners/POLICY.md` (#29).
- M4 Harvest wave 1.
9. #37: an unpinned `ty` release turned SiteBuilder's "Code quality" job red on files the PR never touched. airia-ci's half shipped in ci-v5 (#39): `python-toolchain` installs the gate tools at `pins.toml` and refuses `uv run --with ty`, and HOWTO.md says to pin. It closes when SiteBuilder's job calls the gate, which is M5 adoption (`init.py protect` with Andrea's `gh` login), and the standard does not accept `ty` (`pins.toml`: pyright or mypy-strict), so SiteBuilder also changes type checker. On 2026-10-19 SiteBuilder's review calls `@ci-v5`, but its "Code quality" job still runs `uv run --with ty` unpinned and not through the gate. SiteBuilder#59 merged green on 10-14 without a `# upstream:` marker, so the red cleared on SiteBuilder's side (route not traced), and the drift risk is still there. Andrea's order ranks it second, but it blocks M5, so it sits before M5. Not night work.
- M5 No local workarounds.

## Off-roadmap

Andrea's order (2026-10-21) of the open Issues: #54 (closed 2026-10-23), #37, #53, #50, #35, #27. #63 and #62 came first and are done: #65 deleted `release.yml`, the newer-major step and `HARNESS_MAJOR` (06f0c8a), and #66 caps the automatic reviews at three per PR (4059de8); since #71 (closes #70, 5fd1f79) a head past the cap passes the check with a cap comment instead of failing it. No open Issue is off-roadmap.
