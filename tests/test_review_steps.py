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

import re
import subprocess
import unittest

from steps import ROOT, Runner, step

WORKFLOW = ROOT / ".github/workflows/reusable-claude-review.yml"
FOOTER = "_Automatic review by `claude-code-review.yml`"


def review(run_id: int, comment_id: int = 7) -> dict:
    body = f"Nothing blocking.\n\n---\n{FOOTER} — [run log](https://github.com/o/r/actions/runs/{run_id}), Claude Code 2.1.288._"
    return {"id": comment_id, "html_url": f"https://github.com/o/r/pull/9#c{comment_id}",
            "user": {"login": "github-actions[bot]"}, "body": body}


class OnceAsksForARefreshOnlyOnARerunOfTheSameRun(unittest.TestCase):
    """Once per head, not once per PR: the check is never green on a head no review covered (airia-ci#60)."""

    HEAD = "b" * 40
    STEP = "Skip if this PR already has its automatic review"
    CAP = step(WORKFLOW, STEP)["env"]["REVIEW_CAP"]

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
        p = r.run(WORKFLOW, self.STEP, PR="9", RUN_ID=run_id, ATTEMPT=attempt, HEAD_SHA=head, REVIEW_CAP=self.CAP)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.stdout = p.stdout + (r.summary.read_text() if r.summary.exists() else "")
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

    def three_reviews(self, last_head):
        old = "a" * 40
        return ([review(39, comment_id=3), review(40, comment_id=5), review(41, comment_id=7)],
                {39: old, 40: old, 41: last_head})

    def test_the_cap_is_three(self):
        # Andrea, 2026-10-21: the delegated-merge rule's three rounds, then a human.
        self.assertEqual(self.CAP, "3")

    def test_a_new_head_after_the_third_review_passes_capped_without_a_review(self):
        # airia-ci#62 capped the reviews at three; airia-ci#70: the cap is not a human gate, so
        # the check passes and hands the next step what its summary comment says. Until
        # 2026-10-22 it failed, and four consumer PRs waited on Andrea that day.
        comments, heads = self.three_reviews("c" * 40)
        self.assertEqual(self.once(comments, attempt="1", heads=heads),
                         {"review": "false", "capped": "true", "head": self.HEAD, "count": "3", "cap": "3",
                          "newest": "https://github.com/o/r/pull/9#c7"})
        self.assertIn("::notice title=Automatic review cap reached::PR #9 already has 3 automatic reviews", self.stdout)
        self.assertNotIn("::error", self.stdout)

    def test_a_capped_pr_without_the_event_head_reads_the_prs_head(self):
        comments, heads = self.three_reviews("c" * 40)
        self.assertEqual(self.once(comments, attempt="1", heads=heads, head="", pr_head=self.HEAD)["head"], self.HEAD)

    def test_the_third_reviews_own_head_still_skips_green(self):
        # ready_for_review on the head the third review covered: nothing new to review, nothing to escalate.
        comments, heads = self.three_reviews(self.HEAD)
        self.assertEqual(self.once(comments, attempt="1", heads=heads), {"review": "false"})

    def test_a_rerun_of_the_third_reviewing_run_still_refreshes(self):
        comments, heads = self.three_reviews(self.HEAD)
        self.assertEqual(self.once(comments, run_id="41", attempt="2", heads=heads),
                         {"review": "false", "refresh": "true", "comment-id": "7"})

    def test_a_new_head_after_the_second_review_is_reviewed(self):
        old = "a" * 40
        comments = [review(40, comment_id=5), review(41, comment_id=7)]
        self.assertEqual(self.once(comments, attempt="1", heads={40: old, 41: old}),
                         {"review": "true", "prior-comment-id": "7", "prior-head": old})

    def test_someone_elses_comment_is_not_a_review(self):
        other = {**review(42), "user": {"login": "a-human"}}
        self.assertEqual(self.once([other]), {"review": "true"})


class NoteTheReviewCap(unittest.TestCase):
    """At the cap the PR gets one summary comment per head, never a fourth review (airia-ci#70)."""

    STEP = "Note the review cap"
    HEAD = "c" * 40
    MARKER = f"<!-- airia-ci review-cap {HEAD} -->"

    def note(self, comments, head=HEAD):
        r = Runner(self, [[r"issues/9/comments", {"json": comments}],
                          [r"^pr comment 9 ", {"text": "https://github.com/o/r/pull/9#c9\n"}]])
        p = r.run(WORKFLOW, self.STEP, PR="9", HEAD=head, COUNT="3", CAP="3", NEWEST="https://github.com/o/r/pull/9#c7")
        self.assertEqual(p.returncode, 0, p.stderr)
        posts = [c for c in r.calls() if c["args"][:2] == ["pr", "comment"]]
        return posts, p.stdout + (r.summary.read_text() if r.summary.exists() else "")

    def test_the_first_capped_run_on_a_head_posts_the_summary(self):
        posts, _ = self.note([review(41)])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["args"][:5], ["pr", "comment", "9", "--repo", "o/r"])
        body = posts[0]["files"]["--body-file"]
        self.assertIn("Review cap reached; no new automatic review.", body)
        self.assertIn("3 automatic reviews (the cap is 3)", body)
        self.assertIn("https://github.com/o/r/pull/9#c7", body)
        self.assertIn(f"`{self.HEAD}`", body)
        self.assertIn("every finding in the reviews has a reply", body)
        self.assertIn(self.MARKER, body)
        # The once step counts any bot comment with the footer as a review: this one must never be review #4.
        self.assertNotIn(FOOTER, body)

    def test_a_head_already_noted_is_not_noted_again(self):
        # A re-run, ready_for_review or a re-opened PR on the same capped head: one comment, not one per run.
        noted = {**review(41, comment_id=9), "body": "Review cap reached.\n" + self.MARKER}
        posts, out = self.note([review(41), noted])
        self.assertEqual(posts, [])
        self.assertIn("already says so", out)

    def test_someone_quoting_the_marker_does_not_suppress_it(self):
        quoted = {**review(41, comment_id=9), "user": {"login": "a-human"}, "body": self.MARKER}
        posts, _ = self.note([review(41), quoted])
        self.assertEqual(len(posts), 1)

    def test_a_note_for_another_head_does_not_suppress_this_one(self):
        other = {**review(41, comment_id=9), "body": "<!-- airia-ci review-cap " + "d" * 40 + " -->"}
        posts, _ = self.note([review(41), other])
        self.assertEqual(len(posts), 1)


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


class PostTheReview(unittest.TestCase):
    """The workflow posts, not the model: the review-post action does it (its cases are in test_review_post.py)."""

    def test_the_step_calls_the_action_at_main_and_passes_what_it_needs(self):
        s = step(WORKFLOW, "Post the review")
        self.assertEqual(s["uses"], "AIRIA-Systems-GmbH/airia-ci/.github/actions/review-post@main")
        self.assertNotIn("run", s, "the posting is the action's post_review.py, not an inline block")
        self.assertEqual(set(s["with"]), {"pr", "execution-file", "run-id", "run-url", "claude-version", "head-sha",
                                          "github-token"})
        self.assertIn("steps.claude-review.outputs.execution_file", s["with"]["execution-file"])

    def test_the_step_still_runs_when_claude_failed_but_not_when_it_was_skipped(self):
        # Green means posted: a failed Claude step still reaches the step that fails loud with no review.
        cond = step(WORKFLOW, "Post the review")["if"]
        self.assertIn("always()", cond)
        self.assertIn("steps.claude-review.outcome != 'skipped'", cond)

    def test_the_prompt_asks_for_the_findings_block_and_the_carried_flag(self):
        prompt = step(WORKFLOW, "Run Claude Code Review")["with"]["prompt"]
        for fragment in ("```airia-findings", '{"findings": []}', "blocker", '"carried": true', '"carried": false',
                         "ci-results/previous-review.md"):
            self.assertIn(fragment, prompt)


if __name__ == "__main__":
    unittest.main()
