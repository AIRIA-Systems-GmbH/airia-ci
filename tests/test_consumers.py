# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""scripts/consumers.py against a fake `gh`.

What these tests hold: consumers call @main (2026-10-20), so a pin on any
ci-vN tag is reported PINNED and fails the run, because a fix never reaches a
frozen tag; a lookup that fails is an error, never an empty "all current".

Run: python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

from steps import FAKE_RUFF, ROOT, Runner

SCRIPT = ROOT / "scripts/consumers.py"
spec = importlib.util.spec_from_file_location("consumers", SCRIPT)
consumers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(consumers)

CALLER = "uses: AIRIA-Systems-GmbH/airia-ci/.github/workflows/reusable-claude-review.yml@{}\n"


class Scan(unittest.TestCase):
    def run_main(self, rules, *owners):
        r = Runner(self, rules)
        r.tool("ruff", FAKE_RUFF)
        out, err = io.StringIO(), io.StringIO()
        consumers.workflows.cache_clear()
        with mock.patch.dict(os.environ, r.env()), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = consumers.main(list(owners or ["o"]))
        return code, out.getvalue(), err.getvalue(), r


class Consumers(Scan):
    def test_a_pin_on_any_tag_is_flagged_and_fails_the_run(self):
        code, out, _, _ = self.run_main([
            ["repo list o", {"json": [{"nameWithOwner": "o/new"}, {"nameWithOwner": "o/old"}]}],
            ["o/(new|old)/contents/.github/workflows --jq", {"json": [{"path": ".github/workflows/ci.yml"}]}],
            ["o/new/contents/.github/workflows/ci.yml", {"text": CALLER.format("main")}],
            ["o/old/contents/.github/workflows/ci.yml", {"text": CALLER.format("ci-v2")}],
        ])
        self.assertEqual(code, 1)
        self.assertIn("o/new\t.github/workflows/ci.yml\treusable-claude-review.yml@main\tcurrent", out)
        self.assertIn("o/old\t.github/workflows/ci.yml\treusable-claude-review.yml@ci-v2\tPINNED (not @main)", out)
        self.assertIn("2 consumers, 2 pins, 1 not on @main", out)

    def test_all_current_passes_and_skips_repos_without_workflows_and_airia_ci_itself(self):
        code, out, _, r = self.run_main([
            ["repo list o", {"json": [{"nameWithOwner": n} for n in
                                      ("o/a", "o/empty", "AIRIA-Systems-GmbH/airia-ci")]}],
            ["o/empty/contents", {"exit": 1, "stderr": "Not Found"}],
            ["o/a/contents/.github/workflows --jq", {"json": [{"path": ".github/workflows/ci.yml"}]}],
            ["o/a/contents/.github/workflows/ci.yml", {"text": CALLER.format("main")}],
        ])
        self.assertEqual(code, 0)
        self.assertIn("1 consumers, 1 pins, 0 not on @main", out)
        self.assertFalse([c for c in r.calls() if "airia-ci/contents" in " ".join(c["args"])],
                         "airia-ci's own workflows are not a consumer")

    def test_an_unreadable_owner_is_an_error_not_an_empty_list(self):
        code, out, err, _ = self.run_main([["repo list", {"exit": 1, "stderr": "HTTP 401"}]])
        self.assertEqual(code, 2)
        self.assertIn("cannot list o's repositories: HTTP 401", err)
        self.assertEqual(out, "")

    def test_an_unreadable_workflow_is_an_error(self):
        code, _, err, _ = self.run_main([
            ["repo list o", {"json": [{"nameWithOwner": "o/a"}]}],
            ["o/a/contents/.github/workflows --jq", {"json": [{"path": ".github/workflows/ci.yml"}]}],
            ["o/a/contents/.github/workflows/ci.yml", {"exit": 1, "stderr": "HTTP 403"}],
        ])
        self.assertEqual(code, 2)
        self.assertIn("cannot read o/a/.github/workflows/ci.yml: HTTP 403", err)


T = consumers.PINS["tools"]
STANDARD_CI = """\
on:
  pull_request:
jobs:
  ci:
    uses: AIRIA-Systems-GmbH/airia-ci/.github/workflows/reusable-python-gate.yml@main
"""
PYPROJECT = ("[project]\nname = 'app'\n[tool.pytest.ini_options]\naddopts = '--cov=app'\n"
             "[tool.ruff.lint]\nselect = ['E', 'W', 'F', 'I', 'B', 'C4', 'UP', 'SIM']\nignore = ['E501']\n")
LOCK = "".join(f'[[package]]\nname = "{n}"\nversion = "{T[n]}"\n' for n in ("pytest", "pytest-cov", "coverage"))
RULESET = {"name": "Restrict Direct Merge on Main", "bypass_actors": [], "rules": [
    {"type": "required_status_checks", "parameters": {"required_status_checks": [
        {"context": "ci / gate"}, {"context": "ci / claude-review / claude-review"}]}}]}


def consumer(ci=STANDARD_CI, pyproject=PYPROJECT, lock=LOCK, ruleset=RULESET, issues=(), extra=(), ruff=None):
    """The fake `gh` of one consumer, o/app; None for a file it does not have."""
    def file(path, text):
        return [f"o/app/contents/{path} ", {"exit": 1, "stderr": "gh: Not Found (HTTP 404)"} if text is None
                else {"text": text}]
    return [
        *extra,
        ["repo list o", {"json": [{"nameWithOwner": "o/app"}]}],
        ["o/app/contents/.github/workflows --jq", {"json": [{"path": ".github/workflows/ci.yml"}]}],
        file(".github/workflows/ci.yml", ci),
        file("pyproject.toml", pyproject),
        file("uv.lock", lock),
        file("ruff.toml", ruff),
        file(".ruff.toml", None),
        ["o/app/rulesets --jq", {"json": [{"name": "Restrict Direct Merge on Main", "id": 7}] if ruleset else []}],
        ["o/app/rulesets/7", {"json": ruleset}],
        ["issue list", {"json": [{"number": n, "title": t} for n, t in issues]}],
        ["issue create", {"text": "https://github.com/o/app/issues/12\n"}],
        ["issue (edit|close)", {"text": ""}],
    ]


class Conformance(Scan):
    def scan(self, *flags, **kw):
        return self.run_main(consumer(**kw), "--conformance", *flags, "o")

    def issue_calls(self, r, verb):
        return [c["args"] for c in r.calls() if c["args"][:2] == ["issue", verb]]

    def test_a_consumer_on_the_standard_has_no_drift_and_its_old_issue_is_closed(self):
        code, out, err, r = self.scan(issues=[(3, consumers.TITLE), (4, "something else")])
        self.assertEqual(code, 0, out + err)
        self.assertIn("o/app\t0 drift\tclosed #3", out)
        self.assertEqual([a[2] for a in self.issue_calls(r, "close")], ["3"])
        self.assertEqual(self.issue_calls(r, "create") + self.issue_calls(r, "edit"), [])

    def test_a_conforming_consumer_with_no_issue_files_nothing(self):
        code, out, _, r = self.scan()
        self.assertEqual(code, 0)
        self.assertIn("o/app\t0 drift\tno drift", out)
        self.assertIn("0 of 1 consumers drift from the standard", out)

    def test_every_kind_of_drift_lands_in_one_new_issue(self):
        ci = STANDARD_CI.replace("@main", "@ci-v2") + (
            "    with:\n      coverage-floor: 85\n      type-checker: ty\n"
            "  lint:\n    runs-on: x\n    steps:\n      - run: uvx ruff check\n")
        pyproject = PYPROJECT.replace("'--cov=app'", "'--cov=app --no-cov --cov-fail-under=80'") + (
            "[tool.coverage.report]\nfail_under = 70\n[tool.ty]\n")
        lock = LOCK + '[[package]]\nname = "ruff"\nversion = "0.1.0"\n'
        bypass = {**RULESET, "bypass_actors": [{"actor_type": "RepositoryRole", "actor_id": 5}],
                  "rules": [{"type": "required_status_checks", "parameters": {"required_status_checks": [
                      {"context": "ci / gate"}]}}]}
        code, out, _, r = self.scan(ci=ci, pyproject=pyproject, lock=lock, ruleset=bypass)
        self.assertEqual(code, 1)
        (create,) = self.issue_calls(r, "create")
        self.assertEqual(create[create.index("--title") + 1], consumers.TITLE)
        body = create[create.index("--body") + 1]
        for expected in ("calls airia-ci at `ci-v2`, not `@main`",
                         "runs ruff unpinned",
                         "uv.lock resolves ruff 0.1.0, below the pinned",
                         "fail_under is 70, under the standard's 90",
                         "`--no-cov`",
                         "`--cov-fail-under=80`, under the standard's 90",
                         "`coverage-floor: 85`, under the standard's 90",
                         "type-checker `ty` is not one the standard accepts",
                         "ty is configured or locked",
                         "can be bypassed (RepositoryRole 5)",
                         "`ci / claude-review / claude-review` is not a required check"):
            with self.subTest(expected=expected):
                self.assertIn(expected, body)
        self.assertIn("o/app\t11 drift\topened https://github.com/o/app/issues/12", out)

    def test_a_narrowed_ruff_floor_drifts(self):
        # The root configuration, as the gate reads it: a ruff.toml wins over pyproject.toml.
        code, out, _, r = self.scan(ruff="[lint]\nselect = ['E', 'F']\n[lint.per-file-ignores]\n'src/*' = ['F401']\n")
        self.assertEqual(code, 1)
        body = self.issue_calls(r, "create")[0][-1]
        self.assertIn("ruff floor: ruff.toml: ruff does not enable the floor's B006, B008, C401", body)
        self.assertIn("ruff floor: ruff.toml: per-file-ignores 'src/*' switches off the floor's F401", body)

    def test_a_ruff_that_cannot_check_the_floor_is_reported_as_drift(self):
        with mock.patch.dict(os.environ, {"FAKE_RUFF_FAIL": "--show-settings"}):
            code, _, _, r = self.scan()
        self.assertEqual(code, 1)
        self.assertIn("ruff floor not checked: `ruff check --show-settings", self.issue_calls(r, "create")[0][-1])

    def test_an_open_drift_issue_is_rewritten_not_duplicated(self):
        code, out, _, r = self.scan(ruleset=None, issues=[(3, consumers.TITLE)])
        self.assertEqual(code, 1)
        (edit,) = self.issue_calls(r, "edit")
        self.assertEqual(edit[2], "3")
        self.assertIn("run `init.py protect`", edit[edit.index("--body") + 1])
        self.assertEqual(self.issue_calls(r, "create"), [])
        self.assertIn("rewrote #3", out)

    def test_a_repository_with_its_own_gates_is_not_on_the_standard(self):
        own = "on:\n  pull_request:\njobs:\n  review:\n    uses: AIRIA-Systems-GmbH/airia-ci/.github/workflows/" \
              "reusable-claude-review.yml@main\n"
        code, out, _, _ = self.scan("--dry-run", ci=own, lock=None)
        self.assertEqual(code, 1)
        self.assertIn("no pull-request workflow calls `reusable-python-gate.yml`", out)
        self.assertIn("no uv.lock", out)
        # No gate, no check name to require yet: one line says what is wrong, not an unfollowable second.
        self.assertNotIn("is not a required check", out)

    def test_mypy_strict_outside_its_one_repository_drifts(self):
        ci = STANDARD_CI + "    with:\n      type-checker: mypy-strict\n"
        code, out, _, _ = self.scan("--dry-run", ci=ci)
        self.assertEqual(code, 1)
        self.assertIn("accepted only where it was already the gate", out)

    def test_a_dry_run_prints_and_writes_nothing(self):
        code, out, _, r = self.scan("--dry-run", ruleset=None)
        self.assertEqual(code, 1)
        self.assertIn("would file:", out)
        self.assertEqual([c for c in r.calls() if c["args"][0] == "issue"], [])
        code, out, _, _ = self.scan("--dry-run")
        self.assertIn("would close any open drift issue", out)

    def test_a_repository_without_pyproject_is_checked_for_pin_and_ruleset_only(self):
        code, out, _, _ = self.scan("--dry-run", pyproject=None)
        self.assertEqual(code, 1)
        self.assertIn("no pyproject.toml: only the pin and the ruleset are checked", out)
        self.assertNotIn("uv.lock", out)

    def test_a_lookup_that_fails_is_an_error_not_a_clean_scan(self):
        failing = {
            "cannot read o/app/pyproject.toml: HTTP 403": ["o/app/contents/pyproject.toml", {"exit": 1, "stderr": "HTTP 403"}],
            "cannot list o/app's rulesets: HTTP 403": ["o/app/rulesets --jq", {"exit": 1, "stderr": "HTTP 403"}],
            "cannot read o/app's ruleset: HTTP 404": ["o/app/rulesets/7", {"exit": 1, "stderr": "HTTP 404"}],
            "cannot list o/app's issues: HTTP 410": ["issue list", {"exit": 1, "stderr": "HTTP 410"}],
            "cannot write o/app's drift issue: HTTP 422": ["issue create", {"exit": 1, "stderr": "HTTP 422"}],
        }
        for message, rule in failing.items():
            with self.subTest(message):
                code, _, err, _ = self.run_main(consumer(ruleset=None if "write" in message else RULESET,
                                                         extra=[rule]), "--conformance", "o")
                self.assertEqual(code, 2)
                self.assertIn(message, err)

    def test_a_failed_close_is_an_error_not_a_close(self):
        code, out, err, _ = self.run_main(consumer(issues=[(3, consumers.TITLE)],
                                                   extra=[["issue close", {"exit": 1, "stderr": "HTTP 403"}]]),
                                          "--conformance", "o")
        self.assertEqual(code, 2)
        self.assertIn("cannot close o/app#3: HTTP 403", err)
        self.assertNotIn("closed #3", out)

    def test_one_unreadable_consumer_does_not_stop_the_others(self):
        rules = [
            ["o/bad/rulesets --jq", {"exit": 1, "stderr": "HTTP 403"}],
            ["o/bad/contents/.github/workflows --jq", {"json": [{"path": ".github/workflows/ci.yml"}]}],
            ["o/bad/contents/.github/workflows/ci.yml ", {"text": STANDARD_CI}],
            ["o/bad/contents/(pyproject.toml|uv.lock|\\.?ruff.toml) ", {"exit": 1, "stderr": "Not Found"}],
            *consumer(),
        ]
        rules[rules.index(["repo list o", {"json": [{"nameWithOwner": "o/app"}]}])] = \
            ["repo list o", {"json": [{"nameWithOwner": "o/app"}, {"nameWithOwner": "o/bad"}]}]
        code, out, err, _ = self.run_main(rules, "--conformance", "o")
        self.assertEqual(code, 2)
        self.assertIn("error: o/bad: cannot list o/bad's rulesets: HTTP 403", err)
        self.assertIn("o/app\t0 drift\tno drift", out)
        self.assertIn("0 of 2 consumers drift from the standard; 1 could not be scanned", out)

    def test_the_scheduled_scan_fails_without_its_token(self):
        r = Runner(self)
        wf = ROOT / ".github/workflows/conformance.yml"
        p = r.run(wf, "Require the conformance token", HAS_TOKEN="false")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=No conformance token::", p.stdout)
        self.assertEqual(r.run(wf, "Require the conformance token", HAS_TOKEN="true").returncode, 0)

    def test_the_scheduled_scan_fails_on_drift_through_its_pipe(self):
        r = Runner(self)
        (r.tmp / "pins.toml").write_text('[harness]\npyyaml = "6.0.3"\n[tools]\nruff = "0.17.0"\n')
        (r.tmp / "scripts").mkdir()
        r.tool("uv", '#!/bin/sh\necho "uv $*" > "$(dirname "$0")/uv-args"\necho "1 of 1 consumers drift"\nexit 1\n')
        p = r.run(ROOT / ".github/workflows/conformance.yml", "Scan every consumer and keep its drift issue")
        self.assertEqual(p.returncode, 1)
        self.assertIn("--with pyyaml==6.0.3 --with ruff==0.17.0 python3 scripts/consumers.py --conformance "
                      "AIRIA-Systems-GmbH tumma72",
                      (r.bin / "uv-args").read_text())
        self.assertIn("1 of 1 consumers drift", r.summary.read_text())


if __name__ == "__main__":
    unittest.main()
