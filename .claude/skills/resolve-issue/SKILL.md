---
name: resolve-issue
description: Fix one Issue end to end with a reproduce-first protocol — a failing automated test that reproduces it, a fix that turns it green, then a revert check proving the test catches the Issue. Use when asked to "resolve/fix an issue", "/resolve-issue ISS-n", or by the portfolio night. Harness-agnostic stopgap until the AIRIA harness ships it; projects with the airia-sdlc plugin use the plugin's /resolve-issue and apply the same verification protocol.
argument-hint: "<issue id, file path or GitHub issue number>"
user-invocable: true
version: 1.0.0 (Second-Memory-System harness/skills/resolve-issue, 2026-10-13)
---

# /resolve-issue — reproduce, fix, prove

One Issue, seen through. The fix is not done when a test is green: it is done when the test is
proven to catch the Issue.

## 0. Find and read the Issue

Look where this project keeps Issues, in this order: `airia artifact read issue <ID>` (projects with
`.airia/`), `docs/issues/<ID>-*.md`, `gh issue view <n>`. Read expected vs actual, the reproduction
steps, the context. If the project's `BACKLOG.md` (or `docs/BACKLOG.md`) places it, read what it
blocks. If this repo has role agents, use them: the agent that writes the reproducing test is NOT
the agent that writes the fix (tester vs developer — e.g. Cookiegusto `testone` / `codino`,
Portfolio-Dashboard `test-dev` / `code-dev`, CryptoStudio `harness-engineer` with
`implementation-reviewer` reviewing). Read `docs/CODING.md` / `docs/TESTING.md` if present.

Stop and report (no fix) if the Issue's correction needs a new or changed Requirement, Story,
Design, ADR or Plan, or touches a money / release / deploy path the project gates — that is daytime
work for the owner.

## 1. Reproduce — RED for the right reason

1. Write an automated test that reproduces the Issue at the lowest level that shows it (unit, else
   integration/acceptance). Name it after the Issue (`test_iss_123_...`).
2. Run it. It must FAIL, and fail for the reason the Issue describes (read the assertion message —
   an import error or fixture crash is not a reproduction).
3. Also reproduce the Issue the way it was reported (its steps/command), and record the output.
4. Cannot reproduce? Record what you ran and what you saw on the Issue, and stop.

## 2. Fix — GREEN, and the Issue gone

1. Write the smallest fix that makes the test pass. Never edit the reproducing test to make it pass,
   never weaken an assertion, never skip or xfail a test.
2. Run the test: GREEN. Re-run the reported reproduction: the Issue must no longer manifest.
3. Test green but the Issue still manifests → the test does not capture the Issue. Add another test
   that fails for the remaining cause, and go back to step 2.1. Repeat until both agree.
4. Run the full suite and the project's quality gates (lint, types, architecture/import tests).
   Compare against the baseline on the default branch: no new failures.

## 3. Prove — the revert check

1. Remove the fix only, keeping the tests: `git stash push -- <fix files>` (never the test files).
2. Run the new tests: they must be RED, and the reported reproduction must show the Issue again.
3. Restore the fix: `git stash pop`. Run again: GREEN, Issue gone.
4. If step 2 stays green, the tests do not guard the Issue — return to 2.3.

Record the three runs (RED before, GREEN after, RED on revert / GREEN on restore) with their
commands and one-line results — they go into the PR body.

## 4. Review, record, ship

1. A reviewer (agent or role other than the fixer) reads the diff and the three runs; address its
   findings.
2. Update the Issue the project's way: the `/resolve-issue` workflow of the airia-sdlc plugin where
   it exists; else the Issue file's `status: resolved` plus a *Resolution* section (root cause, fix,
   tests); else a comment on the GitHub issue (it closes when the PR merges: `Fixes #n`).
3. Commit (`fix(<ID>): <what>`), push the branch, open one PR naming the Issue, its severity, the
   root cause, and the three runs. Never push to main, never merge, never force-push.

Command form for unattended runs: call `git`, `uv`, `gh`, `airia` directly — no `cd`, no `&&`, no
shell loops; use the Edit tool rather than `sed -i`/`perl -pi`.
