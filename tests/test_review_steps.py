# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The shared review workflow's shipped steps, against a fake `gh`.

- "Skip if this PR already has its automatic review": once per PR, and only a
  re-run attempt of the run that wrote the review asks for a refresh;
- "Post the review": the workflow posts Claude's final message, and a run that
  produced none fails loud instead of going green having posted nothing;
- the credential, git-exclude and staging steps around them.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from steps import ROOT, Runner, bare, heredoc

WORKFLOW = ROOT / ".github/workflows/reusable-claude-review.yml"
FOOTER = "_Automatic review by `claude-code-review.yml`"


def review(run_id: int, comment_id: int = 7) -> dict:
    body = f"Nothing blocking.\n\n---\n{FOOTER} — [run log](https://github.com/o/r/actions/runs/{run_id}), Claude Code 2.1.288._"
    return {"id": comment_id, "html_url": f"https://github.com/o/r/pull/9#c{comment_id}",
            "user": {"login": "github-actions[bot]"}, "body": body}


class OnceAsksForARefreshOnlyOnARerunOfTheSameRun(unittest.TestCase):
    def once(self, comments, run_id="42", attempt="2"):
        r = Runner(self, [[r"issues/9/comments", {"json": comments}]])
        p = r.run(WORKFLOW, "Skip if this PR already has its automatic review", PR="9", RUN_ID=run_id, ATTEMPT=attempt)
        self.assertEqual(p.returncode, 0, p.stderr)
        return r.outputs()

    def test_no_prior_review_reviews(self):
        self.assertEqual(self.once([]), {"review": "true"})

    def test_a_rerun_of_the_reviewing_run_refreshes(self):
        self.assertEqual(self.once([review(42)]), {"review": "false", "refresh": "true", "comment-id": "7"})

    def test_a_new_push_still_only_skips(self):
        # A new commit is a new run: the once-per-PR rule is unchanged.
        self.assertEqual(self.once([review(41)], run_id="42", attempt="1"), {"review": "false"})
        self.assertEqual(self.once([review(41)], run_id="42", attempt="2"), {"review": "false"})

    def test_a_run_id_that_is_a_prefix_does_not_match(self):
        self.assertEqual(self.once([review(4200)], run_id="42"), {"review": "false"})

    def test_someone_elses_comment_is_not_a_review(self):
        other = {**review(42), "user": {"login": "a-human"}}
        self.assertEqual(self.once([other]), {"review": "true"})


class RequireTheCredential(unittest.TestCase):
    def test_a_missing_token_fails_with_the_fix(self):
        p = Runner(self).run(WORKFLOW, "Require the Claude credential", HAS_CLAUDE_TOKEN="false")
        self.assertEqual(p.returncode, 1)
        self.assertIn("gh secret set CLAUDE_CODE_OAUTH_TOKEN -R o/r", p.stdout)

    def test_a_present_token_passes(self):
        self.assertEqual(Runner(self).run(WORKFLOW, "Require the Claude credential", HAS_CLAUDE_TOKEN="true").returncode, 0)


class StagingForClaude(unittest.TestCase):
    """ci-results/ lives inside the checkout, where Claude can read it, and out of git."""

    def checkout(self, r):
        repo = r.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        return repo

    def test_ci_results_never_shows_in_git_status(self):
        r = Runner(self)
        repo = self.checkout(r)
        self.assertEqual(r.run(WORKFLOW, "Keep ci-results/ out of git", cwd=repo).returncode, 0)
        (repo / "ci-results").mkdir()
        (repo / "ci-results/jobs.md").write_text("x\n")
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo,
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(status, "")

    def test_the_toolchain_status_is_staged_beside_the_gate_results(self):
        r = Runner(self)
        repo = self.checkout(r)
        (r.tmp / "claude-toolchain-status.md").write_text("uv 0.9 provisioned\n")
        p = r.run(WORKFLOW, "Stage the toolchain status for Claude", cwd=repo, GITHUB_WORKSPACE=str(repo))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((repo / "ci-results/claude-toolchain-status.md").read_text(), "uv 0.9 provisioned\n")

    def test_no_toolchain_status_still_creates_the_directory(self):
        r = Runner(self)
        repo = self.checkout(r)
        p = r.run(WORKFLOW, "Stage the toolchain status for Claude", cwd=repo, GITHUB_WORKSPACE=str(repo))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(list((repo / "ci-results").iterdir()), [])


def execution(result: dict | None, *, denials=()) -> list:
    msgs = [{"type": "system"}, {"type": "assistant"}]
    if result is not None:
        msgs.append({"type": "result", "subtype": "success", "num_turns": 12, "is_error": False,
                     "permission_denials": list(denials), **result})
    return msgs


class PostTheReview(unittest.TestCase):
    """The workflow posts, not the model: a run that ends without a review fails loud."""

    STEP = "Post the review"

    def post(self, msgs, version="2.1.300"):
        r = Runner(self, [[r"^pr comment 9 ", {"text": "https://github.com/o/r/pull/9#c1\n"}]])
        exe = r.tmp / "execution.json"
        if msgs is not None:
            exe.write_text(json.dumps(msgs))
        p = r.run(WORKFLOW, self.STEP, PR="9", CLAUDE_VERSION=version, EXECUTION_FILE=str(exe),
                  RUN_URL="https://github.com/o/r/actions/runs/42")
        posted = [c["files"]["--body-file"] for c in r.calls() if c["args"][:2] == ["pr", "comment"]]
        return p, posted

    def test_the_final_message_is_posted_with_the_contract_footer(self):
        p, posted = self.post(execution({"result": "## Review\n\nLooks right."}))
        self.assertEqual(p.returncode, 0, p.stderr)
        (body,) = posted
        self.assertTrue(body.startswith("## Review\n\nLooks right.\n\n---\n"))
        # The once-per-PR check keys on this footer: it is part of the contract.
        self.assertIn(FOOTER + " — [run log](https://github.com/o/r/actions/runs/42), Claude Code 2.1.300._", body)

    def test_no_execution_file_fails_and_posts_nothing(self):
        p, posted = self.post(None)
        self.assertEqual((p.returncode, posted), (1, []))
        self.assertIn("::error title=No review posted::", p.stdout)
        self.assertIn("Claude did not run to completion", p.stdout)

    def test_an_empty_final_message_fails_and_posts_nothing(self):
        p, posted = self.post(execution({"result": "   "}))
        self.assertEqual((p.returncode, posted), (1, []))

    def test_an_errored_run_fails_and_posts_nothing(self):
        p, posted = self.post(execution({"result": "partial", "is_error": True}))
        self.assertEqual((p.returncode, posted), (1, []))

    def test_a_run_without_a_result_message_fails(self):
        p, posted = self.post(execution(None))
        self.assertEqual((p.returncode, posted), (1, []))


class PostTheReviewParser(unittest.TestCase):
    """The Python inside "Post the review", run as the step runs it: `python3 - <execution> <body>`."""

    def parse(self, msgs, version=""):
        with tempfile.TemporaryDirectory() as t:
            exe, body = Path(t) / "execution.json", Path(t) / "body.md"
            exe.write_text(json.dumps(msgs))
            env = bare(PATH="/usr/bin:/bin", RUN_URL="https://run", CLAUDE_VERSION=version)
            p = subprocess.run([sys.executable, str(heredoc(WORKFLOW, "Post the review")), str(exe), str(body)],
                               env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout, body.read_text() if body.exists() else None

    def test_denials_are_listed_first_thing_to_read_when_a_review_is_thin(self):
        denials = [{"tool_name": "Bash", "tool_input": {"command": f"curl {i}"}} for i in range(25)]
        out, body = self.parse(execution({"result": "ok"}, denials=denials))
        self.assertIn("permission denials: 25", out)
        self.assertIn('- Bash: {"command": "curl 0"}', out)
        self.assertEqual(out.count("- Bash:"), 20, "at most twenty are listed")
        self.assertIsNotNone(body)

    def test_the_last_result_message_wins(self):
        msgs = execution({"result": "first"}) + [{"type": "result", "result": "second", "is_error": False}]
        _, body = self.parse(msgs)
        self.assertTrue(body.startswith("second\n"))

    def test_no_version_says_the_action_installed_its_own(self):
        _, body = self.parse(execution({"result": "ok"}))
        self.assertIn("Claude Code installed by the action._", body)

    def test_an_errored_or_empty_result_writes_no_body_so_nothing_is_posted(self):
        # The step posts only a non-empty body file, and otherwise fails as
        # UNREVIEWED: an error text or a blank answer must never reach the PR
        # as if it were a review.
        for result in ({"result": "API Error: overloaded", "is_error": True}, {"result": "  \n"}, {}):
            with self.subTest(result=result):
                out, body = self.parse(execution(result))
                self.assertIn("No review text in the final message.", out)
                self.assertIsNone(body)

    def test_a_corrupt_execution_file_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as t:
            exe = Path(t) / "execution.json"
            exe.write_text("{not json")
            p = subprocess.run([sys.executable, str(heredoc(WORKFLOW, "Post the review")), str(exe), str(Path(t) / "b")],
                               env=bare(RUN_URL="u"), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("No execution file", p.stdout)


if __name__ == "__main__":
    unittest.main()
