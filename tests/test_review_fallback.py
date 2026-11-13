# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""A usage limit on the review's primary token retries once on an optional fallback token (airia-ci#139).

The four paths are the shipped steps, sliced out of the workflow and the claude-limit action: the primary run
was a review; the primary run hit a limit and the fallback run is a review; both runs hit a limit; the fallback
secret is absent. A limit is never posted. The fallback step's `if` is false without the secret and when the
primary run was not at its limit. The both-limits error names both reset times.

Run: python3 -m unittest discover -s tests -p test_review_fallback.py
"""

from __future__ import annotations

import importlib.util
import json
import unittest

import yaml
from steps import ROOT, Runner, step

WORKFLOW = ROOT / ".github/workflows/reusable-claude-review.yml"
GATE = ROOT / ".github/workflows/reusable-python-gate.yml"
ACTION = ROOT / ".github/actions/claude-limit/action.yml"
POST = ROOT / ".github/actions/review-post/action.yml"
RESPOND = ROOT / ".github/workflows/reusable-claude-respond.yml"
_spec = importlib.util.spec_from_file_location("claude_limit", ROOT / ".github/actions/claude-limit/claude_limit.py")
claude_limit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(claude_limit)
_init = importlib.util.spec_from_file_location("init", ROOT / "scripts/init.py")
init = importlib.util.module_from_spec(_init)
_init.loader.exec_module(init)

TOKEN = "tok-fallback-must-not-print"
PRIMARY_RESET = "2026-11-11 08:00 UTC"
FALLBACK_WHEN = "7pm (Europe/Berlin)"
NO_RESET = "no reset time"
SECRET_LINE = "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK }}"
SHARED_PROMPT = "${{ env.REVIEW_PROMPT }}"


def limit_run(text, resets_at=None):
    """An unpaid run whose text or rejected event is a usage limit. The prose must never reach a log."""
    msgs = []
    if resets_at is not None:
        msgs.append({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": resets_at}})
    msgs.append({"type": "result", "subtype": "success", "num_turns": 1, "is_error": False, "total_cost_usd": 0,
                 "modelUsage": {}, "result": text})
    return msgs


def review_run():
    body = '## Review\n\nNothing to change.\n\n```airia-findings\n{"findings": []}\n```'
    return [{"type": "result", "subtype": "success", "num_turns": 3, "is_error": False, "total_cost_usd": 0.02,
             "modelUsage": {"m": {"inputTokens": 10}}, "permission_denials": [], "result": body}]


def write_execution(runner, msgs):
    path = runner.tmp / "execution.json"
    path.write_text(json.dumps(msgs))
    return path


class TheContract(unittest.TestCase):
    """What a caller sees: the optional secret, one prompt for both runs, and no change to @claude."""

    def workflow(self):
        return yaml.safe_load(WORKFLOW.read_text())

    def test_the_workflow_declares_the_optional_secret_and_the_header_shows_it(self):
        secrets = self.workflow()[True]["workflow_call"]["secrets"]
        # Not required at the call, so a caller without it gets the titled No Claude credential error (airia-ci#143).
        self.assertIs(secrets["CLAUDE_CODE_OAUTH_TOKEN"]["required"], False)
        self.assertIs(secrets["CLAUDE_CODE_OAUTH_TOKEN_FALLBACK"]["required"], False)
        # "organization:" contains the letters on:, so split on the workflow key at the start of a line.
        header = WORKFLOW.read_text().split("\non:", 1)[0]
        self.assertIn(SECRET_LINE, header)

    def test_init_writes_the_secret_on_the_review_callers_and_not_on_the_responder(self):
        review = init.REVIEW_JOB.format(needs="gates", harness=init.HARNESS, tag=init.TAG, runner=init.THOR)
        gate = init.PYTHON_GATE.format(branch="main", harness=init.HARNESS, tag=init.TAG, runner=init.THOR)
        respond = init.RESPOND.format(harness=init.HARNESS, tag=init.TAG, runner=init.THOR, gate="ci.yml")
        self.assertIn(SECRET_LINE, review)
        self.assertIn(SECRET_LINE, gate)
        self.assertNotIn("FALLBACK", respond)
        self.assertNotIn("FALLBACK", RESPOND.read_text(), "the @claude responder is not part of this contract")

    def test_the_python_gate_forwards_the_optional_secret(self):
        # The gate calls the review workflow; a secret the caller passes must be declared and forwarded there.
        doc = yaml.safe_load(GATE.read_text())
        self.assertIs(doc[True]["workflow_call"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN_FALLBACK"]["required"], False)
        self.assertEqual(doc["jobs"]["claude-review"]["secrets"]["CLAUDE_CODE_OAUTH_TOKEN_FALLBACK"],
                         "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK }}")

    def test_the_prompt_is_one_job_env_and_both_runs_read_it_with_the_same_inputs(self):
        # The same prompt and the same inputs: only the token differs, and nothing copies the prompt text.
        prompt = self.workflow()["jobs"]["claude-review"]["env"]["REVIEW_PROMPT"]
        for fragment in ("REPO: ${{ github.repository }}", "PR NUMBER: ${{ inputs.pr-number }}", "```airia-findings",
                         '{"findings": []}', "${{ inputs.extra-prompt }}"):
            self.assertIn(fragment, prompt)
        primary = step(WORKFLOW, "Run Claude Code Review")["with"]
        fallback = step(WORKFLOW, "Run Claude Code Review on the fallback token")["with"]
        self.assertEqual(primary["prompt"], SHARED_PROMPT)
        self.assertEqual(fallback["prompt"], SHARED_PROMPT)
        self.assertEqual(primary["claude_code_oauth_token"], "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}")
        self.assertEqual(fallback["claude_code_oauth_token"], "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK }}")
        self.assertEqual({k: v for k, v in primary.items() if k != "claude_code_oauth_token"},
                         {k: v for k, v in fallback.items() if k != "claude_code_oauth_token"})
        self.assertEqual(step(WORKFLOW, "Run Claude Code Review on the fallback token")["uses"],
                         step(WORKFLOW, "Run Claude Code Review")["uses"])

    def test_the_fallback_run_is_gated_on_the_retry_output_and_a_primary_limit_still_reaches_it(self):
        fallback = step(WORKFLOW, "Run Claude Code Review on the fallback token")
        self.assertEqual(fallback["if"], "always() && steps.fallback.outputs.retry == 'true'")
        primary = step(WORKFLOW, "Run Claude Code Review")
        # With the secret the primary may fail and the job still goes green on the fallback's review; without it
        # the expression is false and a primary failure fails the job as it does today.
        self.assertEqual(primary["continue-on-error"], "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK != '' }}")
        decide = step(WORKFLOW, "Decide whether to retry on the fallback token")
        self.assertEqual(decide["if"],
                         "always() && steps.once.outputs.review == 'true' && steps.claude-review.outcome != 'skipped'")
        self.assertEqual(decide["uses"], "AIRIA-Systems-GmbH/airia-ci/.github/actions/claude-limit@main")
        self.assertEqual(decide["with"], {"execution-file": "${{ steps.claude-review.outputs.execution_file }}",
                                          "fallback-token": "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK }}",
                                          "mode": "decide"})
        self.assertNotIn("env", decide, "the token travels once, as the action's input")

    def test_posting_uses_the_run_that_answered_and_skips_a_double_limit(self):
        both = step(WORKFLOW, "Report both usage limits")
        self.assertEqual(both["if"],
                         "always() && steps.fallback.outputs.retry == 'true' && steps.claude-review-fallback.outcome != 'skipped'")
        self.assertEqual(both["with"], {"execution-file": "${{ steps.claude-review-fallback.outputs.execution_file }}",
                                        "primary-reset": "${{ steps.fallback.outputs.primary-reset }}", "mode": "both"})
        post = step(WORKFLOW, "Post the review")
        self.assertEqual(post["with"]["execution-file"],
                         "${{ steps.fallback.outputs.retry == 'true' && steps.claude-review-fallback.outputs.execution_file"
                         " || steps.claude-review.outputs.execution_file }}")
        for clause in ("always()", "steps.once.outputs.review == 'true'", "steps.claude-review.outcome != 'skipped'",
                       "steps.both-limits.outcome != 'failure'"):
            self.assertIn(clause, post["if"])

    def test_the_action_passes_the_token_through_the_environment_and_never_echoes_it(self):
        inner = step(ACTION, "Decide the fallback retry")
        self.assertEqual(inner["env"]["FALLBACK_TOKEN"], "${{ inputs.fallback-token }}")
        self.assertNotIn("echo", inner["run"])
        self.assertNotIn("${{", inner["run"])
        self.assertEqual(step(ACTION, "Classify the failure")["if"], "inputs.mode == 'say'")
        self.assertEqual(yaml.safe_load(ACTION.read_text())["inputs"]["mode"]["default"], "say")


class Decide(unittest.TestCase):
    def decide(self, msgs, token):
        r = Runner(self)
        env = {"EXECUTION_FILE": str(write_execution(r, msgs))}
        if token is not None:
            env["FALLBACK_TOKEN"] = token
        p = r.run(ACTION, "Decide the fallback retry", **env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(TOKEN, p.stdout + p.stderr)
        if r.output.exists():
            self.assertNotIn(TOKEN, r.output.read_text())
        return r.outputs()

    def test_a_primary_review_sets_nothing_even_when_the_secret_is_present(self):
        # The fallback must not run when the primary run was not at its limit.
        self.assertEqual(self.decide(review_run(), TOKEN), {})

    def test_a_paid_review_that_mentions_a_rate_limit_is_an_answer_not_a_limit(self):
        # This repository's reviews say "usage limit" in every other sentence; a retry would replace the review.
        paid = review_run()
        paid[0]["result"] = "The PR handles the Claude usage limit well.\n\n```airia-findings\n{\"findings\": []}\n```"
        self.assertEqual(self.decide(paid, TOKEN), {})

    def test_a_failed_run_whose_error_names_a_limit_sets_retry(self):
        failed = [{"type": "result", "subtype": "error_during_execution", "num_turns": 4, "is_error": True,
                   "total_cost_usd": 0.1, "modelUsage": {"m": {}}, "result": "You've hit your limit · resets 3pm"}]
        self.assertEqual(self.decide(failed, TOKEN), {"retry": "true", "primary-reset": "3pm"})

    def test_a_primary_limit_with_the_secret_sets_retry_and_the_reset_phrase(self):
        outputs = self.decide(limit_run("You've hit your limit · resets Nov 11 at 8am", resets_at=1794384000), TOKEN)
        self.assertEqual(outputs, {"retry": "true", "primary-reset": PRIMARY_RESET})

    def test_a_primary_limit_without_a_reset_time_records_no_reset_time(self):
        outputs = self.decide(limit_run("Claude usage limit reached. Please try again later."), TOKEN)
        self.assertEqual(outputs, {"retry": "true", "primary-reset": NO_RESET})

    def test_a_primary_limit_with_no_secret_sets_nothing(self):
        # The fallback must not run when the caller did not pass the secret: the job behaves exactly as today.
        self.assertEqual(self.decide(limit_run("You've hit your limit · resets 7pm (Europe/Berlin)"), ""), {})
        self.assertEqual(self.decide(limit_run("You've hit your limit · resets 7pm (Europe/Berlin)"), None), {})


class BothLimits(unittest.TestCase):
    def both(self, msgs, primary=PRIMARY_RESET):
        r = Runner(self)
        env = {"EXECUTION_FILE": str(write_execution(r, msgs))}
        if primary is not None:
            env["PRIMARY_RESET"] = primary
        p = r.run(ACTION, "Report both usage limits", **env)
        self.assertEqual(r.calls(), [], "a limit failure is never posted as a review")
        self.assertNotIn("ACCOUNT-PRIVATE", p.stdout + p.stderr)
        return p

    def test_a_fallback_that_answered_prints_nothing_and_exits_zero(self):
        p = self.both(review_run())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "")

    def test_both_limits_are_one_error_naming_both_reset_times(self):
        p = self.both(limit_run(f"ACCOUNT-PRIVATE hit your limit · resets {FALLBACK_WHEN}"))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("::error title=Claude usage limit::"), 1)
        self.assertIn("Both accounts are at their limit.", p.stdout)
        self.assertIn(f"Primary reset: {PRIMARY_RESET}.", p.stdout)
        self.assertIn(f"Fallback reset: {FALLBACK_WHEN}.", p.stdout)
        self.assertIn(claude_limit.TOKEN_OK, p.stdout)

    def test_a_missing_reset_time_is_named_no_reset_time(self):
        p = self.both(limit_run("Claude usage limit reached. Please try again later."), primary=None)
        self.assertEqual(p.returncode, 1)
        self.assertIn(f"Primary reset: {NO_RESET}.", p.stdout)
        self.assertIn(f"Fallback reset: {NO_RESET}.", p.stdout)
        self.assertIn(claude_limit.TOKEN_OK, p.stdout)


class WhatGetsPosted(unittest.TestCase):
    """The post step receives the execution file of the run that answered; a limit file is never a review."""

    def post(self, msgs):
        rules = [
            [r"^api --paginate repos/o/r/pulls/9/comments", {"json": []}],
            [r"^api repos/o/r/pulls/9 ", {"json": {"head": {"sha": "c" * 40}}}],
            [r"^api --method POST repos/o/r/pulls/9/reviews ", {"json": {"id": 7}}],
            [r"^pr comment 9 ", {"text": "https://github.com/o/r/pull/9#c1\n"}],
        ]
        r = Runner(self, rules)
        exe = write_execution(r, msgs)
        p = r.run(POST, "Post the review", PR="9", RUN_ID="42", EXECUTION_FILE=str(exe),
                  RUN_URL="https://github.com/o/r/actions/runs/42", HEAD_SHA="c" * 40, CLAUDE_VERSION="1")
        summaries = [c["files"].get("--body-file", "") for c in r.calls() if c["args"][:2] == ["pr", "comment"]]
        return p, summaries

    def test_a_review_is_posted_and_a_limit_is_not(self):
        posted, summaries = self.post(review_run())
        self.assertEqual(posted.returncode, 0, posted.stderr)
        self.assertEqual(len(summaries), 1)
        self.assertNotIn("usage limit", summaries[0])
        limited, summaries = self.post(limit_run("ACCOUNT-PRIVATE usage limit reached|1794384000"))
        self.assertEqual(limited.returncode, 1)
        self.assertEqual(summaries, [], "a limit failure is never posted as a review")
        self.assertIn("::error title=Claude usage limit::", limited.stdout)
        self.assertNotIn("Both accounts are at their limit.", limited.stdout)
        self.assertNotIn("ACCOUNT-PRIVATE", limited.stdout)


if __name__ == "__main__":
    unittest.main()
