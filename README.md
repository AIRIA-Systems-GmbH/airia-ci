<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# airia-ci

The shared CI harness for AIRIA repositories. It has one reusable workflow and two composite actions, consumed at the moving tag `@ci-v2`.

| Path | What it is |
|---|---|
| `.github/workflows/reusable-claude-review.yml` | The automatic Claude review. It runs once per PR, as the last job of the caller's merge-gate workflow, and reads the gate results instead of re-running them. |
| `.github/actions/ci-results` | Downloads every completed job of a workflow run (its conclusion, failed steps and full log) for a Claude job to read. |
| `.github/actions/claude-cli` | A Claude Code build cached on the runner's `/var/cache/ci` volume, refreshed at most once a day. |

## Using it

The call contract is the header of `reusable-claude-review.yml`. Read it before wiring a repository. In short, the caller:

- adds a last job with `needs:` set to every gate job, `if: always() && …`, and `uses: AIRIA-Systems-GmbH/airia-ci/.github/workflows/reusable-claude-review.yml@ci-v2`;
- has its own `CLAUDE_CODE_OAUTH_TOKEN` secret, which it passes to that job (this repository holds no secrets);
- has self-hosted runners labelled `[self-hosted, linux, x64, thor]`;
- optionally has `.github/actions/claude-toolchain/action.yml`, to provision its toolchain, and `.github/claude-review.md`, for its own review rules.

## Versioning

`ci-v2` moves on every compatible change. A change to the call contract is `ci-v3`. `ci-v2` replaced the reviewer's bare `Bash` permission with an explicit allowlist (read-only commands and the common Python gate runners); a repository whose gates fall outside it passes them in `extra-allow`. `ci-v1` is frozen at its last commit and still grants a bare `Bash` — move off it. The review footer string `_Automatic review by \`claude-code-review.yml\`` is part of the contract, because the once-per-PR check keys on it.

## Licence

Apache-2.0. See `LICENSES/Apache-2.0.txt`.
