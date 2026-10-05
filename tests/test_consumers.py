# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""scripts/consumers.py against a fake `gh`.

What these tests hold: a pin older than the newest ci-vN is reported BEHIND
and fails the run, because moving the tag never reaches it; the newest major
is found by number, not by spelling; a lookup that fails is an error, never
an empty "all current".

Run: python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

from steps import ROOT, Runner

SCRIPT = ROOT / "scripts/consumers.py"
spec = importlib.util.spec_from_file_location("consumers", SCRIPT)
consumers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(consumers)

TAGS = [r"airia-ci/tags", {"json": [{"name": n} for n in ("ci-v2", "ci-v10", "ci-v3", "ci-vnext", "ci-v10.1")]}]
CALLER = "uses: AIRIA-Systems-GmbH/airia-ci/.github/workflows/reusable-claude-review.yml@{}\n"


class Consumers(unittest.TestCase):
    def run_main(self, rules, *owners):
        r = Runner(self, rules)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, r.env()), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = consumers.main(list(owners or ["o"]))
        return code, out.getvalue(), err.getvalue(), r

    def test_a_frozen_pin_is_behind_and_fails_the_run(self):
        code, out, _, _ = self.run_main([
            TAGS,
            ["repo list o", {"json": [{"nameWithOwner": "o/new"}, {"nameWithOwner": "o/old"}]}],
            ["o/(new|old)/contents/.github/workflows --jq", {"json": [{"path": ".github/workflows/ci.yml"}]}],
            ["o/new/contents/.github/workflows/ci.yml", {"text": CALLER.format("ci-v10")}],
            ["o/old/contents/.github/workflows/ci.yml", {"text": CALLER.format("ci-v2")}],
        ])
        self.assertEqual(code, 1)
        self.assertIn("o/new\t.github/workflows/ci.yml\treusable-claude-review.yml@ci-v10\tcurrent", out)
        self.assertIn("o/old\t.github/workflows/ci.yml\treusable-claude-review.yml@ci-v2\tBEHIND (newest is ci-v10)", out)
        self.assertIn("2 consumers, 2 pins, 1 behind ci-v10", out)

    def test_all_current_passes_and_skips_repos_without_workflows_and_airia_ci_itself(self):
        code, out, _, r = self.run_main([
            TAGS,
            ["repo list o", {"json": [{"nameWithOwner": n} for n in
                                      ("o/a", "o/empty", "AIRIA-Systems-GmbH/airia-ci")]}],
            ["o/empty/contents", {"exit": 1, "stderr": "Not Found"}],
            ["o/a/contents/.github/workflows --jq", {"json": [{"path": ".github/workflows/ci.yml"}]}],
            ["o/a/contents/.github/workflows/ci.yml", {"text": CALLER.format("ci-v10")}],
        ])
        self.assertEqual(code, 0)
        self.assertIn("1 consumers, 1 pins, 0 behind ci-v10", out)
        self.assertFalse([c for c in r.calls() if "airia-ci/contents" in " ".join(c["args"])],
                         "airia-ci's own workflows are not a consumer")

    def test_an_unreadable_owner_is_an_error_not_an_empty_list(self):
        code, out, err, _ = self.run_main([TAGS, ["repo list", {"exit": 1, "stderr": "HTTP 401"}]])
        self.assertEqual(code, 2)
        self.assertIn("cannot list o's repositories: HTTP 401", err)
        self.assertEqual(out, "")

    def test_an_unreadable_workflow_is_an_error(self):
        code, _, err, _ = self.run_main([
            TAGS,
            ["repo list o", {"json": [{"nameWithOwner": "o/a"}]}],
            ["o/a/contents/.github/workflows --jq", {"json": [{"path": ".github/workflows/ci.yml"}]}],
            ["o/a/contents/.github/workflows/ci.yml", {"exit": 1, "stderr": "HTTP 403"}],
        ])
        self.assertEqual(code, 2)
        self.assertIn("cannot read o/a/.github/workflows/ci.yml: HTTP 403", err)

    def test_no_release_tag_or_no_tag_list_is_an_error(self):
        code, _, err, _ = self.run_main([[r"airia-ci/tags", {"json": [{"name": "ci-vnext"}]}]])
        self.assertEqual(code, 2)
        self.assertIn("has no ci-vN tag", err)
        code, _, err, _ = self.run_main([[r"airia-ci/tags", {"exit": 1, "stderr": "HTTP 502"}]])
        self.assertEqual(code, 2)
        self.assertIn("cannot list AIRIA-Systems-GmbH/airia-ci tags: HTTP 502", err)


if __name__ == "__main__":
    unittest.main()
