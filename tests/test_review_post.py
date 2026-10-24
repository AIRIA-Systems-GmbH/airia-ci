# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The review-post action: the review is one summary comment plus one comment per finding.

- the final message ends with an `airia-findings` block; a missing or malformed
  one fails the job, an empty one posts the summary only;
- a finding is an inline comment, a file-level comment when its line is not in
  the diff, and a line of the summary when its file is not in the diff either;
- only the summary carries the footer the once-per-head guard counts;
- a finding already on the PR is linked, not posted again.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path

from steps import ROOT, Runner

ACTION = ROOT / ".github/actions/review-post/action.yml"
SCRIPT = ROOT / ".github/actions/review-post/post_review.py"
spec = importlib.util.spec_from_file_location("post_review", SCRIPT)
pr_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr_mod)

FOOTER = "_Automatic review by `claude-code-review.yml`"
HEAD = "c" * 40


def finding(fid="F1", file="src/a.py", line=12, severity="major", text="`x` is unchecked.", carried=False):
    f = {"id": fid, "file": file, "severity": severity, "text": text, "carried": carried}
    if line is not None:
        f["line"] = line
    return f


def message(findings, summary="## Review\n\nOne thing to fix."):
    return summary + "\n\n```airia-findings\n" + json.dumps({"findings": findings}, indent=1) + "\n```"


def execution(result: str, **extra) -> list:
    return [{"type": "system"}, {"type": "result", "subtype": "success", "num_turns": 3, "is_error": False,
                                 "permission_denials": [], "result": result, **extra}]


INLINE = r"^api --method POST repos/o/r/pulls/9/comments .* -F line="
FILE_LEVEL = r"^api --method POST repos/o/r/pulls/9/comments .* subject_type=file"
LISTING = r"^api --paginate repos/o/r/pulls/9/comments"
SUMMARY = r"^pr comment 9 "
UNPROCESSABLE = {"exit": 1, "stderr": "gh: Validation Failed (HTTP 422)\npull_request_review_thread.line could not be resolved"}

# What `gh api` really does on a 422: the one-line summary on stderr, the response body (with the errors[]
# detail) on stdout. PR #80's review job showed stderr as exactly `gh: Validation Failed (HTTP 422)`.
NOT_IN_DIFF_422 = {"exit": 1, "stderr": "gh: Validation Failed (HTTP 422)\n",
                   "text": json.dumps({"message": "Validation Failed", "status": "422", "errors": [
                       {"resource": "PullRequestReviewComment", "code": "custom",
                        "field": "pull_request_review_thread.line",
                        "message": "pull_request_review_thread.line could not be resolved"}]})}
OTHER_422 = {"exit": 1, "stderr": "gh: Validation Failed (HTTP 422)\n",
             "text": json.dumps({"message": "Validation Failed", "status": "422", "errors": [
                 {"resource": "PullRequestReviewComment", "code": "custom", "field": "commit_id",
                  "message": "commit_id is not part of the pull request"}]})}


class TheStep(unittest.TestCase):
    """The action's shell and Python together, against a fake `gh`, as the review job runs them."""

    def post(self, msgs, rules=(), version="2.1.300", head=HEAD, run_id="42"):
        rules = [*rules,
                 [LISTING, {"json": []}],
                 [INLINE, {"json": {"html_url": "https://github.com/o/r/pull/9#discussion_r1"}}],
                 [FILE_LEVEL, {"json": {"html_url": "https://github.com/o/r/pull/9#discussion_r2"}}],
                 [r"^api repos/o/r/pulls/9 ", {"json": {"head": {"sha": HEAD}}}],
                 [SUMMARY, {"text": "https://github.com/o/r/pull/9#c1\n"}]]
        r = Runner(self, rules)
        exe = r.tmp / "execution.json"
        if msgs is not None:
            exe.write_text(json.dumps(msgs))
        p = r.run(ACTION, "Post the review", PR="9", RUN_ID=run_id, EXECUTION_FILE=str(exe), CLAUDE_VERSION=version,
                  HEAD_SHA=head, RUN_URL=f"https://github.com/o/r/actions/runs/{run_id}")
        calls = r.calls()
        summaries = [c["files"]["--body-file"] for c in calls if c["args"][:2] == ["pr", "comment"]]
        findings = [(c["args"], c["files"]) for c in calls if "--method" in c["args"]]
        return p, summaries, findings

    def test_each_finding_is_its_own_inline_comment_and_the_summary_links_them(self):
        both = [finding("F1", "src/a.py", 12), finding("F2", "src/b.py", 3, "minor", "Rename it.")]
        p, summaries, comments = self.post(execution(message(both)))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(comments), 2, "one comment per finding")
        (args1, files1), (args2, files2) = comments
        self.assertIn("path=src/a.py", args1)
        self.assertIn("line=12", args1)
        self.assertIn("side=RIGHT", args1)
        self.assertIn(f"commit_id={HEAD}", args1)
        self.assertIn("path=src/b.py", args2)
        (body1,) = files1.values()
        self.assertIn("**major** · F1", body1)
        self.assertIn("`x` is unchecked.", body1)
        (body2,) = files2.values()
        self.assertIn("**minor** · F2", body2)
        (summary,) = summaries
        self.assertIn("- **F1** (major) `src/a.py:12` — [comment](https://github.com/o/r/pull/9#discussion_r1)", summary)
        self.assertIn("- **F2** (minor) `src/b.py:3`", summary)
        self.assertNotIn("airia-findings", summary, "the machine-readable block is not part of the summary")

    def test_each_finding_comment_names_the_run_and_the_finding_in_a_hidden_marker(self):
        _, _, comments = self.post(execution(message([finding("F7")])), run_id="42")
        (_, files), = comments
        (body,) = files.values()
        self.assertIn("<!-- airia-finding run=42 id=F7 file=src/a.py -->", body)
        self.assertTrue(pr_mod.MARKER.search(body), "the marker is what a re-review and the unanswered-findings check read")

    def test_no_finding_comment_carries_the_review_footer(self):
        # The once-per-head guard counts the footer: a finding comment that had it would count as a review.
        p, summaries, comments = self.post(execution(message([finding("F1"), finding("F2", line=None)])))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(comments), 2)
        for _, files in comments:
            (body,) = files.values()
            self.assertNotIn(FOOTER, body)
        (summary,) = summaries
        self.assertEqual(summary.count(FOOTER), 1)
        self.assertIn(FOOTER + " — [run log](https://github.com/o/r/actions/runs/42), Claude Code 2.1.300._", summary)
        self.assertTrue(summary.rstrip().endswith("2.1.300._"), "the footer closes the summary")

    def test_a_finding_that_carries_the_footer_in_its_text_is_refused(self):
        p, summaries, comments = self.post(execution(message([finding(text=f"copied: {FOOTER}")])))
        self.assertEqual((p.returncode, summaries, comments), (1, [], []))
        self.assertIn("would count as a review", p.stdout)

    def test_a_line_outside_the_diff_falls_back_to_a_file_level_comment(self):
        p, summaries, comments = self.post(execution(message([finding()])), rules=[[INLINE, UNPROCESSABLE]])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(comments), 2, "the inline attempt, then the file-level one")
        self.assertIn("line=12", comments[0][0])
        self.assertIn("subject_type=file", comments[1][0])
        self.assertNotIn("line=12", comments[1][0])
        self.assertIn("[comment](https://github.com/o/r/pull/9#discussion_r2)", summaries[0])

    def test_a_not_in_the_diff_422_whose_detail_is_only_on_stdout_falls_back_to_a_file_level_comment(self):
        # Real gh keeps the errors[] detail off stderr; matching stderr alone failed every such finding (PR #80).
        p, summaries, comments = self.post(execution(message([finding()])), rules=[[INLINE, NOT_IN_DIFF_422]])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(comments), 2, "the inline attempt, then the file-level one")
        self.assertIn("subject_type=file", comments[1][0])
        self.assertIn("[comment](https://github.com/o/r/pull/9#discussion_r2)", summaries[0])

    def test_a_file_level_422_with_the_detail_on_stdout_falls_back_to_the_summary(self):
        p, summaries, comments = self.post(execution(message([finding()])),
                                           rules=[[INLINE, NOT_IN_DIFF_422], [FILE_LEVEL, NOT_IN_DIFF_422]])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(comments), 2)
        self.assertIn("(not in the diff: answer in a PR comment", summaries[0])

    def test_any_other_422_still_fails_even_with_a_body(self):
        p, summaries, comments = self.post(execution(message([finding()])), rules=[[INLINE, OTHER_422]])
        self.assertEqual((p.returncode, summaries, len(comments)), (1, [], 1))
        self.assertIn("::error title=No review posted::finding F1: gh api failed: gh: Validation Failed (HTTP 422)",
                      p.stdout)

    def test_a_not_in_the_diff_text_without_a_422_status_still_fails(self):
        p, summaries, _ = self.post(execution(message([finding()])), rules=[[INLINE, {
            "exit": 1, "stderr": "gh: Server Error (HTTP 500)\n", "text": NOT_IN_DIFF_422["text"]}]])
        self.assertEqual((p.returncode, summaries), (1, []))

    def test_a_finding_without_a_line_is_file_level_straight_away(self):
        p, _, comments = self.post(execution(message([finding(line=None)])))
        self.assertEqual(p.returncode, 0, p.stderr)
        (args, _), = comments
        self.assertIn("subject_type=file", args)

    def test_a_file_outside_the_diff_has_no_comment_and_stays_in_the_summary(self):
        # No comment can hold a threaded reply here, so the slice-2 check could never see it answered.
        p, summaries, comments = self.post(execution(message([finding(text="No test covers it.\nSecond line.")])),
                                           rules=[[INLINE, UNPROCESSABLE], [FILE_LEVEL, UNPROCESSABLE]])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(comments), 2, "both attempts failed, and no issue comment follows")
        (summary,) = summaries
        self.assertIn("- **F1** (major) `src/a.py:12` (not in the diff: answer in a PR comment that quotes its id)", summary)
        self.assertIn("  > No test covers it.\n  > Second line.", summary)
        self.assertNotIn("[comment](", summary)

    def test_an_error_other_than_422_fails_and_posts_no_summary(self):
        p, summaries, _ = self.post(execution(message([finding()])),
                                    rules=[[INLINE, {"exit": 1, "stderr": "HTTP 500: Server Error"}]])
        self.assertEqual((p.returncode, summaries), (1, []))
        self.assertIn("::error title=No review posted::finding F1: gh api failed: HTTP 500", p.stdout)

    def test_a_failing_summary_comment_fails_the_job(self):
        p, _, _ = self.post(execution(message([])), rules=[[SUMMARY, {"exit": 1, "stderr": "HTTP 403"}]])
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=No review posted::gh pr comment failed: HTTP 403", p.stdout)

    def test_an_empty_findings_list_posts_the_summary_only(self):
        p, summaries, comments = self.post(execution(message([], summary="Nothing blocking.")))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(comments, [])
        (summary,) = summaries
        self.assertTrue(summary.startswith("Nothing blocking.\n\n---\n" + FOOTER))
        self.assertNotIn("### Findings", summary)

    def test_a_missing_findings_block_fails_and_posts_nothing(self):
        p, summaries, comments = self.post(execution("## Review\n\nLooks right."))
        self.assertEqual((p.returncode, summaries, comments), (1, [], []))
        self.assertIn("::error title=No review posted::the final message does not end with an `airia-findings` block", p.stdout)

    def test_a_malformed_findings_block_fails_and_posts_nothing(self):
        bad = "Review.\n\n```airia-findings\n{not json\n```"
        p, summaries, comments = self.post(execution(bad))
        self.assertEqual((p.returncode, summaries, comments), (1, [], []))
        self.assertIn("is not JSON of the form", p.stdout)

    def test_a_finding_posted_by_an_earlier_review_is_linked_not_posted_again(self):
        # A re-review keeps an open finding's id; its comment is already on the PR.
        p, summaries, comments = self.post(execution(message([finding("F1", carried=True), finding("F2", "src/c.py", 5)])),
                                           rules=[[LISTING, {"json": [self.earlier()]}]])
        self.assertEqual(p.returncode, 0, p.stderr)
        (args, _), = comments
        self.assertIn("path=src/c.py", args, "only the new finding F2 is posted")
        (summary,) = summaries
        self.assertIn("- **F1** (major) `src/a.py:12` (still open from an earlier review) — "
                      "[comment](https://github.com/o/r/pull/9#discussion_r9)", summary)
        self.assertIn("- **F2** (major) `src/c.py:5` — [comment]", summary)

    @staticmethod
    def earlier(fid="F1", file="src/a.py", login="github-actions[bot]", kind="Bot"):
        marker = pr_mod.comment_body(finding(fid, file), "41").splitlines()[-1]
        return {"html_url": "https://github.com/o/r/pull/9#discussion_r9", "user": {"login": login, "type": kind},
                "body": f"**major** · {fid}\n\nOld text.\n\n{marker}\n"}

    def test_a_carried_finding_on_another_file_than_its_marker_is_posted_as_new_and_noted(self):
        # The old F1's comment is about other code: linking it would hide the new problem.
        p, summaries, comments = self.post(execution(message([finding("F1", "src/other.py", 4, carried=True)])),
                                           rules=[[LISTING, {"json": [self.earlier("F1", "src/a.py")]}]])
        self.assertEqual(p.returncode, 0, p.stderr)
        (args, _), = comments
        self.assertIn("path=src/other.py", args)
        self.assertNotIn("still open", summaries[0])
        self.assertNotIn("discussion_r9", summaries[0])
        self.assertIn("marked as carried, but no earlier comment has this id and file: posted as new", summaries[0])

    def test_a_finding_that_is_not_carried_is_posted_as_new_even_on_the_same_id_and_file(self):
        # A fresh review numbers from F1 again, and the model may reuse a file: only `carried` links.
        p, summaries, comments = self.post(execution(message([finding("F1", "src/a.py", 12, carried=False)])),
                                           rules=[[LISTING, {"json": [self.earlier("F1", "src/a.py")]}]])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(comments), 1)
        self.assertNotIn("discussion_r9", summaries[0])
        self.assertNotIn("still open", summaries[0])
        self.assertNotIn("marked as carried", summaries[0])

    def test_a_carried_finding_with_no_marker_at_all_is_posted_as_new_and_noted(self):
        p, summaries, comments = self.post(execution(message([finding("F9", carried=True)])))
        self.assertEqual((p.returncode, len(comments)), (0, 1))
        self.assertIn("marked as carried", summaries[0])

    def test_the_file_in_the_marker_survives_spaces(self):
        name = "docs/my notes.md"
        self.assertIn("file=docs/my%20notes.md -->", pr_mod.comment_body(finding("F1", name), "42"))
        p, summaries, comments = self.post(execution(message([finding("F1", name, carried=True)])),
                                           rules=[[LISTING, {"json": [self.earlier("F1", name)]}]])
        self.assertEqual((p.returncode, comments), (0, []))
        self.assertIn("still open from an earlier review", summaries[0])

    def test_a_marker_from_another_bot_is_recognised(self):
        app = self.earlier(login="airia-reviewer[bot]")
        p, summaries, comments = self.post(execution(message([finding("F1", carried=True)])),
                                           rules=[[LISTING, {"json": [app]}]])
        self.assertEqual((p.returncode, comments), (0, []))
        self.assertIn("discussion_r9", summaries[0])

    def test_a_marker_quoted_by_a_user_does_not_suppress_a_finding(self):
        spoof = self.earlier(login="a-human", kind="User")
        p, _, comments = self.post(execution(message([finding("F1", carried=True)])),
                                   rules=[[LISTING, {"json": [spoof]}]])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(comments), 1)

    def test_a_422_about_something_else_fails_the_job(self):
        # A push landed mid-review: every comment would 422 on its commit_id, and none may be filed as "not in the diff".
        stale = {"exit": 1, "stderr": "gh: Validation Failed (HTTP 422)\ncommit_id is not part of the pull request"}
        p, summaries, comments = self.post(execution(message([finding()])), rules=[[INLINE, stale]])
        self.assertEqual((p.returncode, summaries, len(comments)), (1, [], 1))
        self.assertIn("::error title=No review posted::finding F1: gh api failed", p.stdout)
        self.assertIn("commit_id is not part of the pull request", p.stdout)

    def test_either_wording_of_the_diff_422_falls_back_case_insensitively(self):
        for wording in ("pull_request_review_thread.line could not be resolved",
                        "Pull request review thread line must be part of the diff"):
            with self.subTest(wording=wording):
                err = {"exit": 1, "stderr": f"gh: Validation Failed (HTTP 422)\n{wording.upper()}"}
                p, _, comments = self.post(execution(message([finding()])), rules=[[INLINE, err]])
                self.assertEqual((p.returncode, len(comments)), (0, 2))

    def test_without_the_event_head_the_prs_head_is_read(self):
        _, _, comments = self.post(execution(message([finding()])), head="")
        self.assertIn(f"commit_id={HEAD}", comments[0][0])

    def test_an_unreadable_head_fails_the_job(self):
        p, summaries, _ = self.post(execution(message([finding()])), head="",
                                    rules=[[r"^api repos/o/r/pulls/9 ", {"exit": 1, "stderr": "HTTP 404"}]])
        self.assertEqual((p.returncode, summaries), (1, []))

    def test_no_version_says_the_action_installed_its_own(self):
        _, summaries, _ = self.post(execution(message([])), version="")
        self.assertIn("Claude Code installed by the action._", summaries[0])

    def test_no_execution_file_fails_and_posts_nothing(self):
        p, summaries, _ = self.post(None)
        self.assertEqual((p.returncode, summaries), (1, []))
        self.assertIn("Claude did not run to completion", p.stdout)
        self.assertIn("::error title=No review posted::", p.stdout)

    def test_an_empty_or_errored_final_message_fails_and_posts_nothing(self):
        for result in (execution("   "), execution("API Error", is_error=True), execution("x")[:1]):
            with self.subTest(result=result[-1]):
                p, summaries, _ = self.post(result)
                self.assertEqual((p.returncode, summaries), (1, []))
                self.assertIn("No review text in the final message.", p.stdout)

    def test_denials_are_listed_first_thing_to_read_when_a_review_is_thin(self):
        denials = [{"tool_name": "Bash", "tool_input": {"command": f"curl {i}"}} for i in range(25)]
        p, _, _ = self.post(execution(message([]), permission_denials=denials))
        self.assertIn("permission denials: 25", p.stdout)
        self.assertIn('- Bash: {"command": "curl 0"}', p.stdout)
        self.assertEqual(p.stdout.count("- Bash:"), 20, "at most twenty are listed")

    def test_the_last_result_message_wins(self):
        msgs = execution("first") + [{"type": "result", "result": message([], summary="second"), "is_error": False}]
        _, summaries, _ = self.post(msgs)
        self.assertTrue(summaries[0].startswith("second\n"))

    def test_a_corrupt_execution_file_is_reported_not_raised(self):
        r = Runner(self)
        exe = r.tmp / "execution.json"
        exe.write_text("{not json")
        p = r.run(ACTION, "Post the review", PR="9", RUN_ID="1", EXECUTION_FILE=str(exe), RUN_URL="u")
        self.assertEqual(p.returncode, 1)
        self.assertIn("No execution file", p.stdout)


class ParseFindings(unittest.TestCase):
    def bad(self, findings, expect):
        with self.assertRaises(pr_mod.ReviewError) as cm:
            pr_mod.parse_findings(message(findings))
        self.assertIn(expect, str(cm.exception))

    def test_a_valid_block_is_split_from_the_summary(self):
        summary, found = pr_mod.parse_findings(message([finding()], summary="Text."))
        self.assertEqual((summary, found), ("Text.", [finding()]))

    def test_the_last_block_wins_when_the_summary_quotes_one(self):
        quoted = "Format:\n```airia-findings\n{\"findings\": []}\n```\n"
        summary, found = pr_mod.parse_findings(message([finding()], summary=quoted))
        self.assertEqual(len(found), 1)
        self.assertIn("Format:", summary)

    def test_text_after_the_block_is_refused(self):
        with self.assertRaises(pr_mod.ReviewError):
            pr_mod.parse_findings(message([]) + "\n\nAnd one more thing.")

    def test_findings_that_are_not_a_list_or_not_objects_are_refused(self):
        with self.assertRaises(pr_mod.ReviewError):
            pr_mod.parse_findings("x\n```airia-findings\n{\"findings\": {}}\n```")
        with self.assertRaises(pr_mod.ReviewError):
            pr_mod.parse_findings("x\n```airia-findings\n[]\n```")
        self.bad(["F1"], "finding 1 is not an object")

    def test_every_field_is_checked(self):
        self.bad([finding(fid="has space")], "no usable id")
        self.bad([finding("F1"), finding("F1")], "the id is used twice")
        self.bad([finding(file=" ")], "has no file")
        self.bad([finding(line=0)], "line must be a positive integer")
        self.bad([finding(line=True)], "line must be a positive integer")
        self.bad([finding(severity="huge")], "severity must be one of")
        self.bad([finding(text="  ")], "has no text")

    def test_carried_must_be_present_and_a_bool(self):
        missing = {k: v for k, v in finding().items() if k != "carried"}
        self.bad([missing], "carried must be true or false")
        for value in ("true", 1, None):
            self.bad([{**finding(), "carried": value}], "carried must be true or false")


class TheShippedFiles(unittest.TestCase):
    def test_the_action_calls_the_script_beside_it(self):
        self.assertRegex(ACTION.read_text(), re.escape('python3 "$GITHUB_ACTION_PATH/post_review.py"'))
        self.assertTrue(Path(SCRIPT).is_file())


if __name__ == "__main__":
    unittest.main()
