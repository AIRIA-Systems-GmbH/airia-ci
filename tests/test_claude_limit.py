# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The @claude responder says a Claude usage limit is what failed it (airia-ci#103, part b).

The classifier is the one the automatic review uses (#104); this pins it on the responder's path, with
fixture execution files, and pins that both callers resolve the shared module the way GitHub lays the
repository out. No branch run of the responder can exist, so these are its proof before the canary.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import shutil
import unittest

import yaml
from steps import ROOT, Runner, step

ACTION = ROOT / ".github/actions/claude-limit/action.yml"
RESPOND = ROOT / ".github/workflows/reusable-claude-respond.yml"
RETRY = "comment `@claude` again after the limit resets."
SECRET = "the model transcript must stay out of the log"


def execution(result: str, **extra) -> list:
    return [{"type": "system"}, {"type": "result", "subtype": "error_during_execution", "num_turns": 4,
                                 "is_error": True, "total_cost_usd": 0.1, "modelUsage": {"m": {}}, "result": result, **extra}]


class SayWhyClaudeFailed(unittest.TestCase):
    STEP = "Classify the failure"

    def say(self, msgs):
        r = Runner(self)
        exe = r.tmp / "execution.json"
        if msgs is not None:
            exe.write_text(msgs if isinstance(msgs, str) else json.dumps(msgs))
        p = r.run(ACTION, self.STEP, EXECUTION_FILE=str(exe))
        self.assertEqual(p.returncode, 0, "the job is already failing: this step never fails it, or hides why")
        return p.stdout

    def test_a_limit_with_a_reset_time_is_titled_and_gives_the_reset(self):
        out = self.say(execution(f"{SECRET}. You've hit your limit · resets 7pm (Europe/Berlin)"))
        self.assertIn("::error title=Claude usage limit::", out)
        self.assertIn("the limit resets 7pm (Europe/Berlin); " + RETRY, out)
        self.assertNotIn(SECRET, out, "stdout is the job log: only the reset time leaves the file")
        self.assertNotIn("hit your limit", out)

    def test_the_cli_epoch_form_is_a_reset_time_in_utc(self):
        out = self.say(execution("Claude AI usage limit reached|1761674400"))
        self.assertIn("the limit resets 2025-10-28 18:00 UTC; " + RETRY, out)

    def test_a_limit_without_a_reset_time_gives_the_retry_hint(self):
        out = self.say(execution("Claude usage limit reached. Please try again later."))
        self.assertIn("::error title=Claude usage limit::", out)
        self.assertIn("the text gives no reset time; " + RETRY, out)

    def test_an_api_rate_limit_in_the_transcript_is_a_limit(self):
        limited = {"type": "assistant", "error": "rate_limit",
                   "message": {"content": [{"type": "text", "text": "API Error: Rate limit reached"}]}}
        self.assertIn("title=Claude usage limit::", self.say([limited, *execution("")]))

    def test_the_zero_usage_signature_is_a_likely_limit_or_auth_failure(self):
        out = self.say(execution("", num_turns=1, total_cost_usd=0, modelUsage={}))
        self.assertIn("::error title=Likely Claude usage limit or auth failure::", out)
        self.assertIn("errored after 1 turn with no model usage and no cost", out)
        self.assertIn("if it is the limit, " + RETRY, out)

    def test_a_generic_error_prints_nothing(self):
        self.assertEqual(self.say(execution("Tool use was denied by the user")), "")

    def test_a_context_or_budget_limit_is_not_a_usage_limit(self):
        for text in ("Context limit reached", "Budget limit reached"):
            with self.subTest(text):
                self.assertEqual(self.say(execution(text)), "")

    def test_a_normal_success_prints_nothing(self):
        ok = [{"type": "result", "subtype": "success", "num_turns": 3, "is_error": False, "result": "Done."}]
        self.assertEqual(self.say(ok), "")

    def test_a_missing_or_corrupt_execution_file_is_a_generic_failure(self):
        for msgs in (None, "{not json", '{"type": "result"}'):
            with self.subTest(msgs):
                self.assertEqual(self.say(msgs), "")

    def test_a_non_object_message_is_skipped(self):
        self.assertIn("title=Claude usage limit::", self.say(["junk", *execution("5-hour limit reached, resets 3pm")]))


class WiredIntoTheResponder(unittest.TestCase):
    def test_it_runs_after_a_failed_claude_step_and_reads_its_execution_file(self):
        s = step(RESPOND, "Say why Claude failed")
        self.assertEqual(s["if"], "failure() && steps.claude.outcome == 'failure'")
        self.assertEqual(s["uses"], "AIRIA-Systems-GmbH/airia-ci/.github/actions/claude-limit@main")
        self.assertEqual(s["with"], {"execution-file": "${{ steps.claude.outputs.execution_file }}"})
        names = [x.get("name") for x in yaml.safe_load(RESPOND.read_text())["jobs"]["claude"]["steps"]]
        self.assertEqual(names[names.index("Run Claude Code") + 1], "Say why Claude failed")


class TheSharedModuleResolves(unittest.TestCase):
    """review-post imports the classifier from its sibling action directory, as a downloaded repository lays it out."""

    def test_the_review_still_names_the_limit_when_the_repo_is_somewhere_else(self):
        r = Runner(self)
        root = r.tmp / "_actions" / "AIRIA-Systems-GmbH" / "airia-ci" / "main"
        shutil.copytree(ROOT / ".github/actions", root / ".github/actions",
                        ignore=shutil.ignore_patterns("__pycache__"))
        exe = r.tmp / "execution.json"
        exe.write_text(json.dumps(execution("You've hit your limit · resets 7pm")))
        p = r.run(root / ".github/actions/review-post/action.yml", "Post the review", cwd=r.tmp / "bin",
                  PR="9", RUN_ID="1", RUN_URL="u", EXECUTION_FILE=str(exe))
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Claude usage limit::", p.stdout)
        self.assertIn("the limit resets 7pm", p.stdout)


if __name__ == "__main__":
    unittest.main()
