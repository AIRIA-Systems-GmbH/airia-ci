<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# Using airia-ci in your repository

Tasks for a repository that calls airia-ci. What each piece is lives in the [README](README.md); the call contracts are the headers of [`reusable-claude-review.yml`](.github/workflows/reusable-claude-review.yml) and [`reusable-claude-respond.yml`](.github/workflows/reusable-claude-respond.yml), and they win over this page.

## Wire a repository

From the root of its checkout, logged in with `gh`:

```
python3 <(gh api repos/AIRIA-Systems-GmbH/airia-ci/contents/scripts/init.py -H 'Accept: application/vnd.github.raw')
```

It writes only the files that are missing:

- `.github/workflows/claude.yml`, the `@claude` caller;
- the `claude-review` job at the end of your pull-request workflow; with no such workflow, `.github/workflows/merge-gate.yml`, whose placeholder gate fails until you put your real gates in it; with several, it prints the job for you to add to the one that gates merges;
- `.github/claude-review.md`, your review rules, to fill in.

Then it sets the `CLAUDE_CODE_OAUTH_TOKEN` secret (from that environment variable, or by running `claude setup-token`) and offers a ruleset on the default branch: changes only by pull request, with the gate jobs required and every commit signed and verified, no force-push or deletion. A private repository needs GitHub Pro (personal) or Team (organization) for it. `--runner '["ubuntu-latest"]'` uses GitHub's runners instead of the Thor pool; `--yes` answers every question yes.

The script is fetched from `main` and writes the pin it names (today `@ci-v5`). It commits nothing: commit the files on a branch and open a PR. That PR's own gate run is the first review.

## Adopt the standard

From `ci-v5` on, a Python repository's merge gate is the AIRIA quality standard, `reusable-python-gate.yml`. Every repository runs the same gates, with the tool versions in [`pins.toml`](pins.toml), and the same configuration on a laptop, on another machine and in the cloud. A project may **add** to it. It may **not weaken** it, and code enforces that, not this page.

1. **One workflow file**, `.github/workflows/ci.yml` (`init.py` writes it for a Python repository that has no pull-request workflow):

   ```yaml
   name: CI

   on:
     pull_request:
       types: [opened, synchronize, reopened, ready_for_review]
     push:
       branches: [main]

   permissions: {}

   jobs:
     ci:
       permissions:
         contents: read
         pull-requests: write
         issues: write
         actions: read
         checks: read
       uses: AIRIA-Systems-GmbH/airia-ci/.github/workflows/reusable-python-gate.yml@ci-v5
       secrets:
         CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
   ```

   It replaces the repository's own lint, type and test jobs and its `claude-review` job, because the gate runs the review itself. Keep `claude.yml` for `@claude`.
2. **Protect the branch**: from the checkout, with your `gh` login, run
   `python3 <(gh api repos/AIRIA-Systems-GmbH/airia-ci/contents/scripts/init.py -H 'Accept: application/vnd.github.raw') protect`.
   It writes the "Restrict Direct Merge on Main" ruleset, or rewrites the existing one into the standard form:
   - no bypass;
   - changes by pull request only, up to date with the base;
   - review threads resolved;
   - signed commits;
   - no force-push or deletion;
   - required checks `ci / gate` and `ci / claude-review / claude-review`.

   The `ci` prefix is the name of your calling job, read from your workflow.

What the gate expects of the repository. It fails, and says what is missing, when any of these is not true:

- `.python-version` is committed. Its Python must be one of `pins.toml`'s `[python] allowed`.
- `uv.lock` is committed, and `uv sync --locked` works.
- pytest, pytest-cov and coverage are in a dev group, at or above their pins (`uv add --dev pytest==<pin> pytest-cov==<pin> coverage==<pin>`). The same goes for mypy under `mypy-strict`, and for import-linter when `[tool.importlinter]` exists. They run inside your environment, at your locked versions.
- pytest measures your package: `addopts = "--cov=<package>"` under `[tool.pytest.ini_options]`. With no coverage report, or with `--no-cov`, the test job fails, because there is no figure to hold to the floor.
- The tree is `ruff format`ted, every file carries REUSE licence information, and every action in `.github/` is pinned to a commit (zizmor, with the standard policy; only `AIRIA-Systems-GmbH/airia-ci/*` may follow a tag).
- No workflow step runs a tool unpinned: `uvx ruff`, `uv run --with ty`, `uv tool run x` and `pipx run x` are refused. Pin it (`uvx ruff@0.17.0`, `--with ty==0.0.20`), or lock it and call it with `uv run`.

### What runs

| Check | What it runs | Required |
|---|---|---|
| `ci / lint` | ruff check, ruff format --check, the type checker, import-linter (when configured) | |
| `ci / changes` | whether the PR touched anything but `docs-paths`; with that input empty, always yes | |
| `ci / test …` | pytest per Python, at the coverage floor, listing the slowest tests (`--durations`) | |
| `ci / security` | reuse lint, pip-audit over the locked requirements, gitleaks over the tree, zizmor over `.github/` | |
| **`ci / gate`** | red unless every job above, and every extra job, succeeded; a skipped job is not a pass, except `lint` and `test` on a docs-only PR | required |
| **`ci / claude-review / claude-review`** | the automatic review, after `gate`, even a red one; with no Claude credential it fails, and says why | required |

The same gates on a laptop, at the versions in `pins.toml`:

```
uv sync --locked
uv run ruff check . && uv run ruff format --check .        # or uvx ruff@<pin> if ruff is not locked
uvx --from 'pyright[nodejs]==<pin>' pyright --pythonpath .venv/bin/python .
uv run pytest --cov-report=term-missing --cov-fail-under=90
uvx reuse@<pin> lint
uv export --locked --no-emit-workspace --format requirements-txt -o /tmp/req.txt && uvx pip-audit@<pin> --disable-pip -r /tmp/req.txt
gitleaks dir --redact .
uvx zizmor@<pin> --offline .github
```

### Extend it

| Input | What it adds | Limit |
|---|---|---|
| `coverage-floor` | a higher floor | refused under 90 |
| `type-checker` | `mypy-strict` instead of `pyright` | only where pins.toml allows it (AIRIA-process); `ty` is refused until its 1.0 |
| `python-versions` | more Pythons, e.g. `'["3.12", "3.13"]'` | each from `pins.toml`'s allowed list |
| `paths` | what ruff and the type checker read | |
| `postgres-image`, `qdrant-image`, `job-env` | service containers for the tests; `CI_POSTGRES_DSN` / `CI_QDRANT_URL` mapped onto your names | |
| `extra-jobs` | your own gate jobs, counted by `ci / gate` | see below |
| `runner` | GitHub's runners for a public repository (`'["ubuntu-24.04"]'`) | |
| `durations` | how many of the slowest tests pytest lists after every run (default 25; `0` lists all) | always printed; there is no way to switch it off |
| `docs-paths` | the docs-only fast path, see below | some paths are never docs |
| `extra-allow`, `extra-deny`, `extra-prompt` | the review's allowlist, denylist and prompt | the shared deny list always wins |

There is no `skip-*` input, and none will be added.

**The docs-only fast path** (`docs-paths`, off by default). A PR that changes only documentation need not wait for the test matrix. Name your documentation as git glob pathspecs, one per line:

```yaml
    with:
      docs-paths: |
        docs/**
        **/*.md
```

When every path a pull request changes matches one (for a rename, both the old and the new path), `ci / lint` and `ci / test` are skipped and `ci / gate` passes on the rest: `security`, which still checks the docs for REUSE information and secrets, and every extra job. In glob pathspecs `*` stops at `/`, so `*.md` is the top level only and `**/*.md` is every directory. Everything still runs on a push to the default branch, on any event but a pull request, and on a PR that changes no file. Some paths are never docs, whatever the patterns say: `.airia/` (suites read it), `.github/`, every `tests/` or `test/` directory, Python sources, and the files that configure the tools or the build (any `*.toml`, `uv.lock`, `.python-version`, `setup.cfg`, `*.ini`, `.importlinter`, `.coveragerc`, `requirements*.txt`, `Dockerfile*`). Name your documentation narrowly all the same: anything else a pattern matches, such as a fixture outside `tests/`, skips the tests. The required checks stay `ci / gate` and the review, so the ruleset does not change.

An extra job of your own goes before the gate, and its result is handed in:

```yaml
jobs:
  e2e:
    runs-on: ubuntu-24.04
    steps: [...]
  ci:
    needs: [e2e]
    if: always()          # without it a red e2e skips the whole gate, and `ci / gate` never reports
    permissions: { contents: read, pull-requests: write, issues: write, actions: read, checks: read }
    uses: AIRIA-Systems-GmbH/airia-ci/.github/workflows/reusable-python-gate.yml@ci-v5
    with:
      extra-jobs: ${{ toJSON(needs) }}
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

### May add, may not weaken

- **May:** raise the floor; skip lint and test on a docs-only PR (`docs-paths`); test on more allowed Pythons; add your own jobs through `extra-jobs`; add review rules (`.github/claude-review.md`, `extra-allow`, `extra-deny`); lock a newer version of a tool than the pin.
- **May not:** lower the coverage floor, in the input or in `pyproject.toml`; switch a gate off; use a type checker outside the list; lock a tool below its pin; run a tool unpinned; add a bypass to the ruleset or drop a required check.

The weekly conformance scan (`conformance.yml`) checks each consumer against this list. It keeps one issue, "airia-ci standard drift", in any consumer that falls short, and closes it once nothing drifts.

## Write your review rules

`.github/claude-review.md` is read by the reviewer before anything else. Put there only what is particular to the repository: how each gate runs locally, what a reviewer must not run, and the traps a newcomer falls into. The review procedure itself lives in airia-ci; do not restate it.

## Give the reviewer your toolchain

If the reviewer should be able to run something CI does not cover, add a composite action at `.github/actions/claude-toolchain/action.yml` that installs it. Both workflows run it after the checkout when it exists. Have it write what it provisioned, and what it could not, to `$RUNNER_TEMP/claude-toolchain-status.md`; Claude reads that file first.

## Let Claude run your gates

Claude's Bash is an allowlist (read-only commands and the common Python gate runners), not a bare `Bash`. A command outside it is refused, and the reviewer reports that gate as not run. Add your own in `extra-allow`, one Claude Code rule per line, and put dangerous repository CLIs in `extra-deny`:

```yaml
    with:
      extra-allow: |
        Bash(cargo test:*)
        Bash(cargo clippy:*)
      extra-deny: |
        Bash(my-money-cli:*)
```

The review and `@claude` are separate callers: set the same lists on both. The shared deny list always wins.

## Pin your quality tools

On the standard ([Adopt the standard](#adopt-the-standard)) the gate installs the tools at the pins and refuses unpinned ones. Until then, your own gate jobs install their linters and type checkers, and airia-ci does not. Pin every one of them. `uv run --with ty ty check` or `uvx ruff check` installs whatever was released last night, so a new diagnostic turns your PR red on files it never touched (airia-ci#37: a `ty` release did that to SiteBuilder).

Put the tools in a dev dependency group with an exact version (`uv add --dev ty==<version> ruff==<version>`) and call them as `uv run ty check …` and `uv run ruff check …`, so `uv.lock` holds the version and a bump is its own PR that shows the new diagnostics. A one-off `--with ty==<version>` also works, but nothing tells you when it is stale. `uv run ty` is not in the reviewer's default allowlist: add `Bash(uv run ty:*)` to `extra-allow` so the review can run it.

## Run on GitHub's runners

Both workflows default to the Thor pool (`[self-hosted, linux, x64, thor]`). A public repository cannot use it. Pass `runner: '["ubuntu-latest"]'` (a JSON array of `runs-on` labels) to both callers.

## Use @claude

Mention `@claude` in a PR or issue comment, a review, or an issue body. Only someone with write or admin access can start it; anyone else is refused before anything from the repository runs. It can edit, commit and push to the PR's branch, never force-push.

Inputs of `reusable-claude-respond.yml` worth knowing:

- `gate-workflow: ci.yml`: on a PR, the results of that workflow's run for the head commit are given to Claude, as the review has them.
- `postgres-image` and `qdrant-image`: start a service container; their addresses are `CI_POSTGRES_DSN` and `CI_QDRANT_URL`. `job-env` maps them onto your own names, e.g. `AIRIA_DATA_POSTGRES_DSN=$CI_POSTGRES_DSN`.
- `extra-prompt`: repository-specific sentences for the system prompt, with no single quotes.

## Get another review

The automatic review runs once per PR, on purpose: a reviewer that re-reviews every push turns a PR into an endless back-and-forth. For a second look after changes, comment `@claude please review`.

Re-running the gate on the same commit (`gh run rerun --failed`) does not post a second review: it rewrites the existing review's gate-results section with the new attempt's results.

## Stay current

- **Within a major** there is nothing to do: `@ci-v5` moves, and your next run uses it. Every move is a GitHub Release (`ci-v5.N`) listing what changed. To hear of them, choose *Watch → Custom → Releases* on this repository.
- **Which harness ran** is in every job's `ci-results/harness.md`: the airia-ci workflow, ref and commit (the review's copy also has the inputs you passed).
- **A new major** never reaches you on its own: your pin is frozen. From `ci-v3` on, both workflows look up airia-ci's tags on every run; when a newer `ci-vN` exists, the job shows a *Newer airia-ci major* warning and the automatic review ends with a note naming it. Then follow the next section.

## Upgrade to a new major

A new major means the call contract changed: an input, a secret, an output or the review footer.

1. Read the major's Release notes and the entry for it below: they say what a caller must change.
2. In `.github/workflows/claude.yml` and in your gate workflow, change every `AIRIA-Systems-GmbH/airia-ci/…@ci-vN` to the new tag, and make the change the notes call for.
3. Open a PR. Its gate run is reviewed by the new major: check the review's `harness.md` names the new ref.

| Major | What a caller must change |
|---|---|
| `ci-v5` | Change every `@ci-v4` to `@ci-v5`; no input, secret or output a ci-v4 caller passes changed. A Python repository then [adopts the standard](#adopt-the-standard): its gate jobs and its `claude-review` job give way to the one-file caller of `reusable-python-gate.yml`, and `init.py protect` makes `ci / gate` and the Claude review the required checks. Any other repository stops after the pin change. |
| `ci-v4` | Frozen; move off it. Add `checks: read` to the `permissions:` of both calling jobs (`claude-review` in your gate workflow, `claude` in `claude.yml`): without it the run fails at startup. With it, a gate that hit its `timeout-minutes` is reported as `cancelled (time cap)`, not as infrastructure. |
| `ci-v3` | Frozen; move off it. Drop the `FRAMEWORK_READ_TOKEN` secret from both callers: passing it fails with an undefined-secret error. Gate results are staged in `ci-results/` inside the checkout. |
| `ci-v2` | Frozen at `9ed10a6`; move off it. Its reviewer never sees the gate results (its prompt names `$RUNNER_TEMP` in a command Bash refuses). It replaced the bare `Bash` permission with the allowlist: gates outside it go in `extra-allow`. |
| `ci-v1` | Frozen; move off it. It still grants a bare `Bash`. |

## When something goes wrong

- **No review on the PR.** `claude-review` is red with *No review posted*: the run ended without review text. Treat the PR as unreviewed and comment `@claude please review`.
- **The review job shows SKIPPED.** Its `if:` lacks `always()`: with a bare `needs:`, one red gate skips the review. The job `init.py` writes has it.
- **A draft PR was never reviewed.** The gate workflow's `pull_request` types must include `ready_for_review`.
- **No Claude credential.** The repository has no `CLAUDE_CODE_OAUTH_TOKEN` secret, or the caller does not pass it: `gh secret set CLAUDE_CODE_OAUTH_TOKEN -R <owner>/<repo>`. An `@claude` retry cannot help until then.
- **The review says a gate was not run, or lists permission denials.** The command is outside the allowlist: add it to `extra-allow` on both callers.
- **`@claude` does nothing.** The author lacks write access, or `claude.yml` is not on the default branch (comment events run the default branch's workflow).
