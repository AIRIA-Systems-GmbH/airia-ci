<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# airia-ci

> **This README is for airia-ci's maintainers.** If you are adopting airia-ci in a repository, read the **[Quick start guide → HOWTO.md](HOWTO.md#quick-start-adopt-the-standard-in-a-python-repository)**: five steps, in order, each a command. Read it on GitHub (`main`), not in an old local checkout: one from before `ci-v5` has no Python gate in it at all.

**For a Python repository, airia-ci is `reusable-python-gate.yml`, the whole merge gate** (lint, types, tests at a 90 % coverage floor, licences, vulnerabilities, secrets, workflow security), which ends with the Claude review. The two Claude workflows below are the parts it calls, and the whole of what a repository in another language uses.

The shared CI harness for AIRIA repositories, and the distribution channel of the AIRIA quality standard. It has three reusable workflows and six composite actions, consumed at `@main`.

**The standard** is one workflow file in a Python repository, calling `reusable-python-gate.yml`, plus `init.py protect` for the branch. Every repository then runs the same gates, at the versions in `pins.toml`, summed into one required `gate` check and followed by a required Claude review. A project may add to it, never weaken it. [HOWTO.md](HOWTO.md#adopt-the-standard) has the snippet and the rules.

| Path | What it is |
|---|---|
| `.github/workflows/reusable-python-gate.yml` | The AIRIA quality standard for a Python repository. It runs `lint` (ruff check and format, pyright or mypy --strict, import-linter), `test` (pytest per Python at the coverage floor), `security` (reuse, pip-audit, gitleaks, zizmor), then `gate`: red unless every one of them, and every caller's extra job, succeeded. Then the Claude review. Its call contract is its own header. |
| `.github/workflows/reusable-claude-review.yml` | The automatic Claude review. It runs once per head commit (a push after its review is reviewed again, with the earlier review as its memory), as the last job of the caller's merge-gate workflow, and reads the gate results instead of re-running them. |
| `.github/workflows/reusable-claude-respond.yml` | The `@claude` responder. Only an OWNER, MEMBER or COLLABORATOR can start it; it can edit, commit and push, and it reads the PR's gate results. Its call contract is its own header. |
| `.github/actions/claude-settings` | The one Claude Code permission policy both workflows use: an explicit Bash allowlist (never a bare `Bash`), in a read-only `review` profile and a `respond` profile that can also write and commit. |
| `.github/actions/ci-results` | Downloads every completed job of a workflow run (its conclusion, failed steps and full log) for a Claude job to read. A job that was cancelled before any step started is labelled `cancelled (never ran)`: it has no log and is not a result. One that hit its `timeout-minutes` is labelled `cancelled (time cap)`, read from the check run's annotations (the token needs `checks: read`); if they cannot be read it is `cancelled (time cap unchecked, ran …)`. |
| `.github/actions/review-refresh` | When a gate is re-run on the same commit, writes that attempt's gate table into the existing automatic review instead of reviewing again. `review_refresh.py` beside it does the rewrite. |
| `.github/actions/claude-cli` | A Claude Code build cached on the runner's `/var/cache/ci` volume, refreshed at most once a day. `CLAUDE_CLI_CACHE` moves the cache root; only the tests set it. |
| `.github/actions/python-toolchain` | The AIRIA Python standard's toolchain: uv and Python from `.python-version`, `uv sync --locked`, and the gate tools on PATH at the versions in `pins.toml`. It refuses an input below the standard (a lower coverage floor, a type checker outside the accepted list), a `uv.lock` that resolves a gate tool below its pin, and a workflow step that runs a tool unpinned (`uvx ruff`, `uv run --with ty`). `toolchain.py` beside it is the logic. |
| `templates/python/` | What a consumer copies: the standard's `pyproject.toml` blocks (`init.py` adds the missing ones; a test holds their versions to `pins.toml`) and `ci-local.sh`, the gate's lint and test jobs on a laptop. |
| `pins.toml` | The standard itself: one exact version per gate tool, the allowed Python versions, the 90 % coverage floor and the accepted type checkers. A project may raise a floor, never lower it. |
| `.github/actions/verified-commit` | Commits a job's working-tree changes as a GitHub-signed ("Verified") commit, through `createCommitOnBranch` and a contents-only token minted for the AIRIA commit App, so no signing key lives on the runner. `verified_commit.py` beside it is the same thing as a CLI. Its contract and limits are its own header. |

## Using it

**[HOWTO.md](HOWTO.md)** is the guide for a calling repository: wiring it, review rules, toolchain, `extra-allow`, `@claude`, staying current, upgrading a major, and what to do when a review does not appear.

To wire a repository, run `scripts/init.py` from the root of its checkout, logged in with `gh`:

```
python3 <(gh api repos/AIRIA-Systems-GmbH/airia-ci/contents/scripts/init.py -H 'Accept: application/vnd.github.raw')
```

It writes whichever of these are missing: the `@claude` caller; the merge gate, which for a Python repository with no pull-request workflow is `.github/workflows/ci.yml` calling `reusable-python-gate.yml` (the whole standard, with the review), and otherwise the review job appended to the one pull-request workflow (or a placeholder `merge-gate.yml`); `.github/claude-review.md`; and, in a Python repository, the standard's `pyproject.toml` blocks from `templates/python/`. It sets the `CLAUDE_CODE_OAUTH_TOKEN` secret, offers a ruleset on the default branch, and prints the steps that remain. It commits nothing.

The call contract is the header of `reusable-claude-review.yml`; read it before wiring a repository by hand. In short, the caller:

- adds a last job with `needs:` set to every gate job, `if: always() && …`, and `uses: AIRIA-Systems-GmbH/airia-ci/.github/workflows/reusable-claude-review.yml@main`;
- has its own `CLAUDE_CODE_OAUTH_TOKEN` secret, which it passes to that job (this repository's own secret serves only its self-review and `@claude`);
- runs on self-hosted runners labelled `[self-hosted, linux, x64, thor]`, unless it passes `runner:` (the `runs-on` labels as a JSON array, e.g. `'["ubuntu-24.04"]'`, which a public repository or one outside AIRIA-Systems-GmbH must pass);
- optionally has `.github/actions/claude-toolchain/action.yml`, to provision its toolchain, and `.github/claude-review.md`, for its own review rules.

Both workflows also write `ci-results/harness.md`, which names the airia-ci workflow and commit the job runs. The review's copy also records the inputs the caller passed.

## Tests

`python3 -m unittest discover -s tests` needs Python 3.11+ and PyYAML. The tests run the shipped code itself: `tests/steps.py` slices every `run:` block out of the action and workflow YAML (and each Python heredoc into its own file) and runs it with the shell flags GitHub uses, against a fake `gh`. The `claude-cli` tests need GNU tools and skip on macOS. `validation/ci-results-staging/` is the live proof that the reviewer reads the staged gate results.

The `self-test` workflow runs on every pull request, on GitHub-hosted runners:

- `unittest`, the suite above;
- `coverage`, the gate: Python (coverage.py, statements and branches) and bash (kcov, lines) as one figure over everything consumers execute, refused under 95%, and refused if any test skips or any shipped block goes unmeasured. Run it locally with the same pinned image:

  ```
  docker run --rm -v "$PWD:/src" -w /src --entrypoint bash \
    kcov/kcov@sha256:481289ae32e55e5b733019515acd10948a4f76dfed381765577db909664fc603 tests/coverage.sh
  ```

- `lint`: actionlint, shellcheck (also over the sliced composite-action blocks, which actionlint does not read) and ruff;
- `actions`: the composite actions at the PR's own commit (the reusable workflows call them `@main`, so this is the only job that runs a change to an action before it merges);
- `claude-review`: this repository reviews itself with its own reusable workflow, on `ubuntu-24.04` (a public repository cannot use the Thor pool), except on a Dependabot PR, which gets no secrets. `claude.yml` is its `@claude` responder.

## Versioning

Callers call `@main`, never a pinned tag (Andrea, 2026-10-20): a merged fix reaches every caller on its next run, and a red build is fixed here, never pinned back. A change to the call contract (inputs, secrets, outputs, the review footer string `_Automatic review by \`claude-code-review.yml\``, on which the once-per-head check keys) is announced in an issue before it merges, because no tag shields a caller from it.

- **The `ci-vN` tags are frozen history** and no longer move: `ci-v5` stays at `62b3606` (`ci-v5.4`). Inside a tag the workflows call their actions at that same tag, so moving one past #56 would give its callers main's actions under the tag's workflows. A frozen caller moves to `@main` ([HOWTO.md](HOWTO.md#upgrade-to-a-new-major) lists what each tag asks of it).
- **The tag machinery stays until #63:** `release.yml`, the *Look for a newer airia-ci major* step and `HARNESS_MAJOR`. Do not dispatch `release.yml`.

`conformance.yml` runs `scripts/consumers.py --conformance` weekly. It keeps one "airia-ci standard drift" issue in each consumer that falls short of the standard, and it needs the `CONFORMANCE_TOKEN` secret (see its header).

`scripts/consumers.py` lists every caller and flags one pinned to a `ci-vN` tag instead of `@main`; `scripts/canary.py` proves `@claude` answers on a consumer PR. [CLAUDE.md](CLAUDE.md) has the commands.

## Licence

Apache-2.0. See `LICENSES/Apache-2.0.txt`.
