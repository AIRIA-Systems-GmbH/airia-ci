---
roadmap: Roadmap-airia-ci
updated: 2026-10-25
---
<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# airia-ci backlog

The order in which to do the work. Milestones are those of the Roadmap note `Roadmap-airia-ci`, kept in its order. Issues are GitHub issues on AIRIA-Systems-GmbH/airia-ci. Each Issue sits above the milestone it blocks. An Issue that breaks something already released goes at the very top.

2026-10-25 (cut from `main` at 52d59cc): two Issues closed since 2026-10-24. #53 (port lanes) and #35 (per-runner tool cache) closed on 2026-10-24 at 17:42 UTC, after the operator steps ran: all 8 active CT 144 runners have had their own port lane and tool cache since 16:12 UTC (#89, #90). Merged since 818feac: #84 (the re-cut of night triage #78), #85 and #86 (#27 Step 0: the CT 144 snapshot, `runners/measure.sh` and the open items), #87 (#53 port lanes 10000+1000·i), #88 (#31: `ANSIBLE_HOME` and `CI_JOB_PREFIX` in `job-isolation`), #89 and #90 (CT 144's lanes and caches live, and the burst sampler running), #91 (POLICY.md records the pool as two hosts, CT 144 and the Orlando VM, and CT 144 keeps its 12 cores). Every open Issue is labelled. Since 2026-10-24 the coordinators have neither run `gh pr update-branch` on this repository nor been allowed to (#79): a stale PR gets a local signed merge of `origin/main` or is re-cut from it.

## Breaks something released

1. #50: AIRIA-framework's "AIRIA Control Tests" job failed a two-daemon test with `ReadinessTimeoutError` (30 s) on the pool; the re-run was green. One occurrence, on 2026-10-18. The analysis on the Issue (2026-10-24) finds weak support for a host-wide stall. A port collision is plausible but not shown, and what exists cannot show the cause. The framework's half is AIRIA-framework#1036 (open, no activity since 2026-10-24). Since 2026-10-24 16:12 UTC every CT 144 runner has had its own port lane (#53, closed), so if the cause was a collision between runners, the lanes should stop it. No recurrence has been reported. The Issue can close once the framework's test uses the lane or #1036 names another cause. Not night work: the next step is upstream.
2. #27: load-induced timeouts on the orlando runners when about 15 PR runs start together (framework, SiteBuilder). Systemic: it fires on every burst. Andrea's ruling (2026-10-24): Step 0 first, then option D, more Portfolio-Dashboard runners. Option E (moving Portfolio-Dashboard into the org) is excluded. Step 0 merged in #85/#86. The burst sampler (`measure.sh`, unit `ci-measure`) runs on CT 144 until about 2026-10-26 03:42 UTC (#89). Next: read its log against options A and C, then option D. #91 records CT 144's 12 cores as decided. The fix is pool capacity, which is the operator's job.

## Roadmap

- M2 Owned: airia-agile process + BACKLOG.md + triage loop. Waits on airia-agile running headless; the night owns it in the interim.
- M3 The runner pool is code (NEXT).
3. #29: Thor runner pool (CT 144) as code. The pool-as-code part merged in #77 (53ad490), and #85–#91 have extended `runners/POLICY.md` since. Left open: PUSH versus PULL for applying the pool's config, which is Andrea's decision (PUSH is documented, PULL is an open proposal), and moving the originals out of `~/Development`, an operator step. Not night work.
4. #31: per-job state isolation (donor AIRIA-process #159). The repository parts are merged: private git config (#83), then `ANSIBLE_HOME`, `ANSIBLE_LOCAL_TEMP` and the Docker name prefix `CI_JOB_PREFIX` (#88); ports are the runner lanes (#53, closed). Left open: AIRIA-process adopting `job-isolation@main` and deleting its local step, which is the donor's change and is routed to the Process coordinator. Blocks M4's job-isolation action (one donor deleting its copy).
- M4 Harvest wave 1.
5. #30: Python 3.12–3.14 matrix as a reusable job. It blocks M4 and goes first in M4 (ruling 2026-10-13). It is the CI carrier of framework M3, whose Requirement is still being elicited. The Issue has no comment and has been unchanged since 2026-10-13.
6. #37: an unpinned `ty` release turned SiteBuilder's "Code quality" job red on files the PR never touched. airia-ci's half shipped in ci-v5 (#39): `python-toolchain` installs the gate tools at `pins.toml` and refuses `uv run --with ty`, and HOWTO.md says to pin. The Issue closes when SiteBuilder's job calls the gate, which is M5 adoption (`init.py protect` with Andrea's `gh` login). The standard does not accept `ty` (`pins.toml`: pyright or mypy-strict), so SiteBuilder also has to change its type checker. The Issue has had no activity since 2026-10-14. It blocks M5, so it sits before M5. Not night work.
- M5 No local workarounds. Cookiegusto's #24 (merged 2026-10-24) moves it to `@main` (not re-scanned); Portfolio-Dashboard is on `@main` by the 2026-10-24 scan. ParlAIment still calls none.

## Off-roadmap

Andrea's order (2026-10-21) of the open Issues: #54 (closed 2026-10-23), #37, #53 (closed 2026-10-24), #50, #35 (closed 2026-10-24), #27. #63 and #62 came first and are done. No open Issue is off-roadmap.

## Pick

None tonight. Every open Issue waits on an operator step, an upstream fix, an Andrea decision or another repository:
- #50 waits on AIRIA-framework#1036. Since 2026-10-24 16:12 UTC the port lanes are live, so a recurrence there is now the evidence to watch for.
- #27 waits on the burst sampler, which runs until about 2026-10-26 03:42 UTC; then option D, which is pool capacity and the operator's.
- #29 waits on Andrea's PUSH versus PULL decision.
- #31's remaining part is AIRIA-process adopting the action.
- #30's Requirement is still being elicited.
- #37's remaining part is SiteBuilder's adoption (M5).
