# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The shared review workflow's shipped steps, against a fake `gh`.

- "Skip if this PR already has its automatic review": once per PR, and only a
  re-run attempt of the run that wrote the review asks for a refresh;
- "Post the review": the workflow posts Claude's final message, and a run that
  produced none fails loud instead of going green having posted nothing;
- "Require the merge gate's results": a review that could read no gate job
  fails before Claude runs, instead of going green having verified nothing;
- the credential, git-exclude and staging steps around them.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import re
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
    """Once per head, not once per PR: the check is never green on a head no review covered (airia-ci#60)."""

    HEAD = "b" * 40

    def once(self, comments, run_id="42", attempt="2", heads=None, head=HEAD, pr_head=None):
        # `heads` maps a review's run id to the PR head that run was for; a missing id is an unreadable run.
        rules = [[r"issues/9/comments", {"json": comments}]]
        for rid, sha in (heads or {}).items():
            rules.append([rf"actions/runs/{rid}( |$)", {"json": {"id": rid, "head_sha": sha}}])
        rules.append([r"actions/runs/\d+( |$)", {"exit": 1, "stderr": "HTTP 404: Not Found"}])
        # `actions/runs/` with no id lists runs, as the real API does: `.head_sha` of the list is null.
        rules.append([r"actions/runs/( |$)", {"json": {"total_count": 0, "workflow_runs": []}}])
        if pr_head:
            rules.append([r"pulls/9( |$)", {"json": {"head": {"sha": pr_head}}}])
        r = Runner(self, rules)
        p = r.run(WORKFLOW, "Skip if this PR already has its automatic review", PR="9", RUN_ID=run_id,
                  ATTEMPT=attempt, HEAD_SHA=head)
        self.assertEqual(p.returncode, 0, p.stderr)
        return r.outputs()

    def test_no_prior_review_reviews(self):
        self.assertEqual(self.once([]), {"review": "true"})

    def test_a_rerun_of_the_reviewing_run_refreshes(self):
        self.assertEqual(self.once([review(42)]), {"review": "false", "refresh": "true", "comment-id": "7"})

    def test_a_push_after_the_review_reviews_the_new_head(self):
        # CryptoStudio#430: three commits after the review, and the check passed in 8 s on 250cdad9.
        old = "a" * 40
        expected = {"review": "true", "prior-comment-id": "7", "prior-head": old}
        self.assertEqual(self.once([review(41)], run_id="42", attempt="1", heads={41: old}), expected)
        self.assertEqual(self.once([review(41)], run_id="42", attempt="2", heads={41: old}), expected)

    def test_a_head_already_reviewed_by_another_run_is_skipped(self):
        # ready_for_review or a re-opened PR runs the gate again on the same head.
        self.assertEqual(self.once([review(41)], attempt="1", heads={41: self.HEAD}), {"review": "false"})

    def test_the_newest_review_is_the_one_compared(self):
        old, mid = "a" * 40, "c" * 40
        comments = [review(40, comment_id=5), review(41, comment_id=7)]
        self.assertEqual(self.once(comments, attempt="1", heads={40: old, 41: self.HEAD}), {"review": "false"})
        self.assertEqual(self.once(comments, attempt="1", heads={40: self.HEAD, 41: mid}),
                         {"review": "true", "prior-comment-id": "7", "prior-head": mid})

    def test_a_review_whose_run_cannot_be_read_reviews_again(self):
        # Never green on "could not tell": a deleted or unreadable run counts as another head.
        self.assertEqual(self.once([review(41)], attempt="1"),
                         {"review": "true", "prior-comment-id": "7", "prior-head": "unknown"})

    def test_a_footer_without_its_run_link_reviews_again(self):
        # An edited footer: no run to ask, so the head it covered is unknown, never "null".
        edited = {**review(41), "body": review(41)["body"].replace("/actions/runs/41)", ")")}
        self.assertEqual(self.once([edited], attempt="1", heads={41: self.HEAD}),
                         {"review": "true", "prior-comment-id": "7", "prior-head": "unknown"})

    def test_a_copied_footer_in_the_body_does_not_hide_the_real_run(self):
        # The review body may quote an earlier footer with its run link; the real one comes last.
        quoting = {**review(42), "body": "Quoted: [run log](https://github.com/o/r/actions/runs/41)\n" + review(42)["body"]}
        self.assertEqual(self.once([quoting]), {"review": "false", "refresh": "true", "comment-id": "7"})

    def test_without_the_event_head_the_prs_head_is_read(self):
        self.assertEqual(self.once([review(41)], attempt="1", heads={41: self.HEAD}, head="", pr_head=self.HEAD),
                         {"review": "false"})

    def test_a_run_id_that_is_a_prefix_does_not_match(self):
        self.assertEqual(self.once([review(4200)], run_id="42", heads={4200: self.HEAD}), {"review": "false"})

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


class RequireTheGateResults(unittest.TestCase):
    """The review exists to verify the gates; one that read none must not be green (airia-ci#24).

    AIRIA-DevTools PR #14 (run 37056603140): ci-results could not list the
    run's jobs, the reviewer said so as told, and Claude Review went green.
    """

    STEP = "Require the merge gate's results"

    def run_with(self, collected, jobs_md="# CI results\n"):
        r = Runner(self)
        (r.tmp / "ci-results").mkdir()
        (r.tmp / "ci-results/jobs.md").write_text(jobs_md)
        return r.run(WORKFLOW, self.STEP, GITHUB_WORKSPACE=str(r.tmp), GITHUB_RUN_ID="37056603140", COLLECTED=collected)

    def test_jobs_that_could_not_be_listed_fail_with_why_and_the_fix(self):
        p = self.run_with("0", "**The run's jobs could not be listed** (gh api exited non-zero)\n")
        self.assertEqual(p.returncode, 1)
        self.assertIn("could not be listed", p.stdout)
        self.assertIn("::error title=No CI results::", p.stdout)
        self.assertIn("gh run rerun 37056603140 --failed", p.stdout)

    def test_a_run_with_no_finished_job_fails(self):
        table = "| Job | Conclusion | Failed steps | Log |\n|---|---|---|---|\n"
        self.assertEqual(self.run_with("0", table).returncode, 1)

    def test_an_unset_count_fails(self):
        # The collect step skipped or never wrote its output: still nothing read.
        self.assertEqual(self.run_with("").returncode, 1)

    def test_a_missing_jobs_md_still_fails_with_the_error(self):
        r = Runner(self)
        p = r.run(WORKFLOW, self.STEP, GITHUB_WORKSPACE=str(r.tmp), GITHUB_RUN_ID="1", COLLECTED="0")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=No CI results::", p.stdout)

    def test_collected_gate_results_pass(self):
        p = self.run_with("3")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, "")


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
        self.assertEqual([f.name for f in (repo / "ci-results").iterdir()], ["harness.md"])

    def test_the_harness_names_its_commit_and_the_callers_inputs(self):
        # Without it the reviewer of a PR that moves the airia-ci pin cannot
        # tell which harness ran or what the call passed (DevTools PR #20).
        r = Runner(self)
        repo = self.checkout(r)
        p = r.run(WORKFLOW, "Stage the toolchain status for Claude", cwd=repo, GITHUB_WORKSPACE=str(repo),
                  HARNESS_REF="o/airia-ci/.github/workflows/reusable-claude-review.yml@refs/tags/ci-v3",
                  HARNESS_SHA="ab7b0e6", CALLER_INPUTS='{"pr-number": 20}')
        self.assertEqual(p.returncode, 0, p.stderr)
        harness = (repo / "ci-results/harness.md").read_text()
        self.assertIn("reusable-claude-review.yml@refs/tags/ci-v3", harness)
        self.assertIn("- Commit: ab7b0e6", harness)
        self.assertIn('{"pr-number": 20}', harness)

    def test_a_re_review_gets_the_earlier_review_as_its_memory(self):
        # A stateless re-roll on every push is the ping-pong of AIRIA-framework #405; with the
        # earlier review in hand, the next one reports only what is new or still open.
        r = Runner(self, [[r"issues/comments/7( |$)", {"json": review(41)}]])
        repo = self.checkout(r)
        p = r.run(WORKFLOW, "Stage the toolchain status for Claude", cwd=repo, GITHUB_WORKSPACE=str(repo),
                  PRIOR_COMMENT_ID="7", PRIOR_HEAD="a" * 40)
        self.assertEqual(p.returncode, 0, p.stderr)
        prior = (repo / "ci-results/previous-review.md").read_text()
        self.assertTrue(prior.startswith(f"# The earlier automatic review, of head {'a' * 40}\n"), prior)
        self.assertIn("Nothing blocking.", prior)
        self.assertNotIn(FOOTER, prior, "a reviewer shown the footer copies it into its own body")
        self.assertIn("ci-results/previous-review.md", WORKFLOW.read_text(), "the prompt points the reviewer at it")

    def test_an_unreadable_earlier_review_still_lets_the_review_run(self):
        r = Runner(self, [[r"issues/comments/7( |$)", {"exit": 1, "stderr": "HTTP 404"}]])
        repo = self.checkout(r)
        p = r.run(WORKFLOW, "Stage the toolchain status for Claude", cwd=repo, GITHUB_WORKSPACE=str(repo),
                  PRIOR_COMMENT_ID="7", PRIOR_HEAD="unknown")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse((repo / "ci-results/previous-review.md").exists())

    def test_a_runner_without_the_job_workflow_context_says_unknown(self):
        r = Runner(self)
        repo = self.checkout(r)
        p = r.run(WORKFLOW, "Stage the toolchain status for Claude", cwd=repo, GITHUB_WORKSPACE=str(repo),
                  HARNESS_REF="", HARNESS_SHA="", CALLER_INPUTS="{}")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("- Commit: unknown", (repo / "ci-results/harness.md").read_text())


class InternalPins(unittest.TestCase):
    """Consumers call @main, so airia-ci calls itself @main: one @ci-vN ref and an @main caller runs a mixed gate."""

    def test_every_ref_to_airia_ci_under_github_is_main(self):
        refs = {f"{f.relative_to(ROOT)}: {m.group(0)}"
                for f in sorted((ROOT / ".github").rglob("*.yml"))
                for m in re.finditer(r"AIRIA-Systems-GmbH/airia-ci/\S+@\S+", f.read_text())}
        self.assertTrue(refs, "the reusable workflows call their actions by full path")
        self.assertEqual({r for r in refs if not r.endswith("@main")}, set())

    def test_init_wires_a_new_consumer_to_main(self):
        # A consumer wired to a frozen tag would never receive a fix.
        (tag,) = re.findall(r'^TAG = "(.+)"$', (ROOT / "scripts/init.py").read_text(), re.MULTILINE)
        self.assertEqual(tag, "main")


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

    def parse(self, msgs, version="", **env):
        with tempfile.TemporaryDirectory() as t:
            exe, body = Path(t) / "execution.json", Path(t) / "body.md"
            exe.write_text(json.dumps(msgs))
            env = bare(PATH="/usr/bin:/bin", RUN_URL="https://run", CLAUDE_VERSION=version, **env)
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
