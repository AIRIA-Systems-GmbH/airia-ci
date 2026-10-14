# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""scripts/init.py against a scratch checkout and a fake `gh`.

What these tests hold: an existing file is never overwritten; the review job
lands last in the one gate workflow and waits for every gate; the required
checks are the names GitHub reports for those gates; the token reaches `gh` on
stdin, never in an argument; a refused ruleset under a Free plan is explained,
not raised; nothing is guessed when the gate workflow is ambiguous.

Run: python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import json
import os
import unittest
from unittest import mock

import yaml
from steps import ROOT, Runner

SCRIPT = ROOT / "scripts/init.py"
spec = importlib.util.spec_from_file_location("init", SCRIPT)
init = importlib.util.module_from_spec(spec)
spec.loader.exec_module(init)

VIEW = ["repo view", {"json": {"nameWithOwner": "o/app", "defaultBranchRef": {"name": "main"}}}]
NO_SECRET = ["secret list", {"json": []}]
HAS_SECRET = ["secret list", {"json": [{"name": "CLAUDE_CODE_OAUTH_TOKEN"}]}]
SET = ["secret set", {}]
NO_RULESET = [r"rulesets --jq", {"json": []}]
HAS_RULESET = [r"rulesets --jq", {"json": [{"name": init.RULESET, "id": 7}]}]
CREATE = ["-X POST .*rulesets", {"text": "{}"}]
UPDATE = ["-X PUT .*rulesets/7", {"text": "{}"}]

STANDARD = """\
name: CI
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - run: true
  quality:
    name: Quality
    needs: [e2e]
    if: always()
    uses: AIRIA-Systems-GmbH/airia-ci/.github/workflows/reusable-python-gate.yml@ci-v5
    with:
      extra-jobs: ${{ toJSON(needs) }}
"""

CI = """\
name: CI
on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, ready_for_review]
jobs:
  lint:
    name: Code quality
    runs-on: ubuntu-latest
    steps:
      - name: not a job name
        run: true
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: true
"""


class Init(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        self.repo = self.r.tmp / "app"
        (self.repo / ".github/workflows").mkdir(parents=True)

    def run_main(self, rules, *args, answers=(), token="tok-123", pasted=""):
        self.r.rules(rules)
        self.r.tool("claude", "#!/bin/sh\necho \"$@\" > \"$(dirname \"$0\")/claude-args\"\n")
        out, err = io.StringIO(), io.StringIO()
        env = {**self.r.env(), "CLAUDE_CODE_OAUTH_TOKEN": token}
        with mock.patch.dict(os.environ, env), \
                mock.patch("builtins.input", side_effect=list(answers)), \
                mock.patch.object(init.getpass, "getpass", return_value=pasted), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = init.main(["--dir", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def file(self, rel):
        return (self.repo / rel).read_text()

    def call(self, pattern):
        return [c for c in self.r.calls() if pattern in " ".join(c["args"])]

    def test_the_callers_grant_every_permission_the_reusable_jobs_request(self):
        # A called job that requests more than its caller grants fails at
        # startup, before any step: ci-v4's `checks: read` is such a request.
        code, out, _ = self.run_main([VIEW, NO_SECRET, SET, NO_RULESET, CREATE], "--yes")
        self.assertEqual(code, 0, out)
        wf = ROOT / ".github/workflows"
        callers = {
            "reusable-claude-review.yml": [yaml.safe_load(self.file(".github/workflows/merge-gate.yml"))["jobs"]["claude-review"],
                                           yaml.safe_load((wf / "self-test.yml").read_text())["jobs"]["claude-review"]],
            "reusable-claude-respond.yml": [yaml.safe_load(self.file(".github/workflows/claude.yml"))["jobs"]["claude"],
                                            yaml.safe_load((wf / "claude.yml").read_text())["jobs"]["claude"]],
        }
        for called, jobs in callers.items():
            (requested,) = [j["permissions"] for j in yaml.safe_load((wf / called).read_text())["jobs"].values()]
            self.assertIn("checks", requested, "ci-results reads a cancelled job's annotations")
            for job in jobs:
                with self.subTest(called=called, caller=job["uses"]):
                    self.assertEqual({k: job["permissions"].get(k) for k in requested}, requested)

    def test_an_empty_repository_gets_a_failing_placeholder_gate_and_everything_wired(self):
        code, out, _ = self.run_main([VIEW, NO_SECRET, SET, NO_RULESET, CREATE], "--yes",
                                     "--runner", '["ubuntu-latest"]')
        self.assertEqual(code, 0, out)
        gate = self.file(".github/workflows/merge-gate.yml")
        self.assertIn("exit 1", gate, "a placeholder gate must never pass")
        self.assertIn("needs: [gates]", gate)
        self.assertIn(f"reusable-claude-review.yml@{init.TAG}", gate)
        self.assertIn("${{ github.event.pull_request.number }}", gate)
        respond = self.file(".github/workflows/claude.yml")
        self.assertIn("gate-workflow: merge-gate.yml", respond)
        self.assertIn("runner: '[\"ubuntu-latest\"]'", respond)
        self.assertIn("${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}", respond)
        self.assertIn("permissions: {}", respond)
        self.assertIn("# Review rules for app", self.file(".github/claude-review.md"))
        (secret,) = self.call("secret set")
        self.assertNotIn("tok-123", secret["args"], "the token travels on stdin only")
        self.assertEqual(secret["files"]["stdin"], "tok-123")
        (ruleset,) = self.call("-X POST")
        body = json.loads(ruleset["files"]["stdin"])
        self.assertEqual(body["conditions"]["ref_name"]["include"], ["~DEFAULT_BRANCH"])
        self.assertEqual(body["bypass_actors"], [])
        self.assertIn({"type": "required_signatures"}, body["rules"], "an unverified commit never reaches main")
        checks = [r for r in body["rules"] if r["type"] == "required_status_checks"][0]
        self.assertEqual(checks["parameters"]["required_status_checks"], [{"context": "gates"}])
        self.assertIn("put the real gates in the same pull request, or it cannot merge", out)

    def test_an_existing_gate_workflow_gets_the_review_last_and_its_check_names_required(self):
        (self.repo / ".github/workflows/ci.yml").write_text(CI)
        code, out, _ = self.run_main([VIEW, HAS_SECRET, NO_RULESET, CREATE], "--yes")
        self.assertEqual(code, 0, out)
        ci = self.file(".github/workflows/ci.yml")
        self.assertTrue(ci.startswith(CI), "the gates are left as they were")
        self.assertIn("needs: [lint, tests]", ci)
        self.assertIn(f"runner: '{init.THOR}'", ci)
        self.assertIn("gate-workflow: ci.yml", self.file(".github/workflows/claude.yml"))
        self.assertEqual(self.call("secret set"), [], "an existing secret is kept")
        body = json.loads(self.call("-X POST")[0]["files"]["stdin"])
        contexts = [c["context"] for r in body["rules"] if r["type"] == "required_status_checks"
                    for c in r["parameters"]["required_status_checks"]]
        self.assertEqual(contexts, ["Code quality", "tests"], "GitHub reports `name:` when a job has one")

    def test_a_flow_style_pull_request_trigger_is_the_gate_not_a_reason_for_a_new_one(self):
        (self.repo / ".github/workflows/ci.yml").write_text(CI.replace(
            "on:\n  pull_request:\n    branches: [main]\n    types: [opened, synchronize, reopened, ready_for_review]\n",
            "on: [push, pull_request]\n"))
        code, out, _ = self.run_main([VIEW, HAS_SECRET, HAS_RULESET], "--yes")
        self.assertEqual(code, 0)
        self.assertFalse((self.repo / ".github/workflows/merge-gate.yml").exists())
        self.assertIn("needs: [lint, tests]", self.file(".github/workflows/ci.yml"))

    def test_a_column_0_comment_between_jobs_hides_none_of_them(self):
        (self.repo / ".github/workflows/ci.yml").write_text(CI.replace("  tests:\n", "# ---- slow gates ----\n  tests:\n"))
        code, _, _ = self.run_main([VIEW, HAS_SECRET, NO_RULESET, CREATE], "--yes")
        self.assertEqual(code, 0)
        self.assertIn("needs: [lint, tests]", self.file(".github/workflows/ci.yml"))
        body = json.loads(self.call("-X POST")[0]["files"]["stdin"])
        self.assertIn({"context": "tests"}, [c for r in body["rules"] if r["type"] == "required_status_checks"
                                              for c in r["parameters"]["required_status_checks"]])

    def test_a_four_space_workflow_is_found_and_read(self):
        four = "\n".join(("  " + line if line.startswith(" ") else line) for line in CI.splitlines()) + "\n"
        (self.repo / ".github/workflows/ci.yml").write_text(four)
        code, _, _ = self.run_main([VIEW, HAS_SECRET, HAS_RULESET], "--yes")
        self.assertEqual(code, 0)
        self.assertFalse((self.repo / ".github/workflows/merge-gate.yml").exists())
        self.assertIn("needs: [lint, tests]", self.file(".github/workflows/ci.yml"))

    def test_a_matrix_job_is_waited_for_but_not_required(self):
        # Its checks are `tests (3.12)`, never `tests`: requiring `tests` would block every PR.
        (self.repo / ".github/workflows/ci.yml").write_text(CI.replace(
            "  tests:\n    runs-on: ubuntu-latest\n",
            "  tests:\n    strategy:\n      matrix:\n        py: ['3.12']\n    runs-on: ubuntu-latest\n"))
        code, out, _ = self.run_main([VIEW, HAS_SECRET, NO_RULESET, CREATE], "--yes")
        self.assertEqual(code, 0)
        self.assertIn("needs: [lint, tests]", self.file(".github/workflows/ci.yml"))
        self.assertIn("tests is a matrix job", out)
        body = json.loads(self.call("-X POST")[0]["files"]["stdin"])
        contexts = [c["context"] for r in body["rules"] if r["type"] == "required_status_checks"
                    for c in r["parameters"]["required_status_checks"]]
        self.assertEqual(contexts, ["Code quality"])

    def test_a_second_run_changes_nothing(self):
        (self.repo / ".github/workflows/ci.yml").write_text(CI)
        self.run_main([VIEW, HAS_SECRET, NO_RULESET, CREATE], "--yes")
        before = {p: p.read_text() for p in (self.repo / ".github").rglob("*") if p.is_file()}
        self.r.log.unlink()
        code, out, _ = self.run_main([VIEW, HAS_SECRET, HAS_RULESET])
        self.assertEqual(code, 0)
        self.assertEqual({p: p.read_text() for p in (self.repo / ".github").rglob("*") if p.is_file()}, before)
        self.assertIn("already calls the review", out)
        self.assertIn("ruleset (exists)", out)
        self.assertEqual(self.call("-X POST"), [])

    def test_several_pull_request_workflows_are_not_guessed_between(self):
        for name in ("a.yml", "b.yml"):
            (self.repo / ".github/workflows" / name).write_text(CI)
        code, out, _ = self.run_main([VIEW, HAS_SECRET, HAS_RULESET], "--yes")
        self.assertEqual(code, 0)
        self.assertIn("several pull-request workflows (a.yml, b.yml)", out)
        self.assertEqual(self.file(".github/workflows/a.yml"), CI)
        self.assertIn("gate-workflow: ''", self.file(".github/workflows/claude.yml"))

    def test_a_workflow_whose_jobs_are_not_last_is_printed_not_edited(self):
        text = CI.replace("    types: [opened, synchronize, reopened, ready_for_review]\n", "") + "concurrency: ci\n"
        (self.repo / ".github/workflows/ci.yml").write_text(text)
        code, out, _ = self.run_main([VIEW, HAS_SECRET, HAS_RULESET], "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.file(".github/workflows/ci.yml"), text)
        self.assertIn("`jobs:` is not its last top-level key", out)
        self.assertIn("needs: [lint, tests]", out)
        self.assertIn("does not run on ready_for_review", out)

    def test_declining_leaves_the_secret_and_main_alone_and_says_what_that_means(self):
        code, out, _ = self.run_main([VIEW, NO_SECRET, NO_RULESET], answers=["n", "no"], token="")
        self.assertEqual(code, 0)
        self.assertIn("the review and @claude fail until the secret is set", out)
        self.assertIn("can push to main directly", out)
        self.assertEqual(self.call("secret set") + self.call("-X POST"), [])

    def test_setup_token_runs_and_the_pasted_token_is_set(self):
        code, _, _ = self.run_main([VIEW, NO_SECRET, SET, HAS_RULESET], answers=[""], token="", pasted=" tok-9 \n")
        self.assertEqual(code, 0)
        self.assertEqual((self.r.bin / "claude-args").read_text().strip(), "setup-token")
        self.assertEqual(self.call("secret set")[0]["files"]["stdin"], "tok-9")

    def test_nothing_pasted_sets_nothing(self):
        code, out, _ = self.run_main([VIEW, NO_SECRET, HAS_RULESET], answers=["y"], token="", pasted="")
        self.assertEqual(code, 0)
        self.assertIn("skipped   the token: none given", out)

    def test_a_plan_that_refuses_rulesets_is_explained_not_raised(self):
        refused = ["-X POST .*rulesets", {"exit": 1, "text": '{"message":"Upgrade to GitHub Pro or make this '
                                                                'repository public to enable this feature."}'}]
        code, out, _ = self.run_main([VIEW, HAS_SECRET, NO_RULESET, refused], "--yes")
        self.assertEqual(code, 0)
        self.assertIn("needs GitHub Pro (personal) or Team (organization)", out)

    def test_other_api_failures_stop_with_the_reason(self):
        cases = [
            ([["repo view", {"exit": 1, "stderr": "not a git repository"}]], "not a GitHub repository checkout"),
            ([VIEW, NO_SECRET, ["secret set", {"exit": 1, "stderr": "HTTP 403"}]], "could not set the secret: HTTP 403"),
            ([VIEW, HAS_SECRET, NO_RULESET, ["-X POST", {"exit": 1, "stderr": "HTTP 422 bad"}]],
             "could not create the ruleset: HTTP 422 bad"),
        ]
        for rules, message in cases:
            with self.subTest(message):
                code, _, err = self.run_main(rules, "--yes")
                self.assertEqual(code, 2)
                self.assertIn(message, err)

    def ruleset_body(self, pattern: str) -> dict:
        (call,) = self.call(pattern)
        return json.loads(call["files"]["stdin"])

    @staticmethod
    def contexts(body: dict) -> list[str]:
        return [c["context"] for r in body["rules"] if r["type"] == "required_status_checks"
                for c in r["parameters"]["required_status_checks"]]

    def test_a_python_repository_gets_the_standard_gate_and_its_two_required_checks(self):
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'app'\n")
        code, out, _ = self.run_main([VIEW, HAS_SECRET, NO_RULESET, CREATE], "--yes")
        self.assertEqual(code, 0, out)
        ci = yaml.safe_load(self.file(".github/workflows/ci.yml"))
        self.assertEqual(ci["jobs"]["ci"]["uses"], f"{init.HARNESS}/reusable-python-gate.yml@{init.TAG}")
        self.assertFalse((self.repo / ".github/workflows/merge-gate.yml").exists())
        self.assertIn("gate-workflow: ci.yml", self.file(".github/workflows/claude.yml"))
        body = self.ruleset_body("-X POST")
        self.assertEqual(self.contexts(body), ["ci / gate", "ci / claude-review / claude-review"])
        self.assertEqual(body, init.ruleset(["ci / gate", "ci / claude-review / claude-review"]))

    def test_the_canonical_ruleset_is_the_standard(self):
        # No bypass, PR only, up to date, threads resolved, signed, no force-push or deletion.
        body = init.ruleset(["ci / gate"])
        self.assertEqual(body["name"], "Restrict Direct Merge on Main")
        self.assertEqual(body["bypass_actors"], [])
        self.assertEqual(body["enforcement"], "active")
        rules = {r["type"]: r.get("parameters") for r in body["rules"]}
        self.assertEqual(set(rules), {"deletion", "non_fast_forward", "required_signatures", "pull_request",
                                      "required_status_checks"})
        self.assertTrue(rules["pull_request"]["required_review_thread_resolution"])
        self.assertTrue(rules["required_status_checks"]["strict_required_status_checks_policy"])

    def test_the_standard_caller_grants_every_permission_the_gate_and_its_review_request(self):
        caller = yaml.safe_load(init.PYTHON_GATE.format(branch="main", harness=init.HARNESS, tag=init.TAG,
                                                        runner=init.THOR))["jobs"]["ci"]["permissions"]
        wf = ROOT / ".github/workflows"
        called = [*yaml.safe_load((wf / "reusable-python-gate.yml").read_text())["jobs"].values(),
                  *yaml.safe_load((wf / "reusable-claude-review.yml").read_text())["jobs"].values()]
        for job in called:
            for scope, level in (job.get("permissions") or {}).items():
                with self.subTest(scope=scope):
                    self.assertIn(caller.get(scope), (level, "write"))

    def test_protect_rewrites_an_existing_ruleset_into_the_canonical_form(self):
        (self.repo / ".github/workflows/ci.yml").write_text(STANDARD)
        code, out, _ = self.run_main([VIEW, HAS_RULESET, UPDATE], "protect", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.call("-X POST"), [])
        body = self.ruleset_body("-X PUT")
        # The caller job's `name:`, not the job before it that it waits for.
        self.assertEqual(self.contexts(body), ["Quality / gate", "Quality / claude-review / claude-review"])
        self.assertIn("rewritten 'Restrict Direct Merge on Main'", out)
        self.assertEqual(self.call("secret"), [], "protect touches only the ruleset")
        self.assertFalse((self.repo / ".github/workflows/claude.yml").exists())

    def test_protect_creates_the_ruleset_when_there_is_none(self):
        (self.repo / ".github/workflows/ci.yml").write_text(STANDARD)
        code, _, _ = self.run_main([VIEW, NO_RULESET, CREATE], "protect", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.contexts(self.ruleset_body("-X POST"))[0], "Quality / gate")

    def test_protect_without_the_standard_gate_is_refused(self):
        (self.repo / ".github/workflows/ci.yml").write_text(CI)
        code, _, err = self.run_main([VIEW], "protect", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("no pull-request workflow calls reusable-python-gate.yml", err)

    def test_protect_declined_changes_nothing(self):
        (self.repo / ".github/workflows/ci.yml").write_text(STANDARD)
        code, out, _ = self.run_main([VIEW, HAS_RULESET], "protect", answers=["n"])
        self.assertEqual(code, 0)
        self.assertIn("skipped   protection: the existing 'Restrict Direct Merge on Main' ruleset was left", out)
        self.assertEqual(self.call("-X PUT"), [])

    def test_protect_reports_a_failed_update(self):
        (self.repo / ".github/workflows/ci.yml").write_text(STANDARD)
        code, _, err = self.run_main([VIEW, HAS_RULESET, ["-X PUT", {"exit": 1, "stderr": "HTTP 403"}]],
                                     "protect", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("could not update the ruleset: HTTP 403", err)

    def test_wiring_a_repository_on_the_standard_keeps_its_workflow_and_rewrites_the_ruleset(self):
        (self.repo / ".github/workflows/ci.yml").write_text(STANDARD)
        code, out, _ = self.run_main([VIEW, HAS_SECRET, HAS_RULESET, UPDATE], "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.file(".github/workflows/ci.yml"), STANDARD)
        self.assertIn("calls the standard gate, which runs the review", out)
        self.assertEqual(self.contexts(self.ruleset_body("-X PUT"))[1], "Quality / claude-review / claude-review")

    def test_a_runner_that_is_not_json_is_refused(self):
        code, _, err = self.run_main([VIEW], "--runner", "ubuntu-latest")
        self.assertEqual(code, 2)
        self.assertEqual(self.r.calls(), [])


if __name__ == "__main__":
    unittest.main()
