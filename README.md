<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# airia-ci

The shared CI harness for AIRIA repositories. It has two reusable workflows and five composite actions, consumed at the moving tag `@ci-v3`.

| Path | What it is |
|---|---|
| `.github/workflows/reusable-claude-review.yml` | The automatic Claude review. It runs once per PR, as the last job of the caller's merge-gate workflow, and reads the gate results instead of re-running them. |
| `.github/workflows/reusable-claude-respond.yml` | The `@claude` responder. Only an OWNER, MEMBER or COLLABORATOR can start it; it can edit, commit and push, and it reads the PR's gate results. Its call contract is its own header. |
| `.github/actions/claude-settings` | The one Claude Code permission policy both workflows use: an explicit Bash allowlist (never a bare `Bash`), in a read-only `review` profile and a `respond` profile that can also write and commit. |
| `.github/actions/ci-results` | Downloads every completed job of a workflow run (its conclusion, failed steps and full log) for a Claude job to read. A job that was cancelled before any step started is labelled `cancelled (never ran)`: it has no log and is not a result. |
| `.github/actions/review-refresh` | When a gate is re-run on the same commit, writes that attempt's gate table into the existing automatic review instead of reviewing again. `review_refresh.py` beside it does the rewrite. |
| `.github/actions/claude-cli` | A Claude Code build cached on the runner's `/var/cache/ci` volume, refreshed at most once a day. `CLAUDE_CLI_CACHE` moves the cache root; only the tests set it. |
| `.github/actions/verified-commit` | Commits a job's working-tree changes as a GitHub-signed ("Verified") commit, through `createCommitOnBranch` and a contents-only token minted for the AIRIA commit App, so no signing key lives on the runner. `verified_commit.py` beside it is the same thing as a CLI. Its contract and limits are its own header. |

## Using it

The call contract is the header of `reusable-claude-review.yml`. Read it before wiring a repository. In short, the caller:

- adds a last job with `needs:` set to every gate job, `if: always() && …`, and `uses: AIRIA-Systems-GmbH/airia-ci/.github/workflows/reusable-claude-review.yml@ci-v3`;
- has its own `CLAUDE_CODE_OAUTH_TOKEN` secret, which it passes to that job (this repository's own secret serves only its self-review and `@claude`);
- runs on self-hosted runners labelled `[self-hosted, linux, x64, thor]`, unless it passes `runner:` (the `runs-on` labels as a JSON array, e.g. `'["ubuntu-latest"]'`);
- optionally has `.github/actions/claude-toolchain/action.yml`, to provision its toolchain, and `.github/claude-review.md`, for its own review rules.

## Tests

`python3 -m unittest discover -s tests` needs Python 3.11+ and PyYAML. The tests run the shipped code itself: `tests/steps.py` slices every `run:` block out of the action and workflow YAML (and each Python heredoc into its own file) and runs it with the shell flags GitHub uses, against a fake `gh`. The `claude-cli` tests need GNU tools and skip on macOS.

The `self-test` workflow runs on every pull request, on GitHub-hosted runners:

- `unittest`, the suite above;
- `coverage`, the gate: Python (coverage.py, statements and branches) and bash (kcov, lines) as one figure over everything consumers execute, refused under 95%, and refused if any test skips or any shipped block goes unmeasured. Run it locally with the same pinned image:

  ```
  docker run --rm -v "$PWD:/src" -w /src --entrypoint bash \
    kcov/kcov@sha256:481289ae32e55e5b733019515acd10948a4f76dfed381765577db909664fc603 tests/coverage.sh
  ```

- `lint`: actionlint, shellcheck (also over the sliced composite-action blocks, which actionlint does not read) and ruff;
- `actions`: the composite actions at the PR's own commit (the reusable workflows call them `@ci-v3`, so this is the only job that runs a change to an action before release);
- `claude-review`: this repository reviews itself with its own reusable workflow, on `ubuntu-latest` (a public repository cannot use the Thor pool). `claude.yml` is its `@claude` responder.

## Versioning

`ci-v3` moves on every compatible change. A change to the call contract is `ci-v4`. A maintainer releases by dispatching `release.yml` on `main` (`move` or `new`); it refuses a commit whose `self-test` did not succeed and a move that is not forward. `ci-v3` removed the optional `FRAMEWORK_READ_TOKEN` secret from both workflows (`airia-*` packages install from the internal index, so no job reads another repository); a caller still passing it must drop it. Both workflows stage the gate results in `ci-results/` inside the checkout (git-excluded) and point Claude at that relative path; no directory outside the checkout is granted and `gh pr checks` is not used — `validation/ci-results-staging/` proves the reviewer reads them. `ci-v2` is frozen at `9ed10a6`: its review prompt tells the reviewer to `cat "$RUNNER_TEMP/…"`, which Bash refuses, so a `ci-v2` review never sees the gate results — move off it. `ci-v2` replaced the reviewer's bare `Bash` permission with an explicit allowlist (read-only commands and the common Python gate runners); a repository whose gates fall outside it passes them in `extra-allow`. `ci-v1` is frozen at its last commit and still grants a bare `Bash` — move off it. The review footer string `_Automatic review by \`claude-code-review.yml\`` is part of the contract, because the once-per-PR check keys on it. A re-run attempt of the run that wrote the review (`gh run rerun --failed`) refreshes that review's gate-results section; it does not post a second review. `verified-commit` exits 2 when the API refuses to create the branch, as it does for a refused commit; it used to fail with a traceback.

## Licence

Apache-2.0. See `LICENSES/Apache-2.0.txt`.
