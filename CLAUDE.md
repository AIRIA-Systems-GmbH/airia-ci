<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# Maintaining airia-ci

Consumers run this repository's YAML at the moving tag `@ci-v3`. Every `run:` block, script and heredoc under `.github/` is shipped code.

## Conventions

- **Shipped code is tested by slicing it from the YAML, never by copying it.** `tests/steps.py` reads each `run:` block out of the action or workflow file and runs it with GitHub's shell flags (composite `shell: bash` gets `set -eo pipefail`; a workflow step with no `shell:` gets `set -e`). A Python heredoc (`python3 - <<'PY'`) is written out as its own `.py` file so coverage.py can measure it. Use `Runner` for a scratch directory and a rule-based fake `gh`; use `bare(...)` for a deliberately minimal child environment that coverage still follows.
- A `run:` block may not contain `${{ … }}`: pass expressions through `env:`. The harness refuses a block with one, because a sliced copy would not be the shipped code.
- **New Python in a composite action goes in a file beside `action.yml`** (`review_refresh.py`, `verified_commit.py`), not in a heredoc. `claude-settings` is the existing exception.
- Every file carries an SPDX header (REUSE: `REUSE.toml` covers the files that cannot).
- Commit messages are outcome sentences, as in `git log`: what is true after the commit (`A re-run refreshes the review's gate table; …`), not what was typed.

## The coverage gate

`tests/coverage.sh`, in the pinned kcov image (the command is in the README and in `self-test.yml`). One figure over Python (statements + branches) and bash (lines), floor 95%.

- **A new `run:` block is counted automatically.** `steps.materialise()` writes every shipped block before the tests run, so an untested one is in the figure at 0%. `coverage_total.py` also fails if any shipped `.sh`/`.py` is missing from the reports, and `coverage.sh` fails on any skipped test.
- **The kcov quote correction.** kcov counts a line that starts inside a multi-line quoted string (a jq filter body) as executable and never marks it hit; `coverage_total.lines_inside_quotes` drops those lines and nothing else. Two other kcov blind spots (the first line of a `\`-continued command; `} > file` / `done < file`) are deliberately not corrected: they only understate the figure. Change the correction only with a test in `tests/test_coverage_total.py`.
- Do not lower the floor to make a change pass. A real miss gets a test; a new kcov false-miss class gets a documented, tested correction.

## Tags and versioning

- `ci-v3` moves on every compatible change; a change to a call contract (inputs, secrets, outputs, the review footer string) is `ci-vN+1`. Older tags are frozen.
- Moving a tag is a force-push of a ref other repositories consume: never do it without the maintainer's explicit go-ahead, and only on a SHA whose `self-test` run succeeded.
- `main` can be ahead of `ci-v3`; a merged fix reaches consumers only when the tag moves.

## Before you finish

`python3 -m unittest discover -s tests`, then the coverage gate, then the three linters as `self-test.yml`'s `lint` job runs them (actionlint; shellcheck `-x -S warning` over the tracked scripts and `build/coverage/src/*.sh`; `ruff check .`).
