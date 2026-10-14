---
roadmap: Roadmap-airia-ci
updated: 2026-10-14
---
<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# airia-ci backlog

The order in which to do the work. Milestones are those of the Roadmap note `Roadmap-airia-ci`, kept in its order. Issues are GitHub issues on AIRIA-Systems-GmbH/airia-ci. Each Issue sits above the milestone it blocks. An Issue that breaks something already released goes at the very top.

## Breaks something released

1. #27: load-induced timeouts on the orlando runners when about 15 PR runs start together. Consumer gates (framework, SiteBuilder) go red with no code cause. This is systemic: two consumers are hit, and it fires on every burst. Feeds the capacity/concurrency limits in M3's `runners/POLICY.md` (#29). The fix is pool capacity, so it is #29 / the operator's.
2. #35: `uv` in the shared `/opt/hostedtoolcache` fails with `Text file busy` mid-job on thor. A consumer gate (DevTools) went red with no code cause; one occurrence so far. Feeds the cache rule in M3's `runners/POLICY.md` (#29). Every proposed fix is host-side (per-runner `AGENT_TOOLSDIRECTORY`, or uv pre-seeded on the host) or a consumer-side workaround. Nothing under `.github/` sets up uv.

## Roadmap

- M2 Owned: airia-agile process + BACKLOG.md + triage loop. Waits on airia-agile running headless; the night owns it in the interim.
3. #29: Thor runner pool (CT 144) as code: `runners/` + `POLICY.md`. This is M3's carrier and blocks M3. Andrea is working it himself; not night work.
- M3 The runner pool is code (NEXT).
4. #30: Python 3.12–3.14 matrix as a reusable job. Blocks M4 and goes first in M4 (ruling 2026-10-13). It is the CI carrier of framework M3, whose Requirement is still being elicited.
5. #31: per-job git global config isolation (donor AIRIA-process #159). Blocks M4's job-isolation action. Needs the per-job state list in `runners/POLICY.md` (#29).
- M4 Harvest wave 1.
- M5 No local workarounds.

## Off-roadmap

None.
