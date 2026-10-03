# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The SHIPPED shell of two steps, run against a fake `gh`:

- ci-results' "Collect the run's job results": a job that started no step is
  labelled `cancelled (never ran)` and is not counted as an unreadable log;
- the review's "Skip if this PR already has its automatic review": only a
  re-run attempt of the run that wrote the review asks for a refresh.

Each `run:` block is read out of the YAML consumers execute, never copied.
Needs PyYAML and jq (both on GitHub's ubuntu runners).

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # Serves FAKE_GH_DIR/<name>.json for the endpoints these two steps call.
    import os, re, subprocess, sys
    d = os.environ["FAKE_GH_DIR"]
    args = sys.argv[1:]
    assert args[0] == "api", args
    if "--help" in args:
        print("  --allow-escape-sequences"); sys.exit(0)
    jq, endpoint, rest = ".", None, args[1:]
    while rest:
        a = rest.pop(0)
        if a == "--jq": jq = rest.pop(0)
        elif a.startswith("-"): pass
        else: endpoint = a
    m = re.search(r"/actions/jobs/(\\d+)/logs$", endpoint)
    if m:
        p = os.path.join(d, f"log-{m.group(1)}.txt")
        if not os.path.exists(p):
            print("HTTP 404: Not Found", file=sys.stderr); sys.exit(1)
        sys.stdout.write(open(p).read()); sys.exit(0)
    name = ("jobs" if "/jobs" in endpoint else "comments" if "/comments" in endpoint else "run")
    sys.exit(subprocess.run(["jq", "-r", jq, os.path.join(d, name + ".json")]).returncode)
    """
)


def step_run(path: Path, step_name: str) -> str:
    doc = yaml.safe_load(path.read_text())
    steps = doc["runs"]["steps"] if "runs" in doc else next(iter(doc["jobs"].values()))["steps"]
    (step,) = [s for s in steps if s.get("name") == step_name]
    return step["run"]


class FakeGitHub:
    def __init__(self, tmp: Path, **fixtures):
        self.dir = tmp / "gh"
        self.dir.mkdir()
        (tmp / "bin").mkdir()
        gh = tmp / "bin" / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.path = f"{tmp / 'bin'}:{os.environ['PATH']}"
        for name, value in fixtures.items():
            if name.startswith("log_"):
                (self.dir / f"log-{name[4:]}.txt").write_text(value)
            else:
                (self.dir / f"{name}.json").write_text(json.dumps(value))

    def run(self, script: str, tmp: Path, **env) -> dict[str, str]:
        out, summary = tmp / "out", tmp / "summary"
        full_env = {
            **os.environ,
            "PATH": self.path,
            "FAKE_GH_DIR": str(self.dir),
            "GH_TOKEN": "x",
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_OUTPUT": str(out),
            "GITHUB_STEP_SUMMARY": str(summary),
            **env,
        }
        subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=full_env, check=True, cwd=tmp)
        lines = out.read_text().splitlines() if out.exists() else []
        return dict(line.split("=", 1) for line in lines)


def job(id_, name, conclusion, steps):
    return {"id": id_, "name": name, "status": "completed", "conclusion": conclusion, "steps": steps}


RAN = [{"name": "Run tests", "conclusion": "success"}]


class CollectLabelsJobsThatNeverRan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        jobs = [
            job(1, "Lint", "success", RAN),
            # GitHub's "not acquired by Runner" shape: cancelled, no step, no log.
            job(2, "SurrealDB", "cancelled", []),
            # Cancelled mid-run: it ran, so it has a log worth reading.
            job(3, "Coverage", "cancelled", [{"name": "Run tests", "conclusion": "cancelled"}]),
        ]
        gh = FakeGitHub(
            self.tmp, jobs={"jobs": jobs}, run={"head_sha": "abc123"}, log_1="lint ok\n", log_3="killed\n"
        )
        script = step_run(ROOT / ".github/actions/ci-results/action.yml", "Collect the run's job results")
        self.out = gh.run(script, self.tmp, RUN_ID="42", OUT=str(self.tmp / "ci"))
        self.jobs_md = (self.tmp / "ci/jobs.md").read_text()

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_job_with_no_step_is_labelled_never_ran(self):
        self.assertIn("| SurrealDB | cancelled (never ran) | - | logs/2.log |", self.jobs_md)
        self.assertIn("never ran", (self.tmp / "ci/logs/2.log").read_text())

    def test_a_job_cancelled_mid_run_is_still_plain_cancelled_with_its_log(self):
        self.assertIn("| Coverage | cancelled | - | logs/3.log |", self.jobs_md)
        self.assertEqual((self.tmp / "ci/logs/3.log").read_text(), "killed\n")

    def test_never_ran_is_not_an_unreadable_log(self):
        # Before: "1 of 3 job logs could not be fetched", which the reviewer
        # reported as a log it could not read.
        self.assertNotIn("could not be fetched", self.jobs_md)
        self.assertIn("**1 of 3 jobs never ran**", self.jobs_md)
        self.assertIn("gh run rerun 42 --failed", self.jobs_md)
        self.assertEqual(self.out["collected"], "3")


def review(run_id: int, comment_id: int = 7) -> dict:
    body = (
        "Nothing blocking.\n\n---\n_Automatic review by `claude-code-review.yml` — "
        f"[run log](https://github.com/o/r/actions/runs/{run_id}), Claude Code 2.1.288._"
    )
    return {"id": comment_id, "html_url": f"https://github.com/o/r/pull/9#c{comment_id}",
            "user": {"login": "github-actions[bot]"}, "body": body}


class OnceAsksForARefreshOnlyOnARerunOfTheSameRun(unittest.TestCase):
    SCRIPT = step_run(ROOT / ".github/workflows/reusable-claude-review.yml",
                      "Skip if this PR already has its automatic review")

    def once(self, comments, run_id="42", attempt="2"):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            return FakeGitHub(tmp, comments=comments).run(self.SCRIPT, tmp, PR="9", RUN_ID=run_id, ATTEMPT=attempt)

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


if __name__ == "__main__":
    unittest.main()
