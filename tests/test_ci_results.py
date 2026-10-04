# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The ci-results action's shipped shell, against a fake `gh`.

Its one promise is that it never fails the caller's job: whatever GitHub
answers, jobs.md is written and says what the reviewer can and cannot trust.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from steps import ROOT, Runner

ACTION = ROOT / ".github/actions/ci-results/action.yml"
STEP = "Collect the run's job results"
RAN = [{"name": "Run tests", "conclusion": "success"}]


def job(id_, name, conclusion, steps):
    return {"id": id_, "name": name, "status": "completed", "conclusion": conclusion, "steps": steps}


def rules(jobs, logs, help_text="  --allow-escape-sequences\n", run=None):
    out = [[r"^api --help", {"text": help_text}]]
    for id_, text in logs.items():
        out.append([rf"actions/jobs/{id_}/logs$", {"text": text} if text is not None
                    else {"stderr": "HTTP 404: Not Found\n", "exit": 1}])
    out += [
        [r"actions/jobs/\d+/logs$", {"stderr": "HTTP 404: Not Found\n", "exit": 1}],
        [r"actions/runs/42/jobs", jobs],
        [r"actions/runs/42 ", run or {"json": {"head_sha": "abc123"}}],
    ]
    return out


class Collect(unittest.TestCase):
    def collect(self, jobs, logs, **kw):
        r = Runner(self, rules({"json": {"jobs": jobs}} if isinstance(jobs, list) else jobs, logs, **kw))
        p = r.run(ACTION, STEP, RUN_ID="42", OUT=str(r.tmp / "ci"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.r = r
        return r.outputs(), (r.tmp / "ci/jobs.md").read_text()

    def log(self, id_):
        return (self.r.tmp / f"ci/logs/{id_}.log").read_text()


class CollectLabelsJobsThatNeverRan(Collect):
    def setUp(self):
        jobs = [
            job(1, "Lint", "success", RAN),
            # GitHub's "not acquired by Runner" shape: cancelled, no step, no log.
            job(2, "SurrealDB", "cancelled", []),
            # Cancelled mid-run: it ran, so it has a log worth reading.
            job(3, "Coverage", "cancelled", [{"name": "Run tests", "conclusion": "cancelled"}]),
        ]
        self.out, self.jobs_md = self.collect(jobs, {1: "lint ok\n", 3: "killed\n"})

    def test_a_job_with_no_step_is_labelled_never_ran(self):
        self.assertIn("| SurrealDB | cancelled (never ran) | - | logs/2.log |", self.jobs_md)
        self.assertIn("never ran", self.log(2))

    def test_a_job_cancelled_mid_run_is_still_plain_cancelled_with_its_log(self):
        self.assertIn("| Coverage | cancelled | - | logs/3.log |", self.jobs_md)
        self.assertEqual(self.log(3), "killed\n")

    def test_never_ran_is_not_an_unreadable_log(self):
        # Before: "1 of 3 job logs could not be fetched", which the reviewer
        # reported as a log it could not read.
        self.assertNotIn("could not be fetched", self.jobs_md)
        self.assertIn("**1 of 3 jobs never ran**", self.jobs_md)
        self.assertIn("gh run rerun 42 --failed", self.jobs_md)
        self.assertEqual(self.out["collected"], "3")


class CollectNeverFailsTheJob(Collect):
    def test_no_run_says_so_and_collects_nothing(self):
        r = Runner(self)
        p = r.run(ACTION, STEP, RUN_ID="", OUT=str(r.tmp / "ci"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(r.outputs(), {"collected": "0"})
        self.assertIn("No CI run was found", (r.tmp / "ci/jobs.md").read_text())
        self.assertEqual(r.calls(), [], "nothing to ask GitHub about")

    def test_a_failed_job_listing_still_writes_jobs_md_with_the_error(self):
        out, md = self.collect({"stderr": "HTTP 403: Resource not accessible\n", "exit": 1}, {})
        self.assertEqual(out, {"collected": "0"})
        self.assertIn("**The run's jobs could not be listed**", md)
        self.assertIn("HTTP 403: Resource not accessible", md, "the reviewer must see why")
        self.assertIn("could not be listed", self.r.summary.read_text())

    def test_a_failed_head_sha_lookup_counts_as_a_failed_listing(self):
        out, md = self.collect([job(1, "Lint", "success", RAN)], {1: "ok\n"},
                               run={"stderr": "HTTP 502\n", "exit": 1})
        self.assertEqual(out, {"collected": "0"})
        self.assertIn("HTTP 502", md)

    def test_an_unreadable_or_empty_log_is_counted_and_says_why(self):
        jobs = [job(1, "Lint", "success", RAN), job(2, "Tests", "failure",
                [{"name": "pytest", "conclusion": "failure"}, {"name": "upload", "conclusion": "failure"}]),
                job(3, "Docs", "success", RAN)]
        out, md = self.collect(jobs, {1: "ok\n", 2: None, 3: ""})
        self.assertEqual(out["collected"], "3")
        self.assertIn("| Tests | failure | pytest; upload | logs/2.log |", md)
        self.assertIn("**WARNING: 2 of 3 job logs could not be fetched**", md)
        self.assertIn("HTTP 404", self.log(2))
        self.assertIn("log unavailable", self.log(3))
        self.assertIn("Commit: abc123.", md)

    def test_an_old_gh_is_not_passed_the_escape_flag(self):
        # gh 2.46 on the Thor pool rejects --allow-escape-sequences as unknown.
        self.collect([job(1, "Lint", "success", RAN)], {1: "ok\n"}, help_text="usage: gh api\n")
        (log_call,) = [c["args"] for c in self.r.calls() if c["args"][-1].endswith("/logs")]
        self.assertNotIn("--allow-escape-sequences", log_call)

    def test_a_recent_gh_is_passed_the_escape_flag(self):
        # Recent gh writes NOTHING for a log with terminal escapes without it.
        self.collect([job(1, "Lint", "success", RAN)], {1: "ok\n"})
        (log_call,) = [c["args"] for c in self.r.calls() if c["args"][-1].endswith("/logs")]
        self.assertIn("--allow-escape-sequences", log_call)

    def test_only_completed_jobs_are_collected(self):
        running = {**job(4, "Slow", None, []), "status": "in_progress"}
        out, md = self.collect([job(1, "Lint", "success", RAN), running], {1: "ok\n"})
        self.assertEqual(out["collected"], "1")
        self.assertNotIn("Slow", md)


if __name__ == "__main__":
    unittest.main()
