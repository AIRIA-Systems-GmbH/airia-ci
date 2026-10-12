<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# Contributing to airia-ci

airia-ci is the shared CI platform of the AIRIA repositories: the reusable workflows and composite actions, and the Thor self-hosted runner pool they run on. It is run like an open-source project. Every calling repository, and every agent working in one, is a contributor.

## Upstream first

A fix made in one calling repository helps that repository only. The same fix made here helps all of them.

1. **File here first.** A CI, workflow or runner problem found in any repository gets an issue on airia-ci before anything is fixed locally. Search first:

   ```
   gh issue list -R AIRIA-Systems-GmbH/airia-ci --search "<words>"
   gh issue create -R AIRIA-Systems-GmbH/airia-ci --template ci-defect.yml --label "from:<repo>"
   ```

   There are templates for a **CI defect**, a **Thor runner problem** and a **harvest request**.
2. **Mark every workaround.** You may unblock yourself locally, but the workaround carries the comment `# upstream: airia-ci#<n>`. It is deleted in the PR that adopts the fix. `git grep -n 'upstream: airia-ci#'` in a calling repository lists its open workarounds; no script here counts them yet.
3. **Never make a third copy.** If a job, action or script you need exists in another repository, file a harvest request. A shared piece is harvested once there are two donors. The harvest names its inputs from what differs between the donors, and its first adopter deletes its own copy in the same week.
4. **Link both sides.** If the calling repository records the problem as one of its own artifacts (an Issue in `.airia/`), that artifact links the airia-ci issue. It resolves when the repository adopts the tag that carries the fix.

## Labels

- `from:<repo>`: who filed it. Use one per calling repository.
- `defect`, `runner`, `harvest`: the template the issue came from.

## Runner pool changes

A change to the Thor pool, such as a new interpreter in the toolcache, a new runner, or a change to concurrency or caches, is announced as an issue labelled `runner` **before** it lands. On 2026-10-09, CPython 3.14.8 reached the pool unannounced, and AIRIA-process went red the same day.

## Pull requests

PRs follow [HOWTO.md](HOWTO.md) and the tests in [README.md](README.md#tests). The `self-test` workflow is the gate: coverage stays at or above 95%, and lint must pass. A change reaches calling repositories when it is released under the moving `ci-v3` tag.
