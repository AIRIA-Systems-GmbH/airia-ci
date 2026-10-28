---
roadmap: Roadmap-airia-ci
updated: 2026-10-28
---
<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# airia-ci backlog

The order in which to do the work. Milestones are those of the Roadmap note `Roadmap-airia-ci`, kept in its order. Issues are GitHub issues on AIRIA-Systems-GmbH/airia-ci. Each Issue sits above the milestone it blocks. An Issue that breaks something already released goes at the very top.

2026-10-28 (cut from `main` at adb1a6a): #31 closed on 2026-10-28, #94 (the consumer bump job) is new under M4, and #27's option D is closed. Merged since 52d59cc: #92 (the backlog at 52d59cc), #93 (dependabot, setup-uv) and #95 (#94 slice 1: `bump-recipients.toml` and the read-only planner). Open: #96 (POLICY.md records CT 144's burst figures, #27). Every open Issue is labelled. Since 2026-10-24 the coordinators have neither run `gh pr update-branch` on this repository nor been allowed to (#79): a stale PR gets a local signed merge of `origin/main` or is re-cut from it.

## Breaks something released

1. #50: AIRIA-framework's "AIRIA Control Tests" job failed a two-daemon test with `ReadinessTimeoutError` (30 s) on the pool; the re-run was green. One occurrence, on 2026-10-18. The analysis on the Issue (2026-10-24) finds weak support for a host-wide stall. A port collision is plausible but not shown, and what exists cannot show the cause. The framework's half is AIRIA-framework#1036 (open, no activity since 2026-10-24). Since 2026-10-24 16:12 UTC every CT 144 runner has had its own port lane (#53, closed), so if the cause was a collision between runners, the lanes should stop it. No recurrence has been reported. The Issue can close once the framework's test uses the lane or #1036 names another cause. Not night work: the next step is upstream.
2. #27: load-induced timeouts on the orlando runners when about 15 PR runs start together (framework, SiteBuilder). Systemic: it fires on every burst. Step 0 merged in #85/#86. Option D (more runners on CT 144) is closed: Andrea ruled on 2026-10-25 "No to adding more runners, Thor is already to its knees, we are going to buy a new machine and build more runners there". Option E (moving Portfolio-Dashboard into the org) stays excluded. The CT 144 burst read is posted (https://github.com/AIRIA-Systems-GmbH/airia-ci/issues/27#issuecomment-6471772991): neither A nor C, because CT 144 had no timeout in 36 h 51 m of samples; its figures go into POLICY.md in #96 (open). Left open: the Orlando VM's burst log, which the operator has to collect, and the capacity answer, which is new hardware joining through M3 (pool as code). #91 records CT 144's 12 cores as decided.

## Roadmap

- M2 Owned: airia-agile process + BACKLOG.md + triage loop. Waits on airia-agile running headless; the night owns it in the interim.
- M3 The runner pool is code (NEXT).
3. #29: Thor runner pool (CT 144) as code. The pool-as-code part merged in #77 (53ad490), and #85–#91 have extended `runners/POLICY.md` since. Left open: PUSH versus PULL for applying the pool's config, which is Andrea's decision (PUSH is documented, PULL is an open proposal), and moving the originals out of `~/Development`, an operator step. Not night work.
- M4 Harvest wave 1.
4. #30: Python 3.12–3.14 matrix as a reusable job. It blocks M4 and goes first in M4 (ruling 2026-10-13). It is the CI carrier of framework M3, whose Requirement is still being elicited. The Issue has no comment and has been unchanged since 2026-10-13.
5. #94: every consumer gets a bump PR to the latest framework release set within a day of a release (framework M9), an M4 harvest item (Andrea, 2026-10-25: "go for it"). Slice 1 merged in #95 (adb1a6a: `bump-recipients.toml` and the read-only planner). Slice 2, the reusable dry-run workflow, is on branch `feat/94-bump-reusable-dry-run` (1f1ad8c); its PR is to come. Open question 1 is decided (level 0, 2026-10-28, https://github.com/AIRIA-Systems-GmbH/airia-ci/issues/94#issuecomment-6471757705): a scheduled `bump.yml` in AIRIA-framework calls the workflow on AIRIA-framework's existing Thor runner, which is a change in that repository, not here. Open question 2, widening the AIRIA commit App's permissions, is Andrea's and blocks the write slice.
6. #37: an unpinned `ty` release turned SiteBuilder's "Code quality" job red on files the PR never touched. airia-ci's half shipped in ci-v5 (#39): `python-toolchain` installs the gate tools at `pins.toml` and refuses `uv run --with ty`, and HOWTO.md says to pin. The Issue closes when SiteBuilder's job calls the gate, which is M5 adoption (`init.py protect` with Andrea's `gh` login). The standard does not accept `ty` (`pins.toml`: pyright or mypy-strict), so SiteBuilder also has to change its type checker. The Issue has had no activity since 2026-10-14. It blocks M5, so it sits before M5. Not night work.
- M5 No local workarounds. Cookiegusto's #24 (merged 2026-10-24) moves it to `@main` (not re-scanned); Portfolio-Dashboard is on `@main` by the 2026-10-24 scan. ParlAIment still calls none.

## Off-roadmap

Andrea's order (2026-10-21) of the open Issues: #54 (closed 2026-10-23), #37, #53 (closed 2026-10-24), #50, #35 (closed 2026-10-24), #27. #63 and #62 came first and are done. #31 closed on 2026-10-28. No open Issue is off-roadmap.

## Pick

#94 slice 2, the reusable dry-run workflow: it is written and on `feat/94-bump-reusable-dry-run` (1f1ad8c), so the next step is to open its PR. It needs no write credential. The write slice behind it is blocked on Andrea's App-permissions decision (open question 2), and the AIRIA-framework caller is a change in that repository.

The other open Issues wait on an operator step, an upstream fix, an Andrea decision or another repository:
- #50 waits on AIRIA-framework#1036 (open, no activity since 2026-10-24). The port lanes are live since 2026-10-24 16:12 UTC, so a recurrence there is now the evidence to watch for.
- #27 waits on the Orlando VM's burst log (operator) and on the new machine (hardware, then M3).
- #29 waits on Andrea's PUSH versus PULL decision.
- #30's Requirement is still being elicited.
- #37's remaining part is SiteBuilder's adoption (M5).
