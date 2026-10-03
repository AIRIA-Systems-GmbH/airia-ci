<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# Validation: the reviewer reads ci-results/

`run.sh` runs the code consumers actually execute — each `run:` block is
extracted from the action / workflow YAML by `lib.py`, never copied — and then
a real headless Claude Code under the composed review settings.

| Check | What it proves |
|---|---|
| settings | the `review` profile grants no directory outside the checkout and no `gh pr checks`; `666a54a`'s (the commit before #9) did grant `$RUNNER_TEMP/ci-results` |
| collect | the ci-results action, fed the jobs API of a real run, writes one row per completed job, the head commit, and every log |
| git exclude | the workflows' "Keep ci-results/ out of git" step leaves `git status` empty and `git add -A` staging nothing |
| A | `cat ci-results/jobs.md` (relative, no grant) — the figures are read: the head commit comes back verbatim |
| B | `cat $RUNNER_TEMP/ci-results/jobs.md` — refused even under the old grant (variable expansion) |
| C | `cat <absolute $RUNNER_TEMP path>` with the new settings — refused (outside the checkout) |
| D | the same absolute path under the old grant — read: the grant was what the old layout depended on |

A–D pass only if the denial count AND the presence/absence of the head
commit in Claude's answer both match; Claude cannot know that commit without
reading the file.

    ./run.sh                                     # offline, recorded fixture
    LIVE=actions/checkout:36514614158 ./run.sh   # a real run via the real gh
    SKIP_CLAUDE=1 ./run.sh                       # deterministic checks only

Claude runs with `--setting-sources project` from an empty scratch workspace and
`--permission-prompts none`, so the operator's own `~/.claude` allow rules and
`defaultMode` cannot leak in and nothing that would prompt is approved — as in
a headless Action. Cases A–D put a model in the loop (it is asked to run one
exact command); the verdict is read from `permission_denials`, not from prose.

`fixture/` is a PUBLIC run (`actions/checkout`), logs cut to 40 lines, recorded
by `record-fixture.sh`, which refuses a private repository: this repository is
public, and its consumers' CI logs must never be committed here.

`evidence-offline.txt` / `evidence-live.txt` are the outputs this PR was opened with.
