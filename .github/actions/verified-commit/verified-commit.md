<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# verified-commit

Commits a job's working-tree change through GitHub's `createCommitOnBranch`
GraphQL mutation, as the **AIRIA CI** GitHub App. GitHub signs the commit
itself, so it shows **Verified** with no signing key on the runner, and,
unlike a `GITHUB_TOKEN` commit, it starts the target branch's workflows (a bump
PR runs its merge gate). The local ref is fast-forwarded to the new commit;
nothing is ever force-pushed. `action.yml` states the limits.

```yaml
- uses: actions/checkout@v4          # with its default persisted credentials
- run: ./bump-version.sh 1.2.3        # changes files in the working tree
- uses: AIRIA-Systems-GmbH/airia-ci/.github/actions/verified-commit@ci-v3
  with:
    client-id: ${{ vars.AIRIA_CI_APP_CLIENT_ID }}
    private-key: ${{ secrets.AIRIA_CI_APP_PRIVATE_KEY }}
    message: "Release 1.2.3"
    branch: release/v1.2.3             # must already exist on origin
    # paths: |                         # optional; default: every change
    #   pyproject.toml
    # dry-run: true                    # package and print only
```

Outputs: `committed`, `sha`, `verified`. The step fails if GitHub does not
report the commit as verified.

**What it refuses, before calling the API:** a branch missing on origin; a
local HEAD that is not exactly the remote head (unpushed commits, or a remote
that moved); a detached HEAD without `branch`; a new symlink, submodule or
executable file; a mode change; an edit to an existing executable file. The
API writes every file as `100644`, so any of these would commit something
other than what is on disk. A rename is sent as a deletion plus an addition.

## Operator setup — once, by the operator

Nothing here was done by the PR that added this action: the App, its key and
the secrets are the operator's.

1. **Create the GitHub App** under the `AIRIA-Systems-GmbH` organization
   (Settings → Developer settings → GitHub Apps → New), name e.g. `airia-ci`:
   - Homepage URL: `https://github.com/AIRIA-Systems-GmbH/airia-ci`
   - Webhook: **inactive** (uncheck "Active"); no callback URL.
   - Repository permissions:
     - **Contents: Read and write** — the commit.
     - **Pull requests: Read and write** — open the bump PR.
     - **Metadata: Read** — mandatory, set automatically.
     - Checks: Read — *optional*. Nothing needs it today: the review bot reads
       the gate results from `ci-results/` (airia-ci PR #9), so it needs no
       extra scope. This ONE App covers any later read need too.
   - **Where can this GitHub App be installed: Any account.** CryptoStudio is
     `tumma72/CryptoStudio`, a personal-account repository; an App limited to
     "Only on this account" cannot be installed there. (Alternative: a second,
     identical App owned by `tumma72`.)
2. **Generate a private key** (App settings → Private keys) and note the
   **Client ID** (App settings → About; `Iv…`). The App ID is not used:
   `actions/create-github-app-token` deprecated it for the Client ID.
3. **Install the App** on: `AIRIA-Systems-GmbH` → *Only select repositories* →
   `airia-ci` (for the self-test) plus each repository that will commit
   through it; and on `tumma72` → `CryptoStudio`.
4. **Store the credentials** under exactly these names:
   - variable **`AIRIA_CI_APP_CLIENT_ID`** = the Client ID (not a secret)
   - secret **`AIRIA_CI_APP_PRIVATE_KEY`** = the whole `.pem`, header lines included

   For the org, once, scoped to the selected repositories:
   ```sh
   gh variable set AIRIA_CI_APP_CLIENT_ID --org AIRIA-Systems-GmbH --visibility selected --repos airia-ci --body Iv23…
   gh secret set AIRIA_CI_APP_PRIVATE_KEY --org AIRIA-Systems-GmbH --visibility selected --repos airia-ci < airia-ci.private-key.pem
   ```
   For a personal-account repository (no org secrets there):
   ```sh
   gh variable set AIRIA_CI_APP_CLIENT_ID -R tumma72/CryptoStudio --body Iv23…
   gh secret set AIRIA_CI_APP_PRIVATE_KEY -R tumma72/CryptoStudio < airia-ci.private-key.pem
   ```
   Then delete the downloaded `.pem` (or keep it only in 1Password).
5. **Branch protection**: if a target branch requires signed commits, nothing
   changes (the commits are signed). If it restricts who may push, add the App
   to the allowed actors.

## Post-setup test — one command

After this action is on `main`, run:

```sh
R=AIRIA-Systems-GmbH/airia-ci W=verified-commit-selftest.yml; gh workflow run $W -R $R && sleep 8 && id=$(gh run list -R $R -w $W -L 1 --json databaseId -q '.[0].databaseId') && gh run watch $id -R $R --exit-status > /dev/null; gh run view $id -R $R --log | grep -oE 'verified: (true|false)$'
```

It prints `verified: true` when the App, key, variable, secret and
installation are all right. The workflow commits one file to a throwaway
`verified-commit-selftest/<run>` branch, asks GitHub for the commit's
signature, and deletes the branch. Anything else (`verified: false`, or no
line and a red run) — `gh run view $id -R $R --log-failed` says which step.

## CryptoStudio: release bump through the App (decision 0059) — plan, not done

Today the release-engineer agent bumps the version and CHANGELOG on a release
branch locally and commits there, signed by the operator's 1Password SSH key;
when the signer is locked the release stalls. `deploy/build-bundle.sh` and
`deploy/release.sh` (0059) build and push a bundle **from the release tag**
and never commit, so they stay as they are. Only the bump commit moves:

1. A `workflow_dispatch` workflow in CryptoStudio, `bump.yml`, input
   `version` (validated `^[0-9]+\.[0-9]+\.[0-9]+$`, passed via `env`), runs on
   `ubuntu-latest`: checkout `main`; create `release/v<version>` from `main`
   via the REST refs API; `git checkout -b release/v<version>`; rewrite every
   version declaration the release-engineer lists today (`pyproject.toml`,
   `__version__`, the `uv.lock` project entry) plus the CHANGELOG heading;
   then `verified-commit` with `branch: release/v<version>` and `paths:` set to
   exactly those files; then `gh pr create` with a token from its own
   `actions/create-github-app-token` step (`permission-pull-requests: write`)
   — the action's internal token is not exposed and carries `contents: write`
   only. Because the commit is the App's, the PR's `ci.yml` gate runs.
2. The release-engineer agent stops committing the bump: it dispatches
   `bump.yml`, writes the CHANGELOG body / "surface delta" paragraph into the
   PR (a second `verified-commit` call, or a PR suggestion the operator
   accepts), waits for the gate, merges.
3. Tag on the merged commit as today (`release.yml` builds the ingestor on the
   tag). A tag pushed by `GITHUB_TOKEN` would not start `release.yml`; if the
   tag moves into a workflow too, create it with the App token.
4. `deploy/build-bundle.sh` → `deploy/release.sh` from Gandalf, unchanged.

Not rolled out: no consumer repository is changed by the PR that added this
action; the operator approves that scope.
