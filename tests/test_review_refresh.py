# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""review_refresh.py: a re-run's gate table lands in the existing review.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

from steps import ROOT, Runner

SCRIPT = Path(__file__).resolve().parents[1] / ".github/actions/review-refresh/review_refresh.py"
spec = importlib.util.spec_from_file_location("review_refresh", SCRIPT)
rr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rr)

FOOTER = (
    "\n\n---\n_Automatic review by `claude-code-review.yml` — "
    "[run log](https://github.com/o/r/actions/runs/42), Claude Code 2.1.288._\n"
)
REVIEW = "## Review\n\nNothing blocking.\n\n| Gate | Conclusion |\n|---|---|\n| SurrealDB | cancelled |" + FOOTER


def jobs_md(conclusion: str) -> str:
    return (
        "# CI results — run https://github.com/o/r/actions/runs/42\n\n"
        "Commit: abc. One log per job in logs/, named by job id.\n\n"
        "| Job | Conclusion | Failed steps | Log |\n"
        "|---|---|---|---|\n"
        "| Lint | success | - | logs/1.log |\n"
        f"| SurrealDB | {conclusion} | - | logs/2.log |\n"
    )


class Refresh(unittest.TestCase):
    def test_first_refresh_lands_before_the_footer(self):
        out = rr.refresh(REVIEW, jobs_md("success"), attempt=2)
        before, _footer = out.split("\n---\n_Automatic review", 1)
        # The re-run's result is in the comment, above the footer...
        self.assertIn("| SurrealDB | success | - |", before)
        self.assertIn("attempt 2", before)
        # ...and the review itself is untouched.
        self.assertTrue(out.startswith(REVIEW.split(FOOTER)[0]))

    def test_footer_survives_byte_for_byte(self):
        # The once-per-PR guard keys on the footer: losing it would make the
        # next push post a second automatic review.
        out = rr.refresh(REVIEW, jobs_md("success"), attempt=2)
        self.assertTrue(out.endswith(FOOTER))

    def test_a_later_attempt_replaces_the_earlier_section(self):
        second = rr.refresh(REVIEW, jobs_md("failure"), attempt=2)
        third = rr.refresh(second, jobs_md("success"), attempt=3)
        self.assertEqual(third.count(rr.START), 1)
        self.assertIn("attempt 3", third)
        self.assertNotIn("attempt 2", third)
        self.assertNotIn("| SurrealDB | failure |", third)
        self.assertEqual(third, rr.refresh(REVIEW, jobs_md("success"), attempt=3))

    def test_the_log_column_is_dropped(self):
        # logs/<id>.log is a path inside the review job's checkout; on the PR it
        # points nowhere.
        out = rr.refresh(REVIEW, jobs_md("success"), attempt=2)
        self.assertNotIn("logs/", out)
        self.assertIn("| Job | Conclusion | Failed steps |", out)

    def test_a_comment_without_the_footer_is_refused(self):
        with self.assertRaises(ValueError):
            rr.refresh("someone else's comment", jobs_md("success"), attempt=2)

    def test_a_jobs_md_without_a_table_is_refused(self):
        # A failed lookup writes prose, not a table; refreshing with it would
        # replace real results with nothing.
        with self.assertRaises(ValueError):
            rr.refresh(REVIEW, "# CI results\n\n**The run's jobs could not be listed**", attempt=2)



class TheStep(unittest.TestCase):
    """The action's shell: read the comment, rewrite it, PATCH it back; never wipe it."""

    ACTION = ROOT / ".github/actions/review-refresh/action.yml"

    def refresh(self, body, jobs):
        r = Runner(self, [
            [r"^api --method PATCH repos/o/r/issues/comments/7 ", {"json": {"html_url": "https://c/7"}}],
            [r"^api repos/o/r/issues/comments/7 --jq .body", {"json": {"body": body}}],
        ])
        (r.tmp / "jobs.md").write_text(jobs)
        p = r.run(self.ACTION, "Refresh the gate table", COMMENT_ID="7", JOBS_MD=str(r.tmp / "jobs.md"), ATTEMPT="2")
        patches = [c["files"] for c in r.calls() if "PATCH" in c["args"]]
        return p, patches

    def test_the_refreshed_body_is_patched_into_the_same_comment(self):
        p, patches = self.refresh(REVIEW.rstrip("\n"), jobs_md("success"))
        self.assertEqual(p.returncode, 0, p.stderr)
        (files,) = patches
        (body,) = files.values()
        self.assertIn("### Gate results after re-run (attempt 2)", body)
        self.assertIn("Refreshed the gate results of https://c/7", p.stdout)

    def test_a_refresh_that_cannot_be_built_leaves_the_comment_alone(self):
        p, patches = self.refresh("a human's comment", jobs_md("success"))
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(patches, [], "an empty body would wipe the review, footer included")

    def test_the_script_runs_as_a_cli(self):
        r = Runner(self)
        (r.tmp / "body.md").write_text(REVIEW)
        (r.tmp / "jobs.md").write_text(jobs_md("success"))

        p = subprocess.run([sys.executable, str(SCRIPT), str(r.tmp / "body.md"), str(r.tmp / "jobs.md"), "3"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, rr.refresh(REVIEW, jobs_md("success"), 3))


if __name__ == "__main__":
    unittest.main()
