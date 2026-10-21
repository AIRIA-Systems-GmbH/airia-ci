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


def job(id_, name, conclusion, steps, run_attempt=1):
    return {"id": id_, "name": name, "status": "completed", "conclusion": conclusion, "steps": steps,
            "run_attempt": run_attempt,
            "check_run_url": f"https://api.github.com/repos/o/r/check-runs/{id_}",
            "started_at": "2026-10-13T00:40:51Z", "completed_at": "2026-10-13T01:11:17Z"}


# A real time-capped job's annotations (AIRIA-DevTools run 38328432427, Pytest):
# the cap is one of three, and its job log says only "The operation was canceled."
TIME_CAP = [
    {"annotation_level": "failure", "message": "The job has exceeded the maximum execution time of 30m0s"},
    {"annotation_level": "warning", "message": "Node.js 20 is deprecated. The following actions target Node.js 20 ..."},
    {"annotation_level": "failure", "message": "The operation was canceled."},
]
CANCELLED = [{"annotation_level": "failure", "message": "The operation was canceled."}]
BAD_GATEWAY = {"stderr": "gh: Server Error (HTTP 502)\n", "exit": 1}
FAKE_SLEEP = '#!/bin/sh\necho "$1" >> "$(dirname "$0")/../sleeps"\n'


def rules(jobs, logs, help_text="  --allow-escape-sequences\n", run=None, annotations=None, listings=None):
    out = [[r"^api --help", {"text": help_text}]]
    # Per-filter answers to the jobs listing, ahead of the catch-all `jobs` rule.
    for filter_, listed in (listings or {}).items():
        out.append([rf"actions/runs/42/jobs\?filter={filter_}( |$)", {"json": {"jobs": listed}}])
    for id_, reply in (annotations or {}).items():
        out.append([rf"check-runs/{id_}/annotations ", reply])
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
    def collect(self, jobs, logs, env=None, **kw):
        r = Runner(self, rules({"json": {"jobs": jobs}} if isinstance(jobs, list) else jobs, logs, **kw))
        # No real backoff between retries of a failed gh call: a test that answers an error must stay fast.
        # The fake `sleep` records each pause instead, so the backoff itself is asserted.
        r.tool("sleep", FAKE_SLEEP)
        p = r.run(ACTION, STEP, RUN_ID="42", OUT=str(r.tmp / "ci"), **(env or {}))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.r = r
        return r.outputs(), (r.tmp / "ci/jobs.md").read_text()

    def log(self, id_):
        return (self.r.tmp / f"ci/logs/{id_}.log").read_text()

    def calls_to(self, pattern):
        return len([c for c in self.r.calls() if pattern in " ".join(c["args"])])

    def sleeps(self):
        f = self.r.tmp / "sleeps"
        return f.read_text().split() if f.exists() else []


KILLED = [{"name": "Set up job", "conclusion": "success"}, {"name": "Run tests", "conclusion": "cancelled"},
          {"name": "Coverage floor", "conclusion": "skipped"}]


class CollectLabelsJobsThatNeverRan(Collect):
    def setUp(self):
        jobs = [
            job(1, "Lint", "success", RAN),
            # GitHub's "not acquired by Runner" shape: cancelled, no step, no log.
            job(2, "SurrealDB", "cancelled", []),
            # Cancelled mid-run: it ran, so it has a log worth reading.
            job(3, "Coverage", "cancelled", KILLED),
        ]
        self.out, self.jobs_md = self.collect(jobs, {1: "lint ok\n", 3: "killed\n"},
                                              annotations={3: {"json": CANCELLED}})

    def test_a_job_with_no_step_is_labelled_never_ran(self):
        self.assertIn("| SurrealDB | cancelled (never ran) | - | logs/2.log |", self.jobs_md)
        self.assertIn("never ran", self.log(2))

    def test_a_job_cancelled_mid_run_is_still_plain_cancelled_with_its_log(self):
        # Its annotations were read and carry no time cap: a person or a newer run cancelled it.
        self.assertIn("| Coverage | cancelled | Run tests (cancelled) | logs/3.log |", self.jobs_md)
        self.assertEqual(self.log(3), "killed\n")
        self.assertNotIn("time cap", self.jobs_md)

    def test_only_a_job_that_started_has_its_annotations_read(self):
        reads = [c["args"] for c in self.r.calls() if "annotations" in " ".join(c["args"])]
        self.assertEqual([a[2] for a in reads], ["https://api.github.com/repos/o/r/check-runs/3/annotations"])

    def test_never_ran_is_not_an_unreadable_log(self):
        # Before: "1 of 3 job logs could not be fetched", which the reviewer
        # reported as a log it could not read.
        self.assertNotIn("could not be fetched", self.jobs_md)
        self.assertIn("**1 of 3 jobs never ran**", self.jobs_md)
        self.assertIn("gh run rerun 42 --failed", self.jobs_md)
        self.assertEqual(self.out["collected"], "3")


class CollectLabelsJobsThatHitTheirTimeCap(Collect):
    """ISS-66 in AIRIA-DevTools: a 30m Pytest gate was reported as infrastructure, re-run advised."""

    def test_a_job_whose_annotations_say_time_cap_is_labelled_so(self):
        out, md = self.collect([job(1, "Lint", "success", RAN), job(2, "Pytest", "cancelled", KILLED)],
                               {1: "ok\n", 2: "The operation was canceled.\n"}, annotations={2: {"json": TIME_CAP}})
        self.assertIn("| Pytest | cancelled (time cap) | Run tests (cancelled) | logs/2.log |", md)
        self.assertIn("**1 of 2 jobs hit their `timeout-minutes` cap**", md)
        self.assertIn("not infrastructure", md)
        self.assertNotIn("gh run rerun", md, "a re-run is not the remedy for a gate over its budget")
        self.assertEqual(self.log(2), "The operation was canceled.\n", "its log is still read")

    def test_an_annotation_without_a_message_does_not_hide_the_cap(self):
        out, md = self.collect([job(2, "Pytest", "cancelled", KILLED)], {2: "killed\n"},
                               annotations={2: {"json": [{"annotation_level": "notice", "message": None}, *TIME_CAP]}})
        self.assertIn("| Pytest | cancelled (time cap) |", md)

    def test_unreadable_annotations_leave_the_time_cap_open_and_say_how_long_it_ran(self):
        # A token without `checks: read` (ci-results called outside the reusable
        # workflows) gets a 403 on a private repository. That must not read as "not a timeout".
        out, md = self.collect([job(2, "Pytest", "cancelled", KILLED)], {2: "killed\n"},
                               annotations={2: {"stderr": "HTTP 403: Resource not accessible by integration\n", "exit": 1}})
        self.assertEqual(out["collected"], "1")
        self.assertIn("| Pytest | cancelled (time cap unchecked, ran 30m26s) | Run tests (cancelled) |", md)
        self.assertIn("**1 of 1 jobs were cancelled after they started, and their annotations could not be read**", md)

    def test_a_job_without_timestamps_still_lists(self):
        # Every listed field must survive a job GitHub reports oddly, or the whole listing is lost.
        odd = {**job(2, "Pytest", "cancelled", KILLED), "started_at": None, "check_run_url": None}
        out, md = self.collect([odd], {2: "killed\n"})
        self.assertIn("| Pytest | cancelled (time cap unchecked, ran ?) | Run tests (cancelled) |", md)


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
        self.assertEqual(self.calls_to("actions/runs/42/jobs"), 1, "only a 5xx is worth asking again")

    def test_a_failed_head_sha_lookup_counts_as_a_failed_listing(self):
        out, md = self.collect([job(1, "Lint", "success", RAN)], {1: "ok\n"},
                               run={"stderr": "HTTP 502\n", "exit": 1})
        self.assertEqual(out, {"collected": "0"})
        self.assertIn("HTTP 502", md)
        self.assertEqual(self.calls_to("actions/runs/42/jobs"), 3, "a 5xx on the head_sha read retries the whole listing")

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


class CollectRetriesATransientServerError(Collect):
    """Issue #54: one `gh: Server Error (HTTP 502)` while listing the jobs lost the whole CI evidence."""

    def test_iss_54_a_502_on_the_first_job_listing_is_retried(self):
        listing = {"json": {"jobs": [job(1, "Lint", "success", RAN)]}, "first": 1, "before": BAD_GATEWAY}
        out, md = self.collect(listing, {1: "ok\n"})
        self.assertNotIn("could not be listed", md, "a transient 502 is not a run whose jobs cannot be listed")
        self.assertEqual(out["collected"], "1")
        self.assertIn("Commit: abc123.", md)
        listings = [c for c in self.r.calls() if "actions/runs/42/jobs" in " ".join(c["args"])]
        self.assertEqual(len(listings), 2, "the failed listing is asked once more, and only once")

    def test_the_third_attempt_is_still_made(self):
        listing = {"json": {"jobs": [job(1, "Lint", "success", RAN)]}, "first": 2, "before": BAD_GATEWAY}
        out, md = self.collect(listing, {1: "ok\n"})
        self.assertEqual(out["collected"], "1")
        self.assertEqual(self.calls_to("actions/runs/42/jobs"), 3)

    def test_a_502_that_persists_gives_up_after_three_attempts_and_says_why(self):
        out, md = self.collect(BAD_GATEWAY, {})
        self.assertEqual(out, {"collected": "0"})
        self.assertIn("**The run's jobs could not be listed**", md)
        self.assertIn("HTTP 502", md, "the reviewer must see the last attempt's error")
        self.assertEqual(self.calls_to("actions/runs/42/jobs"), 3, "bounded: a dead API is not asked forever")
        self.assertEqual(md.count("HTTP 502"), 1, "each attempt starts a fresh jobs.err")
        self.assertEqual(self.sleeps(), ["10", "20"], "the default pause, growing with each attempt")

    def test_a_knob_that_is_not_a_number_takes_its_default(self):
        # `three` must not retry forever, and `1s` must not end the step in an arithmetic error.
        out, md = self.collect(BAD_GATEWAY, {}, env={"CI_RESULTS_ATTEMPTS": "three", "CI_RESULTS_RETRY_SLEEP": "1s"})
        self.assertEqual(out, {"collected": "0"})
        self.assertEqual(self.calls_to("actions/runs/42/jobs"), 3)
        self.assertEqual(self.sleeps(), ["10", "20"])

    def test_a_transient_502_on_the_head_sha_read_is_retried(self):
        out, md = self.collect([job(1, "Lint", "success", RAN)], {1: "ok\n"},
                               run={"json": {"head_sha": "abc123"}, "first": 1, "before": BAD_GATEWAY})
        self.assertEqual(out["collected"], "1")
        self.assertIn("Commit: abc123.", md)
        self.assertEqual(self.calls_to("actions/runs/42 "), 2)

    def test_rows_a_failed_attempt_wrote_are_not_listed_twice(self):
        # --paginate can print the pages it got before a later page fails with a 5xx.
        jobs = {"jobs": [job(1, "Lint", "success", RAN), job(2, "Docs", "success", RAN)]}
        listing = {"json": jobs, "first": 1, "before": {"json": jobs, **BAD_GATEWAY}}
        out, md = self.collect(listing, {1: "ok\n", 2: "ok\n"})
        self.assertEqual(out["collected"], "2")
        self.assertEqual(md.count("| Lint |"), 1)
        self.assertEqual(md.count("| Docs |"), 1)

    def test_ci_results_attempts_bounds_the_attempts(self):
        listing = {"json": {"jobs": [job(1, "Lint", "success", RAN)]}, "first": 1, "before": BAD_GATEWAY}
        out, md = self.collect(listing, {1: "ok\n"}, env={"CI_RESULTS_ATTEMPTS": "1"})
        self.assertIn("could not be listed", md)
        self.assertEqual(self.calls_to("actions/runs/42/jobs"), 1)


class CollectAPartialReRunInFlight(Collect):
    """Issue #54, AIRIA-framework run 39277117770: `gh run rerun --failed` re-ran only the review.

    While attempt 2 ran, `filter=latest` reported no completed job: the carried-over
    gates get their attempt-2 copies only when the attempt ends. Their results are
    the attempt-1 executions, which `filter=all` lists.
    """

    REVIEW = "Claude Review / claude-review"

    def collect_rerun(self, all_jobs, latest_jobs, logs):
        # The fake tells the two listings apart, so the old `filter=latest` query finds nothing to read.
        return self.collect(all_jobs, logs, listings={"latest": latest_jobs, "all": all_jobs})

    def test_iss_54_carried_over_gates_are_read_from_their_last_execution(self):
        in_flight = {**job(9, self.REVIEW, None, [], run_attempt=2), "status": "in_progress"}
        out, md = self.collect_rerun(
            [job(1, "Lint", "success", RAN), job(2, "Unit Tests", "success", RAN),
             job(3, self.REVIEW, "failure", RAN), in_flight],
            [in_flight], {1: "lint ok\n", 2: "tests ok\n"})
        self.assertEqual(out["collected"], "2", "both gates of attempt 1 are results; the review is not")
        self.assertIn("| Lint | success | - | logs/1.log |", md)
        self.assertIn("| Unit Tests | success | - | logs/2.log |", md)
        self.assertNotIn(self.REVIEW, md, "the review's own failed attempt 1 is not a gate result")

    def test_a_gate_re_run_in_a_later_attempt_is_read_from_that_attempt(self):
        later = [job(1, "Lint", "success", RAN), job(2, "Unit Tests", "failure", RAN),
                 job(7, "Unit Tests", "success", RAN, run_attempt=2)]
        out, md = self.collect_rerun(later, [job(7, "Unit Tests", "success", RAN, run_attempt=2)],
                                     {1: "lint ok\n", 7: "tests ok\n"})
        self.assertEqual(out["collected"], "2")
        self.assertIn("| Unit Tests | success | - | logs/7.log |", md)
        self.assertNotIn("| Unit Tests | failure |", md, "attempt 1's failure was superseded")
        self.assertLess(md.index("| Lint |"), md.index("| Unit Tests |"), "rows keep GitHub's order")

    def test_a_finished_run_lists_each_job_once(self):
        # After the attempt ends, every carried-over gate has an attempt-2 copy: one row, not two.
        done = [job(1, "Lint", "success", RAN), job(5, "Lint", "success", RAN, run_attempt=2)]
        out, md = self.collect_rerun(done, done[1:], {5: "lint ok\n"})
        self.assertEqual(out["collected"], "1")
        self.assertIn("| Lint | success | - | logs/5.log |", md)


if __name__ == "__main__":
    unittest.main()
