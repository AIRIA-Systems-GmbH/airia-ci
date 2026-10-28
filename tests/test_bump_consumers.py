# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""reusable-bump-consumers.yml and scripts/bump_dry_run.py (#94, the dry-run slice).

What these tests hold: the workflow can write nothing (`contents: read`, no
App token, no write verb, no secret but the optional read-only one); its one
`run:` block reads each recipient through `gh`, plans against the index and
reports it; and a recipient that could not be read or planned fails the run
after every recipient was tried, so a partial plan never reads as "nothing to
bump".

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import yaml
from steps import ROOT, Runner

WORKFLOW = ROOT / ".github/workflows/reusable-bump-consumers.yml"
STEP = "Plan the bump of every recipient"
DOC = yaml.safe_load(WORKFLOW.read_text())
CALL = DOC[True]["workflow_call"]  # PyYAML reads the key `on` as True
JOB = DOC["jobs"]["plan"]

NOT_FOUND = {"stderr": "gh: Not Found (HTTP 404)\n", "exit": 1}
FORBIDDEN = {"stderr": "gh: Resource not accessible (HTTP 403)\n", "exit": 1}


def lock(**versions):
    return "".join(f'[[package]]\nname = "{n}"\nversion = "{v}"\n\n' for n, v in versions.items())


def pyproject(*deps):
    return "[project]\nname = 'x'\ndependencies = [" + ", ".join(repr(d) for d in deps) + "]\n"


def recipients(*repos):
    entries = "".join(f'[[recipient]]\nrepo = "{r}"\npackages = ["airia-auth"]\n' for r in repos)
    return entries + '[[excluded]]\nrepo = "o/framework"\nwhy = "publishes"\n'


def repo_rules(repo, pyproject_text, lock_text):
    contents = f"api repos/{repo}/contents"
    return [
        [f"api repos/{repo} --jq", {"text": "main\n"}],
        [f"{contents}/pyproject.toml", {"text": pyproject_text} if pyproject_text is not None else NOT_FOUND],
        [f"{contents}/uv.lock", {"text": lock_text} if lock_text is not None else NOT_FOUND],
    ]


class Shape(unittest.TestCase):
    def test_it_can_write_nothing(self):
        self.assertEqual(DOC["permissions"], {})
        self.assertEqual(JOB["permissions"], {"contents": "read"})
        text = WORKFLOW.read_text()
        for forbidden in (r": write\b", r"create-github-app-token", r"verified-commit", r"git push", r"gh pr", r"gh issue",
                          r"AIRIA_COMMIT_APP", r"\bcurl\b"):
            self.assertNotRegex(text, forbidden)

    def test_the_only_secret_is_the_optional_read_token_and_nothing_else_is_required(self):
        self.assertEqual(set(CALL["secrets"]), {"CONFORMANCE_TOKEN"})
        self.assertFalse(CALL["secrets"]["CONFORMANCE_TOKEN"]["required"])
        for spec in CALL["inputs"].values():
            self.assertFalse(spec["required"])

    def test_the_inputs_and_their_defaults(self):
        self.assertEqual(set(CALL["inputs"]), {"runner", "index-url", "recipients"})
        self.assertEqual(json.loads(CALL["inputs"]["runner"]["default"]), ["self-hosted", "linux", "x64", "thor"])
        self.assertEqual(CALL["inputs"]["recipients"]["default"], "bump-recipients.toml")
        self.assertTrue((ROOT / CALL["inputs"]["recipients"]["default"]).is_file())
        self.assertEqual(CALL["inputs"]["index-url"]["default"], "https://pypi.de.airia.systems/simple/")

    def test_it_checks_out_airia_ci_at_the_commit_it_was_called_at_without_credentials(self):
        checkout = JOB["steps"][0]
        self.assertRegex(checkout["uses"], r"^actions/checkout@[0-9a-f]{40}$")
        self.assertEqual(checkout["with"]["repository"], "AIRIA-Systems-GmbH/airia-ci")
        self.assertEqual(checkout["with"]["ref"], "${{ job.workflow_sha }}")
        self.assertIs(checkout["with"]["persist-credentials"], False)

    def test_the_reads_use_the_optional_token_and_fall_back_to_the_callers(self):
        self.assertEqual(JOB["steps"][1]["env"]["GH_TOKEN"], "${{ secrets.CONFORMANCE_TOKEN || github.token }}")

    def test_the_outputs_come_from_the_plan_step(self):
        for name in ("moves", "failed", "plan"):
            self.assertEqual(JOB["outputs"][name], f"${{{{ steps.plan.outputs.{name} }}}}")
            self.assertEqual(CALL["outputs"][name]["value"], f"${{{{ jobs.plan.outputs.{name} }}}}")


class Serving:
    """A fake simple index over HTTP: <root>/<package>/index.html."""

    def __init__(self, test, packages: dict[str, list[str]]):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        for package, versions in packages.items():
            (Path(self.tmp.name) / package).mkdir()
            links = "".join(f'<a href="/f/{f}">{f}</a><br/>' for f in
                            (f"{package.replace('-', '_')}-{v}-py3-none-any.whl" for v in versions))
            (Path(self.tmp.name) / package / "index.html").write_text(f"<html><body>{links}</body></html>")
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        handler = functools.partial(Quiet, directory=self.tmp.name)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/"


class Plan(unittest.TestCase):
    def setUp(self):
        self.run = Runner(self)
        self.index = Serving(self, {"airia-auth": ["0.8.0", "0.9.0", "0.9.1"]})
        # The checkout, as the workflow lays it out: airia-ci/ beside the job's working directory.
        (self.run.tmp / "airia-ci").mkdir()
        (self.run.tmp / "airia-ci/scripts").symlink_to(ROOT / "scripts")

    def go(self, listed, rules, index=None, **env):
        (self.run.tmp / "airia-ci/list.toml").write_text(listed)
        self.run.rules(rules)
        return self.run.run(WORKFLOW, STEP, RECIPIENTS="list.toml", INDEX_URL=index or self.index.url, **env)

    def test_it_plans_every_recipient_and_reports_to_the_summary_and_the_outputs(self):
        rules = repo_rules("o/a", pyproject("airia-auth==0.9.0"), lock(**{"airia-auth": "0.9.0"}))
        rules += repo_rules("o/b", pyproject("airia-auth==0.9.1"), lock(**{"airia-auth": "0.9.1"}))
        done = self.go(recipients("o/a", "o/b"), rules)
        self.assertEqual(done.returncode, 0, done.stderr)
        summary = self.run.summary.read_text()
        self.assertIn("dry run: nothing was written", summary)
        self.assertIn("airia-auth\t0.9.0\t0.9.1\tbump", summary)
        self.assertIn("airia-auth\t0.9.1\t0.9.1\tcurrent", summary)
        out = self.run.outputs()
        self.assertEqual((out["recipients"], out["moves"], out["failed"]), ("2", "1", "0"))
        plan = json.loads(out["plan"])
        self.assertEqual([p["repo"] for p in plan], ["o/a", "o/b"])
        self.assertEqual(plan[0]["rows"][0]["target"], "0.9.1")
        self.assertNotIn("text", plan[0])

    def test_it_only_reads_and_never_with_a_write_verb(self):
        rules = repo_rules("o/a", pyproject("airia-auth==0.9.0"), lock(**{"airia-auth": "0.9.0"}))
        self.go(recipients("o/a"), rules)
        calls = [c["args"] for c in self.run.calls()]
        self.assertEqual(len(calls), 3)
        for args in calls:
            self.assertEqual(args[0], "api")
            self.assertTrue(set(args) - {"api"} and not {"-X", "--method", "-f", "-F", "--input"} & set(args))

    def test_a_repository_the_token_cannot_see_fails_the_run_after_the_others_were_planned(self):
        rules = [["api repos/o/hidden --jq", NOT_FOUND]]
        rules += repo_rules("o/a", pyproject("airia-auth==0.9.0"), lock(**{"airia-auth": "0.9.0"}))
        done = self.go(recipients("o/hidden", "o/a"), rules)
        self.assertEqual(done.returncode, 1)
        self.assertIn("**o/hidden**: not-reachable", self.run.summary.read_text())
        self.assertIn("airia-auth\t0.9.0\t0.9.1\tbump", self.run.summary.read_text())
        self.assertIn("::error title=Bump plan::o/hidden is not-reachable", done.stderr)
        self.assertEqual(self.run.outputs()["failed"], "1")

    def test_a_forbidden_file_is_not_read_as_a_missing_one(self):
        rules = repo_rules("o/a", None, None)
        rules[1][1] = FORBIDDEN
        done = self.go(recipients("o/a"), rules)
        self.assertEqual(done.returncode, 1)
        self.assertIn("not-reachable", self.run.summary.read_text())

    def test_a_readable_repository_without_a_root_pyproject_has_nothing_to_bump(self):
        done = self.go(recipients("o/a"), repo_rules("o/a", None, None))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no root pyproject.toml: nothing to bump", self.run.summary.read_text())

    def test_an_unreadable_index_fails_the_run_instead_of_reading_as_nothing_to_bump(self):
        rules = repo_rules("o/a", pyproject("airia-auth==0.9.0"), lock(**{"airia-auth": "0.9.0"}))
        done = self.go(recipients("o/a"), rules, index=self.index.url + "nowhere/")
        self.assertEqual(done.returncode, 1)
        self.assertIn("**o/a**: failed: cannot read", self.run.summary.read_text())
        self.assertEqual(self.run.outputs()["moves"], "0")

    def test_a_lock_that_misses_a_declared_package_fails_that_recipient_only(self):
        rules = repo_rules("o/a", pyproject("airia-auth==0.9.0"), lock(other="1"))
        rules += repo_rules("o/b", pyproject("airia-auth==0.9.0"), lock(**{"airia-auth": "0.9.0"}))
        done = self.go(recipients("o/a", "o/b"), rules)
        self.assertEqual(done.returncode, 1)
        text = self.run.summary.read_text()
        self.assertIn("**o/a**: failed: uv.lock has no airia-auth", text)
        self.assertIn("airia-auth\t0.9.0\t0.9.1\tbump", text)

    def test_the_default_list_is_the_one_in_the_checkout(self):
        (self.run.tmp / "airia-ci/bump-recipients.toml").symlink_to(ROOT / "bump-recipients.toml")
        self.run.rules([[".", FORBIDDEN]])
        done = self.run.run(WORKFLOW, STEP, RECIPIENTS="bump-recipients.toml", INDEX_URL=self.index.url)
        self.assertEqual(done.returncode, 1)
        repos = re.findall(r"\*\*(\S+)\*\*: not-reachable", self.run.summary.read_text())
        self.assertIn("AIRIA-Systems-GmbH/AIRIA-process", repos)
        self.assertNotIn("AIRIA-Systems-GmbH/AIRIA-framework", repos)  # excluded, never read

    def test_without_the_environment_it_still_prints_the_plan(self):
        # No GITHUB_STEP_SUMMARY / GITHUB_OUTPUT (a run by hand): the report goes to stdout only.
        (self.run.tmp / "airia-ci/list.toml").write_text(recipients("o/a"))
        self.run.rules(repo_rules("o/a", pyproject("airia-auth==0.9.0"), lock(**{"airia-auth": "0.9.0"})))
        env = self.run.env()
        for name in ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"):
            del env[name]
        done = subprocess.run([sys.executable, str(ROOT / "scripts/bump_dry_run.py"), "--recipients",
                               str(self.run.tmp / "airia-ci/list.toml"), "--index", self.index.url],
                              env={**env, "PYTHONPATH": ""}, cwd=self.run.tmp, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("airia-auth\t0.9.0\t0.9.1\tbump", done.stdout)
        self.assertFalse(os.path.exists(self.run.summary))


if __name__ == "__main__":
    unittest.main()
