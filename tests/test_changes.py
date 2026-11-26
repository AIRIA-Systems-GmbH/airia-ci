# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The `changes` action says `code=false` only when every changed path is one a lane audited as skippable (airia-ci#170).

A wrong `false` skips a lane's tests on a change that needed them, so each unclear case is pinned to `true`.
The shipped action step and its script run against a fake `gh`.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

import yaml
from steps import ROOT, Runner

ACTION = ROOT / ".github/actions/changes/action.yml"
SCRIPT = ROOT / ".github/actions/changes/changes.sh"
STEP = "Classify the changed paths"
FILES = r"repos/o/r/pulls/7/files"
DOCS = "*.md\ndocs/**"
# A records-aware lane lists the record paths it audited, never a bare `*.md` (that matches every directory).
RECORDS = "README.md docs/** .airia/memory/task/*.md .airia/memory/issue/*.md"


def listing(*names, renamed=None):
    out = [{"filename": n} for n in names]
    if renamed:
        out += [{"filename": new, "previous_filename": old} for old, new in renamed]
    return out


class Changes(unittest.TestCase):
    def classify(self, files=None, skip=DOCS, event="pull_request", api=None):
        r = Runner(self, [[FILES, api or {"json": files}]])
        p = r.script(SCRIPT, EVENT=event, REPO="o/r", PR_NUMBER="7", SKIP_PATHS=skip)
        self.assertEqual(p.returncode, 0, p.stderr)
        return r.outputs()["code"], r.summary.read_text(), r

    def code(self, files, **kw):
        return self.classify(files, **kw)[0]

    def test_docs_only_is_not_code(self):
        code, summary, r = self.classify(listing("README.md", "docs/a/b.png", "HOWTO.md"))
        self.assertEqual(code, "false")
        self.assertIn("all 3 changed paths match", summary)
        self.assertEqual(len(r.summary.read_text().splitlines()), 1, "one summary line")

    def test_a_record_table(self):
        # The lane lists the records it audited as unread by its tests: only those are skippable.
        table = [
            ([".airia/memory/task/T-1.md"], "false"),
            ([".airia/memory/issue/I-1.md", "docs/x.md"], "false"),
            ([".airia/memory/plan/P-1.md"], "true"),  # a record kind the lane did not list
            ([".airia/config/settings.json"], "true"),
            ([".airia/memory/task/T-1.md", "src/app.py"], "true"),
        ]
        for names, want in table:
            with self.subTest(names=names):
                self.assertEqual(self.code(listing(*names), skip=RECORDS), want)

    def test_a_mixed_change_is_code_and_the_summary_counts_the_match(self):
        code, summary, _ = self.classify(listing("README.md", "src/app.py"))
        self.assertEqual(code, "true")
        self.assertIn("1 of 2 changed paths match", summary)

    def test_a_rename_counts_both_sides(self):
        # A code file renamed to .md: its new path is docs, its old one is not.
        self.assertEqual(self.code(listing(renamed=[("src/app.py", "docs/app.md")])), "true")
        self.assertEqual(self.code(listing(renamed=[("docs/a.md", "docs/b.md")])), "false")

    def test_a_pattern_is_a_git_pathspec(self):
        # `*` crosses `/` by default and `:(glob)` stops at it, as in reusable-python-gate's docs-paths.
        files = listing("a/b/c.md")
        self.assertEqual(self.code(files, skip="*.md"), "false")
        self.assertEqual(self.code(files, skip=":(glob)*.md"), "true")
        self.assertEqual(self.code(files, skip=":(glob)**/*.md"), "false")
        self.assertEqual(self.code(files, skip="a"), "false", "a directory covers what is under it")

    def test_3000_files_is_code_because_the_api_stops_there(self):
        self.assertEqual(self.code(listing(*[f"docs/{i}.md" for i in range(3000)])), "true")
        self.assertEqual(self.code(listing(*[f"docs/{i}.md" for i in range(2999)])), "false")

    def test_a_failed_read_is_code(self):
        code, summary, _ = self.classify(api={"stderr": "HTTP 502", "exit": 1})
        self.assertEqual(code, "true")
        self.assertIn("could not be read", summary)

    def test_an_empty_list_is_code(self):
        self.assertEqual(self.code([]), "true")
        self.assertEqual(self.classify(api={"text": ""})[0], "true")

    def test_an_empty_skip_paths_is_code(self):
        for skip in ("", "  \n ", None):
            with self.subTest(skip=skip):
                r = Runner(self, [[FILES, {"json": listing("README.md")}]])
                env = {} if skip is None else {"SKIP_PATHS": skip}
                r.script(SCRIPT, EVENT="pull_request", REPO="o/r", PR_NUMBER="7", **env)
                self.assertEqual(r.outputs()["code"], "true")
                self.assertEqual(r.calls(), [], "no skip-paths: the API is not even asked")

    def test_other_events_are_code(self):
        for event in ("push", "merge_group", "workflow_dispatch", "schedule", ""):
            with self.subTest(event=event):
                code, _, r = self.classify(listing("README.md"), event=event)
                self.assertEqual(code, "true")
                self.assertEqual(r.calls(), [])

    def test_a_missing_pull_request_number_is_code(self):
        r = Runner(self, [[FILES, {"json": listing("README.md")}]])
        r.script(SCRIPT, EVENT="pull_request", REPO="o/r", PR_NUMBER="", SKIP_PATHS=DOCS)
        self.assertEqual(r.outputs()["code"], "true")

    def test_a_pattern_git_refuses_is_code(self):
        self.assertEqual(self.code(listing("README.md"), skip=":(nosuchmagic)x"), "true")

    def test_an_unreadable_listing_is_code(self):
        self.assertEqual(self.classify(api={"text": "not json\n"})[0], "true")

    def test_awkward_paths_are_code_not_a_wrong_false(self):
        for name in ("docs/a\nb.md", "docs/a\rb.md"):
            with self.subTest(name=repr(name)):
                self.assertEqual(self.code(listing(name)), "true")

    def test_a_file_that_becomes_a_directory_is_code(self):
        self.assertEqual(self.code(listing("docs", "docs/x.md")), "true")

    def test_two_pages_are_read(self):
        # `--paginate` is passed, so GitHub's own paging reaches the filter.
        _, _, r = self.classify(listing("README.md"))
        (call,) = r.calls()
        self.assertEqual(call["args"][:3], ["api", "--paginate", "repos/o/r/pulls/7/files"])

    def test_the_shipped_step_hands_the_action_inputs_to_the_script(self):
        # The step in action.yml is what runs on GitHub: it must pass every input the script reads.
        step = yaml.safe_load(ACTION.read_text())["runs"]["steps"][0]
        self.assertEqual(set(step["env"]), {"EVENT", "REPO", "PR_NUMBER", "SKIP_PATHS", "GH_TOKEN"})
        self.assertEqual(step["env"]["GH_TOKEN"], "${{ inputs.github-token }}")
        self.assertEqual(yaml.safe_load(ACTION.read_text())["inputs"]["github-token"]["default"], "${{ github.token }}")
        r = Runner(self, [[FILES, {"json": listing("docs/a.md")}]])
        p = r.run(ACTION, STEP, EVENT="pull_request", REPO="o/r", PR_NUMBER="7", SKIP_PATHS=DOCS)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(r.outputs()["code"], "false")


if __name__ == "__main__":
    unittest.main()
