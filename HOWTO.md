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

The script is fetched from `main` and writes the pin it names (today `@ci-v3`). It commits nothing: commit the files on a branch and open a PR. That PR's own gate run is the first review.

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

- **Within a major** there is nothing to do: `@ci-v3` moves, and your next run uses it. Every move is a GitHub Release (`ci-v3.N`) listing what changed. To hear of them, choose *Watch → Custom → Releases* on this repository.
- **Which harness ran** is in every job's `ci-results/harness.md`: the airia-ci workflow, ref and commit (the review's copy also has the inputs you passed).
- **A new major** never reaches you on its own: your pin is frozen. From `ci-v3` on, both workflows look up airia-ci's tags on every run; when a newer `ci-vN` exists, the job shows a *Newer airia-ci major* warning and the automatic review ends with a note naming it. Then follow the next section.

## Upgrade to a new major

A new major means the call contract changed: an input, a secret, an output or the review footer.

1. Read the major's Release notes and the entry for it below: they say what a caller must change.
2. In `.github/workflows/claude.yml` and in your gate workflow, change every `AIRIA-Systems-GmbH/airia-ci/…@ci-vN` to the new tag, and make the change the notes call for.
3. Open a PR. Its gate run is reviewed by the new major: check the review's `harness.md` names the new ref.

| Major | What a caller must change |
|---|---|
| `ci-v3` | Drop the `FRAMEWORK_READ_TOKEN` secret from both callers: passing it fails with an undefined-secret error. Gate results are staged in `ci-results/` inside the checkout. |
| `ci-v2` | Frozen at `9ed10a6`; move off it. Its reviewer never sees the gate results (its prompt names `$RUNNER_TEMP` in a command Bash refuses). It replaced the bare `Bash` permission with the allowlist: gates outside it go in `extra-allow`. |
| `ci-v1` | Frozen; move off it. It still grants a bare `Bash`. |

## When something goes wrong

- **No review on the PR.** `claude-review` is red with *No review posted*: the run ended without review text. Treat the PR as unreviewed and comment `@claude please review`.
- **The review job shows SKIPPED.** Its `if:` lacks `always()`: with a bare `needs:`, one red gate skips the review. The job `init.py` writes has it.
- **A draft PR was never reviewed.** The gate workflow's `pull_request` types must include `ready_for_review`.
- **No Claude credential.** The repository has no `CLAUDE_CODE_OAUTH_TOKEN` secret, or the caller does not pass it: `gh secret set CLAUDE_CODE_OAUTH_TOKEN -R <owner>/<repo>`. An `@claude` retry cannot help until then.
- **The review says a gate was not run, or lists permission denials.** The command is outside the allowlist: add it to `extra-allow` on both callers.
- **`@claude` does nothing.** The author lacks write access, or `claude.yml` is not on the default branch (comment events run the default branch's workflow).
