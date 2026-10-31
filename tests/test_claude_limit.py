# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The @claude responder says a Claude usage limit is what failed it (airia-ci#103, part b).

The classifier is the one the automatic review uses (#104); this pins it on the responder's path, with
fixture execution files, and pins that both callers resolve the shared module the way GitHub lays the
repository out. No branch run of the responder can exist, so these are its proof before the canary.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import unittest

import yaml
from steps import ROOT, Runner, step

ACTION = ROOT / ".github/actions/claude-limit/action.yml"
_spec = importlib.util.spec_from_file_location("claude_limit", ROOT / ".github/actions/claude-limit/claude_limit.py")
claude_limit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(claude_limit)
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

    def test_the_text_after_the_reset_time_never_reaches_the_job_log(self):
        # airia-ci#113: the job log is public to the repo's readers; only the reset time may leave the execution file.
        out = self.say(execution("You've hit your limit · resets 7pm (Europe/Berlin) and the account id 4711 is shown"))
        self.assertIn("the limit resets 7pm (Europe/Berlin); " + RETRY, out,
                      "the annotation names the reset time and nothing after it")
        self.assertNotIn("account", out, "the text after the reset time leaked into the public job log")
        self.assertNotIn("4711", out, "the text after the reset time leaked into the public job log")

    def test_a_plain_reset_time_followed_by_prose_prints_only_the_time(self):
        # airia-ci#113: "resets 3pm" stays "3pm" however the sentence goes on.
        out = self.say(execution("5-hour limit reached ∙ resets 3pm and then more words"))
        self.assertIn("the limit resets 3pm; " + RETRY, out, "only the reset time leaves the execution file")
        self.assertNotIn("more words", out, "the text after the reset time leaked into the public job log")

    def test_a_resets_phrase_that_is_not_a_time_is_no_reset_time(self):
        # airia-ci#113: a digit after "resets" does not make the phrase a time; printing it would leak the text.
        out = self.say(execution("5-hour limit reached. The daemon resets 2 workers in repo owner/private-thing then stops"))
        self.assertIn("the text gives no reset time; " + RETRY, out, "a phrase not shaped like a time is not a reset time")
        self.assertNotIn("workers", out, "the text after 'resets' leaked into the public job log")
        self.assertNotIn("private-thing", out, "a private repository name leaked into the public job log")

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


class TheResetTime(unittest.TestCase):
    """airia-ci#113: what reset_time returns is printed into a public job log, in a one-line ::error annotation.

    So it must be the reset time and nothing else (no prose, no repository name, no newline), and a real
    reset time in any shape Claude prints must not be lost: losing it drops the one fact the reader needs.
    """

    CASES = (
        # Real reset times: each must come back whole, and only it.
        ("5-hour limit reached ∙ resets 3pm", "3pm"),
        ("You've hit your limit · resets 7pm (Europe/Berlin)", "7pm (Europe/Berlin)"),
        ("limit resets at 3:00 PM", "3:00 PM"),
        ("limit resets 15:00 UTC", "15:00 UTC"),
        ("limit resets on 2026-11-01 15:00", "2026-11-01 15:00"),
        ("limit resets 2026-11-01T15:00:00Z", "2026-11-01T15:00:00Z"),
        ("Opus weekly limit reached ∙ resets Oct 6, 1pm", "Oct 6, 1pm"),
        ("weekly limit reached, resets Oct 6", "Oct 6"),
        ("limit resets Monday 3pm", "Monday 3pm"),
        ("limit resets in 2 hours", "in 2 hours"),
        ("limit resets 3pm (America/New_York)", "3pm (America/New_York)"),
        # A newline must never enter the one-line ::error annotation.
        ("limit resets 3pm\n(Europe/Berlin)", "3pm"),
        # A parenthesis that is not an IANA zone (a repository name) is not printed.
        ("limit resets 3pm (owner/private-thing)", "3pm"),
        ("limit resets 3pm (Owner/Private)", "3pm"),
        # No time after "resets": nothing is printed rather than the text that follows.
        ("limit resets soon", None),
        ("the daemon resets 2 workers in repo owner/private-thing", None),
        # A word that is not a weekday or month is not part of a reset time.
        ("limit resets daemon 3pm", None),
    )

    def test_only_the_reset_time_is_returned_and_a_real_one_is_never_lost(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(claude_limit.reset_time([text]), expected)


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
