# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""reusable-python-gate.yml: the standard's gates, and the one check that sums them.

What these tests hold: no input switches a gate off; `gate` needs every gate
job and is red unless each one, and each of the caller's extra jobs,
succeeded (skipped counts as not passed); the review runs after `gate`, even a
red one, at the same tag as the actions; each gate step runs every check it
names and fails on any; pytest without a coverage figure fails rather than
passing unmeasured, and always prints its slowest tests.

The docs-only fast path: with `docs-paths` empty, or on any event but a pull
request, `changes` says code changed and everything runs, as before; a PR
whose every path (old and new, for a rename) is docs skips lint and test, and
`gate` accepts exactly those two skips and nothing else.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
import unittest
from pathlib import Path

import yaml
from steps import ROOT, Runner

WORKFLOW = ROOT / ".github/workflows/reusable-python-gate.yml"
SHARD_PY = ROOT / ".github/actions/python-toolchain/shard.py"
DOC = yaml.safe_load(WORKFLOW.read_text())
JOBS = DOC["jobs"]
INPUTS = DOC[True]["workflow_call"]["inputs"]  # PyYAML reads the key `on` as True
PINS = tomllib.loads((ROOT / "pins.toml").read_text())

# A fake tool: records its argv, exits with $FAKE_<NAME>_EXIT.
FAKE = """\
#!/bin/sh
name="$(basename "$0" | tr 'a-z-' 'A-Z_')"
echo "$(basename "$0") $*" >> "$(dirname "$0")/calls"
eval "code=\\${{FAKE_${{name}}_EXIT:-0}}"
exit "$code"
"""
# pytest: also writes the JSON coverage report it was asked for, at $FAKE_PERCENT.
FAKE_PYTEST = """\
#!/bin/sh
# `pytest --help` lists -n/--numprocesses only when pytest-xdist is loaded (here: unless $FAKE_NO_XDIST), and
# --timeout only when pytest-timeout is (unless $FAKE_NO_PTIMEOUT).
if [ "$1" = --help ]; then
  echo "  --cov=SOURCE"
  [ -z "$FAKE_NO_XDIST" ] && echo "  -n numprocesses, --numprocesses=numprocesses"
  [ -z "$FAKE_NO_PTIMEOUT" ] && echo "  --timeout=TIMEOUT     Timeout in seconds for each test (pytest-timeout)"
  exit 0
fi
echo "pytest $*" >> "$(dirname "$0")/calls"
# `--collect-only`: print $FAKE_COLLECT, the listing a real pytest would give (exit $FAKE_COLLECT_EXIT).
case " $* " in *" --collect-only "*) printf '%s\\n' "$FAKE_COLLECT"; exit "${FAKE_COLLECT_EXIT:-0}";; esac
for a in "$@"; do
  case "$a" in --cov-report=json:*) [ -n "$FAKE_PERCENT" ] && echo "{\\"totals\\": {\\"percent_covered\\": $FAKE_PERCENT}}" > "${a#--cov-report=json:}";; esac
done
exit "${FAKE_PYTEST_EXIT:-0}"
"""


class Shape(unittest.TestCase):
    def test_no_input_switches_a_gate_off(self):
        # May add, may not weaken: nothing named skip/disable/allow-failure exists to pass.
        for name in INPUTS:
            self.assertNotRegex(name, r"skip|disable|no-|allow-fail|continue")
        self.assertEqual(set(INPUTS), {
            "type-checker", "coverage-floor", "python-versions", "paths", "extra-jobs", "postgres-image",
            "qdrant-image", "job-env", "runner", "durations", "docs-paths", "extra-allow", "extra-deny",
            "extra-prompt", "debt-ratchet", "pytest-workers", "test-timeout-minutes",
            "pytest-timeout-seconds", "test-budget-minutes", "shards", "records-paths", "records-tests"})

    def test_the_debt_ratchet_is_off_by_default_and_reaches_every_toolchain_call(self):
        """Default false keeps every existing caller as it is; the input must reach each job's toolchain call,
        or a job would hold the standard while another held the baseline."""
        self.assertEqual(INPUTS["debt-ratchet"]["type"], "boolean")
        self.assertIs(INPUTS["debt-ratchet"]["default"], False)
        for name in ("lint", "test", "security"):
            with self.subTest(job=name):
                toolchain = next(s for s in JOBS[name]["steps"] if "python-toolchain" in s.get("uses", ""))
                self.assertEqual(toolchain["with"]["debt-ratchet"], "${{ inputs.debt-ratchet }}")

    def test_the_pytest_floor_is_the_toolchains_with_the_input_as_the_fallback(self):
        """Without the ratchet the toolchain output is empty and the step holds the input, as before."""
        steps = JOBS["test"]["steps"]
        toolchain = next(s for s in steps if "python-toolchain" in s.get("uses", ""))
        pytest = next(s for s in steps if s.get("name") == "Pytest at the coverage floor")
        self.assertEqual(pytest["env"]["FLOOR"],
                         f"${{{{ steps.{toolchain['id']}.outputs.coverage-floor || inputs.coverage-floor }}}}")

    def test_the_lint_checkout_has_the_base_commit_for_the_compare(self):
        self.assertEqual(JOBS["lint"]["steps"][0]["with"]["fetch-depth"], 2)

    def test_the_new_inputs_change_nothing_by_default(self):
        # Every existing caller keeps today's behaviour: no fast path, the 25 slowest tests listed.
        self.assertEqual(INPUTS["docs-paths"]["default"], "")
        self.assertEqual(INPUTS["durations"]["default"], 25)

    def test_pytest_workers_are_an_opt_in_that_reaches_the_pytest_step(self):
        # Empty today: a suite that cannot run under xdist yet keeps its own addopts. The intended default is 4.
        self.assertEqual(INPUTS["pytest-workers"]["type"], "string")
        self.assertEqual(INPUTS["pytest-workers"]["default"], "")
        self.assertNotIn("concurrency", json.dumps(JOBS))  # the per-repository cap is set by runner pool, not here
        pytest = next(s for s in JOBS["test"]["steps"] if s.get("name") == "Pytest at the coverage floor")
        self.assertEqual(pytest["env"]["WORKERS"], "${{ inputs.pytest-workers }}")

    def test_the_timeouts_are_step_limits_only_and_an_opt_in_per_test_limit(self):
        # airia-ci#161: step limit ~2x the suite; no job has a timeout, the host scheduler governs the rest.
        self.assertEqual(INPUTS["test-timeout-minutes"]["type"], "string")
        self.assertEqual(INPUTS["test-timeout-minutes"]["default"], "60", "today's limit: no caller turns red")
        self.assertEqual(INPUTS["pytest-timeout-seconds"]["type"], "string")
        self.assertEqual(INPUTS["pytest-timeout-seconds"]["default"], "", "off unless a caller opts in")
        steps = JOBS["test"]["steps"]
        pytest = next(s for s in steps if s.get("name") == "Pytest at the coverage floor")
        self.assertEqual(pytest["timeout-minutes"], "${{ fromJSON(inputs.test-timeout-minutes) }}")
        self.assertEqual(pytest["env"]["TEST_TIMEOUT"], "${{ inputs.pytest-timeout-seconds }}")
        check = next(s for s in steps if s.get("name") == "Test timeout check")
        self.assertEqual(check["env"]["STEP_TIMEOUT"], "${{ inputs.test-timeout-minutes }}")
        self.assertLess(steps.index(check), steps.index(pytest), "refused before pytest")
        # Andrea, 2026-11-24: step timeouts only; no job in the file has a timeout-minutes.
        for name, job in JOBS.items():
            self.assertNotIn("timeout-minutes", job, name)
        for name, limit in {"changes": 5, "lint": 20, "security": 20, "gate": 5}.items():
            for s in JOBS[name]["steps"]:
                self.assertEqual(s["timeout-minutes"], limit, f"{name}: {s.get('name') or s.get('uses')}")
        for s in steps:
            if s is not pytest:  # the test job's other steps have no limit (even the shard upload)
                self.assertNotIn("timeout-minutes", s, s.get("name") or s.get("uses"))

    def test_the_test_budget_is_a_warning_only_input_that_reaches_both_steps(self):
        # airia-ci#156: nothing here may fail a job or cap it, so no job timeout and no concurrency.
        self.assertEqual(INPUTS["test-budget-minutes"]["type"], "string")
        self.assertEqual(INPUTS["test-budget-minutes"]["default"], "10")
        self.assertNotIn("timeout-minutes", JOBS["test"])  # airia-ci#161: step timeouts only
        for name, job in JOBS.items():
            self.assertNotIn("concurrency", job, name)
        self.assertNotIn("concurrency", DOC)
        steps = JOBS["test"]["steps"]
        names = [s.get("name") for s in steps]
        i = names.index("Pytest at the coverage floor")
        self.assertEqual(names[i - 1:i + 2], ["Test budget clock", "Pytest at the coverage floor", "Test budget report"])
        clock, report = steps[i - 1], steps[i + 1]
        self.assertEqual(clock["env"]["BUDGET"], "${{ inputs.test-budget-minutes }}")
        self.assertEqual(report["env"]["BUDGET"], "${{ inputs.test-budget-minutes }}")
        self.assertEqual(report["env"]["STARTED"], f"${{{{ steps.{clock['id']}.outputs.epoch }}}}")
        self.assertEqual(report["if"], "always()")
        self.assertNotIn("continue-on-error", report)

    def test_the_defaults_are_the_standard(self):
        self.assertEqual(INPUTS["coverage-floor"]["default"], PINS["standard"]["coverage-floor"])
        self.assertEqual(INPUTS["type-checker"]["default"], PINS["standard"]["type-checkers"][0])

    def test_gate_needs_every_gate_job_and_runs_when_one_failed(self):
        gates = set(JOBS) - {"gate", "claude-review"}
        self.assertEqual(gates, {"changes", "lint", "test", "coverage", "security"})
        self.assertEqual(set(JOBS["gate"]["needs"]), gates)
        self.assertEqual(JOBS["gate"]["if"], "always()")
        self.assertEqual(JOBS["gate"]["steps"][0]["env"]["CODE"], "${{ needs.changes.outputs.code }}")

    def test_no_gate_job_has_a_condition_or_tolerates_failure(self):
        # A job that can be skipped, or a step that may fail, is a gate that can be switched off.
        # The one condition: lint and test wait for `changes` and skip on a docs-only PR.
        # security never skips: REUSE and gitleaks hold for a doc too.
        # `coverage` also waits for `test` and only exists for a sharded run (`gate` excuses its skip at shards = 1 only).
        conditions = {"lint": "needs.changes.outputs.code == 'true' && needs.changes.outputs.scope != 'records'",
                      "test": "needs.changes.outputs.code == 'true'",
                      "coverage": "needs.changes.outputs.code == 'true' && needs.changes.outputs.scope != 'records' && inputs.shards > 1"}
        for name in ("changes", "lint", "test", "coverage", "security"):
            job = JOBS[name]
            with self.subTest(job=name):
                self.assertEqual(job.get("if"), conditions.get(name))
                self.assertEqual(job.get("needs"), (["changes", "test"] if name == "coverage" else ["changes"])
                                 if name in conditions else None)
                self.assertNotIn("continue-on-error", job)
                for s in job["steps"]:
                    self.assertNotIn("continue-on-error", s)
                    if not s.get("uses", "").startswith("actions/checkout@") and s.get("name") not in (
                            "Test budget report", "Upload this shard's coverage data"):
                        self.assertNotIn("if", s)

    def test_changes_reads_the_merge_commit_only_when_it_needs_to(self):
        _, checkout, diff = JOBS["changes"]["steps"]
        self.assertEqual(checkout["if"], "github.event_name == 'pull_request' && (inputs.docs-paths != '' || "
                                         "(inputs.records-paths != '' && inputs.records-tests != ''))")
        self.assertEqual(checkout["with"], {"fetch-depth": 2, "persist-credentials": False})
        self.assertNotIn("ref", checkout["with"])  # the default on a pull request: refs/pull/N/merge
        self.assertEqual(JOBS["changes"]["outputs"], {"code": f"${{{{ steps.{diff['id']}.outputs.code }}}}",
                                                      "scope": f"${{{{ steps.{diff['id']}.outputs.scope }}}}"})

    def test_every_gate_job_starts_from_the_standard_toolchain(self):
        for name in ("lint", "test", "security"):
            checkout, toolchain = JOBS[name]["steps"][:2]
            with self.subTest(job=name):
                self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
                self.assertFalse(checkout["with"]["persist-credentials"])
                self.assertRegex(toolchain["uses"], r"^AIRIA-Systems-GmbH/airia-ci/\.github/actions/python-toolchain@")
                self.assertEqual(toolchain["with"]["coverage-floor"], "${{ inputs.coverage-floor }}")
                self.assertEqual(toolchain["with"]["type-checker"], "${{ inputs.type-checker }}")

    def test_the_review_runs_after_the_gate_even_a_red_one_and_at_the_same_tag(self):
        review = JOBS["claude-review"]
        self.assertEqual(review["needs"], ["gate"])
        self.assertTrue(review["if"].startswith("always() && github.event_name == 'pull_request'"))
        self.assertEqual(review["secrets"], {
            "CLAUDE_CODE_OAUTH_TOKEN": "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}",
            "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK": "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK }}",
        })
        for name in ("runner", "extra-allow", "extra-deny", "extra-prompt"):
            self.assertEqual(review["with"][name], f"${{{{ inputs.{name} }}}}")
        tags = set(re.findall(r"AIRIA-Systems-GmbH/airia-ci/\S+@(\S+)", WORKFLOW.read_text()))
        self.assertEqual(len(tags), 1, tags)

    def test_every_review_input_the_gate_passes_exists_on_the_review(self):
        review = yaml.safe_load((ROOT / ".github/workflows/reusable-claude-review.yml").read_text())
        self.assertLessEqual(set(JOBS["claude-review"]["with"]), set(review[True]["workflow_call"]["inputs"]))

    def test_the_header_names_the_checks_github_reports(self):
        # `<caller job> / <called job>`, nested once more for the review.
        text = WORKFLOW.read_text()
        self.assertIn("`ci / gate` and `ci / claude-review / claude-review`", text)
        self.assertIn("gate", JOBS)
        review = yaml.safe_load((ROOT / ".github/workflows/reusable-claude-review.yml").read_text())
        self.assertEqual(list(review["jobs"]), ["claude-review"])


class Steps(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        for tool in ("ruff", "pyright", "mypy", "lint-imports", "reuse", "uv", "pip-audit", "gitleaks", "zizmor"):
            self.r.tool(tool, FAKE.format())
        self.r.tool("pytest", FAKE_PYTEST)
        self.project = self.r.tmp / "project"
        self.project.mkdir()
        (self.project / "pyproject.toml").write_text("[project]\nname = 'p'\n")

    def run_step(self, name: str, **env):
        return self.r.run(WORKFLOW, name, cwd=self.project, **env)

    def calls(self) -> list[str]:
        f = self.r.bin / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def test_ruff_runs_both_checks_on_every_path_even_when_the_first_fails(self):
        p = self.run_step("Ruff", PATHS="src tests", FAKE_RUFF_EXIT="1")
        self.assertEqual(p.returncode, 1)
        skip = "--extend-exclude migrations --force-exclude"
        self.assertEqual(self.calls(), [f"ruff check {skip} src tests", f"ruff format --check {skip} src tests"])

    def test_ruff_passes_when_both_pass(self):
        self.assertEqual(self.run_step("Ruff", PATHS=".").returncode, 0)

    def test_the_ruff_floor_step_runs_the_toolchains_check_on_the_paths(self):
        # The python-toolchain action exports AIRIA_TOOLCHAIN; its `ruff` command is tested with the action.
        fake = self.r.tmp / "toolchain.py"
        fake.write_text('import os, sys\nprint("toolchain", *sys.argv[1:], os.environ["PATHS"])\n'
                        'sys.exit(int(os.environ.get("FAKE_FLOOR_EXIT", "0")))\n')
        p = self.run_step("Ruff floor and suppressions", PATHS="src tests", AIRIA_TOOLCHAIN=str(fake))
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "toolchain ruff src tests"))
        p = self.run_step("Ruff floor and suppressions", PATHS=".", AIRIA_TOOLCHAIN=str(fake), FAKE_FLOOR_EXIT="1")
        self.assertEqual(p.returncode, 1)

    def test_pyright_checks_against_the_project_environment(self):
        p = self.run_step("Type check", PATHS="src", TYPE_CHECKER="pyright")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.calls(), [f"pyright --pythonpath {self.project.resolve()}/.venv/bin/python src"])

    def test_mypy_strict_is_strict(self):
        p = self.run_step("Type check", PATHS="src", TYPE_CHECKER="mypy-strict", FAKE_MYPY_EXIT="1")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.calls(), ["mypy --strict src"])

    def test_an_unknown_type_checker_fails(self):
        p = self.run_step("Type check", PATHS="src", TYPE_CHECKER="ty")
        self.assertEqual(p.returncode, 1)
        self.assertIn("no type checker ty", p.stdout)

    def test_import_contracts_run_when_pyproject_configures_them(self):
        (self.project / "pyproject.toml").write_text("[tool.importlinter]\nroot_package = 'p'\n")
        p = self.run_step("Import contracts", FAKE_LINT_IMPORTS_EXIT="1")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.calls(), ["lint-imports "])

    def test_import_contracts_run_from_an_importlinter_file(self):
        (self.project / ".importlinter").write_text("[importlinter]\n")
        self.assertEqual(self.run_step("Import contracts").returncode, 0)
        self.assertEqual(self.calls(), ["lint-imports "])

    def test_no_import_contracts_is_said_not_failed(self):
        p = self.run_step("Import contracts")
        self.assertEqual(p.returncode, 0)
        self.assertIn("No import-linter contracts", p.stdout)
        self.assertEqual(self.calls(), [])

    def test_pytest_at_or_over_the_floor_passes(self):
        p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="90.0", DURATIONS="25")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (call,) = self.calls()
        self.assertIn("--cov-fail-under=90", call)
        self.assertIn(f"--cov-report=json:{self.r.tmp}/coverage.json", call)

    def test_pytest_always_lists_the_slowest_tests(self):
        # The report is how a slow suite is found from CI; 0 lists every test, it does not switch it off.
        for n in ("25", "0", "100"):
            with self.subTest(durations=n):
                (self.r.bin / "calls").unlink(missing_ok=True)
                p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="95", DURATIONS=n)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                (call,) = self.calls()
                self.assertTrue(call.startswith(f"pytest --durations={n} "), call)

    def test_durations_that_are_not_a_count_fail_before_pytest(self):
        for n in ("", "-1", "2.5", "all"):
            with self.subTest(durations=n):
                p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="95", DURATIONS=n)
                self.assertEqual(p.returncode, 1)
                self.assertIn("::error title=durations::", p.stdout)
                self.assertEqual(self.calls(), [])

    def budget_step(self, now: int, budget: str, started: str):
        # `date` is faked, so the elapsed time is exact: the shipped block reads the clock only through it.
        self.r.tool("date", f"#!/bin/sh\necho {now}\n")
        return self.run_step("Test budget report", BUDGET=budget, STARTED=started)

    def summary(self) -> str:
        return self.r.summary.read_text() if self.r.summary.exists() else ""

    def test_a_pytest_within_the_budget_is_summarised_without_a_warning(self):
        p = self.budget_step(1000 + 9 * 60, "10", "1000")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("::warning", p.stdout)
        self.assertEqual(self.summary().strip(), "pytest took 9 min, within the 10-minute budget")

    def test_a_pytest_over_the_budget_warns_with_both_numbers_and_never_fails(self):
        # 10 min 1 s rounds up to 11.
        p = self.budget_step(1000 + 10 * 60 + 1, "10", "1000")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        line = "pytest took 11 min against a budget of 10 min (airia-ci#156)"
        self.assertIn(f"::warning title=Test budget::{line}", p.stdout)
        self.assertEqual(self.summary().strip(), line)

    def test_a_pytest_exactly_at_the_budget_does_not_warn(self):
        p = self.budget_step(1000 + 10 * 60, "10", "1000")
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("::warning", p.stdout)
        self.assertIn("within the 10-minute budget", self.summary())

    def test_an_empty_budget_turns_the_report_off(self):
        p = self.budget_step(1000 + 99 * 60, "", "1000")
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("::warning", p.stdout)
        self.assertEqual(self.summary(), "")

    def test_a_missing_start_epoch_ends_zero_with_nothing_reported(self):
        for started in ("", "x"):
            with self.subTest(started=started):
                p = self.budget_step(5000, "10", started)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotIn("::warning", p.stdout)
                self.assertEqual(self.summary(), "")

    def test_the_clock_step_records_the_start_epoch(self):
        self.r.tool("date", "#!/bin/sh\necho 1234\n")
        p = self.run_step("Test budget clock", BUDGET="10")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.r.outputs()["epoch"], "1234")
        p = self.run_step("Test budget clock", BUDGET="")
        self.assertEqual(p.returncode, 0)

    def test_a_budget_that_is_not_a_positive_whole_number_is_refused_before_pytest(self):
        for b in ("0", "x; rm", "-5", "2.5", "010", "ten"):
            with self.subTest(budget=b):
                p = self.run_step("Test budget clock", BUDGET=b)
                self.assertEqual(p.returncode, 1)
                self.assertIn("::error title=test-budget-minutes::", p.stdout)
                self.assertEqual(self.calls(), [])

    def test_a_step_timeout_that_is_not_a_positive_whole_number_is_refused_before_pytest(self):
        for t in ("", "0", "x; rm", "-5", "2.5", "060", "sixty", "1000000"):
            with self.subTest(timeout=t):
                p = self.run_step("Test timeout check", STEP_TIMEOUT=t)
                self.assertEqual(p.returncode, 1)
                self.assertIn("::error title=test-timeout-minutes::", p.stdout)
                self.assertEqual(self.calls(), [])

    def test_a_positive_whole_step_timeout_is_accepted(self):
        for t in ("60", "1", "120"):
            with self.subTest(timeout=t):
                self.assertEqual(self.run_step("Test timeout check", STEP_TIMEOUT=t).returncode, 0)

    def pytest_step(self, **env):
        return self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="95", DURATIONS="25", **env)

    def test_the_per_test_timeout_adds_timeout_to_pytest(self):
        p = self.pytest_step(TEST_TIMEOUT="300")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (call,) = self.calls()
        self.assertTrue(call.startswith("pytest --timeout=300 --durations=25 "), call)

    def test_workers_and_the_per_test_timeout_combine(self):
        p = self.pytest_step(WORKERS="4", TEST_TIMEOUT="300")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(self.calls()[0].startswith("pytest -n 4 --timeout=300 --durations=25 "))

    def test_no_per_test_timeout_means_no_flag_and_no_plugin_needed(self):
        p = self.pytest_step(TEST_TIMEOUT="", FAKE_NO_PTIMEOUT="1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (call,) = self.calls()
        self.assertTrue(call.startswith("pytest --durations=25 "), call)
        self.assertNotIn("--timeout", call)

    def test_a_per_test_timeout_that_is_not_a_positive_whole_number_is_refused_before_pytest(self):
        for t in ("0", "x; rm", "-1", "2.5", "030", "5m", "3 4"):
            with self.subTest(timeout=t):
                p = self.pytest_step(TEST_TIMEOUT=t)
                self.assertEqual(p.returncode, 1)
                self.assertIn("::error title=pytest-timeout-seconds::", p.stdout)
                self.assertEqual(self.calls(), [])

    def test_a_per_test_timeout_without_the_plugin_fails_naming_the_fix_and_runs_nothing(self):
        p = self.pytest_step(TEST_TIMEOUT="300", FAKE_NO_PTIMEOUT="1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=No pytest-timeout::", p.stdout)
        self.assertIn("Add pytest-timeout to the project's dev dependencies (and refresh uv.lock)", p.stdout)
        self.assertIn("or set pytest-timeout-seconds to empty.", p.stdout)
        self.assertEqual(self.calls(), [])

    def test_the_debt_ratchet_pytest_gets_the_timeout_too_and_fails_without_the_plugin(self):
        debt = self.r.tmp / "debt.py"
        debt.write_text("import sys\nprint('debt', *sys.argv[1:])\n")
        p = self.pytest_step(TEST_TIMEOUT="300", RATCHET="true", AIRIA_DEBT=str(debt))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(self.calls()[0].startswith("pytest --timeout=300 --durations=25 -rfE "), self.calls())
        self.r.bin.joinpath("calls").unlink()
        p = self.pytest_step(TEST_TIMEOUT="300", RATCHET="true", FAKE_NO_PTIMEOUT="1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=No pytest-timeout::", p.stdout)
        self.assertEqual(self.calls(), [])

    def test_workers_add_n_to_pytest(self):
        p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="95", DURATIONS="25", WORKERS="4")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (call,) = self.calls()
        self.assertTrue(call.startswith("pytest -n 4 --durations=25 "), call)

    def test_auto_workers_are_accepted(self):
        p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="95", DURATIONS="25", WORKERS="auto")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(self.calls()[0].startswith("pytest -n auto "))

    def test_empty_workers_leave_the_projects_addopts_alone_and_need_no_xdist(self):
        p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="95", DURATIONS="25", WORKERS="",
                          FAKE_NO_XDIST="1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (call,) = self.calls()
        self.assertTrue(call.startswith("pytest --durations=25 "), call)
        self.assertNotIn(" -n ", call)

    def test_workers_that_are_not_a_positive_number_or_auto_are_refused_before_pytest(self):
        for w in ("0", "x; rm", "-1", "2.5", "04", "AUTO", "4 8"):
            with self.subTest(workers=w):
                p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="95", DURATIONS="25",
                                  WORKERS=w)
                self.assertEqual(p.returncode, 1)
                self.assertIn("::error title=pytest-workers::", p.stdout)
                self.assertEqual(self.calls(), [])

    def test_workers_without_xdist_fail_naming_the_fix_and_never_run_serially(self):
        p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="95", DURATIONS="25", WORKERS="4",
                          FAKE_NO_XDIST="1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=No xdist::", p.stdout)
        self.assertIn("pytest-xdist", p.stdout)
        self.assertIn("dev dependencies", p.stdout)
        self.assertEqual(self.calls(), [])  # no pytest run at all, serial or otherwise

    def test_the_debt_ratchet_pytest_gets_n_too(self):
        debt = self.r.tmp / "debt.py"
        debt.write_text("import sys\nprint('debt', *sys.argv[1:])\n")
        p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="95", DURATIONS="25", WORKERS="4",
                          RATCHET="true", AIRIA_DEBT=str(debt))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (call,) = self.calls()
        self.assertTrue(call.startswith("pytest -n 4 --durations=25 -rfE "), call)

    def test_the_debt_ratchet_without_xdist_fails_too(self):
        p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="95", DURATIONS="25", WORKERS="4",
                          RATCHET="true", FAKE_NO_XDIST="1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=No xdist::", p.stdout)

    def test_pytest_under_the_floor_fails_even_if_pytest_did_not(self):
        # pytest-cov's own fail-under is the first line; the report check is the second.
        p = self.run_step("Pytest at the coverage floor", FLOOR="92.5", FAKE_PERCENT="92.4", DURATIONS="25")
        self.assertEqual(p.returncode, 1)

    def test_no_coverage_report_fails_with_how_to_measure(self):
        # `--no-cov` in addopts: pytest passes, writes no report, and holds no floor.
        (self.r.tmp / "coverage.json").write_text('{"totals": {"percent_covered": 100}}')  # a stale one
        p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="", DURATIONS="25")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=No coverage figure::", p.stdout)
        self.assertIn("--cov=<package>", p.stdout)

    def test_a_failing_test_fails_the_step(self):
        p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="100", FAKE_PYTEST_EXIT="1",
                          DURATIONS="25")
        self.assertEqual(p.returncode, 1)

    def test_reuse_lints_the_whole_tree(self):
        self.assertEqual(self.run_step("Licences (REUSE)", FAKE_REUSE_EXIT="1").returncode, 1)
        self.assertEqual(self.calls(), ["reuse lint"])

    def test_pip_audit_reads_the_locked_requirements_without_installing(self):
        p = self.run_step("Known vulnerabilities in the locked dependencies")
        self.assertEqual(p.returncode, 0)
        req = f"{self.r.tmp}/requirements.txt"
        self.assertEqual(self.calls(), [
            f"uv export --locked --no-emit-workspace --format requirements-txt --output-file {req}",
            f"pip-audit --disable-pip --requirement {req}",
        ])

    def test_a_lock_that_does_not_export_fails_before_the_audit(self):
        p = self.run_step("Known vulnerabilities in the locked dependencies", FAKE_UV_EXIT="2")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(len(self.calls()), 1)

    def test_gitleaks_scans_the_tree_redacted(self):
        self.assertEqual(self.run_step("Secrets in the tree", FAKE_GITLEAKS_EXIT="1").returncode, 1)
        self.assertEqual(self.calls(), ["gitleaks dir --redact --no-banner ."])

    def test_zizmor_reads_github_with_the_standard_policy_offline(self):
        p = self.run_step("Workflow security", ZIZMOR_CONFIG="/a/zizmor.yml")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.calls(), ["zizmor --offline --config /a/zizmor.yml .github"])


def needs(**results: str) -> str:
    return json.dumps({job: {"result": r, "outputs": {}} for job, r in results.items()})


def git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env={**os.environ, **env}).stdout


class Changes(unittest.TestCase):
    """`changes` on a real checkout: the PR's merge commit, as actions/checkout leaves it."""

    STEP = "Did the change touch code"
    DOCS = "docs/**\n**/*.md"

    def setUp(self):
        self.r = Runner(self)
        self.repo = self.r.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.write({"README.md": "r", "src/app.py": "a", "docs/guide.md": "g", ".airia/notes.md": "n"})
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()

    def write(self, files: dict[str, str | None]) -> None:
        for name, text in files.items():
            f = self.repo / name
            if text is None:
                f.unlink()
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text)

    def pr(self, files: dict[str, str | None] | None = None, rename: tuple[str, str] | None = None) -> None:
        """A PR branch with these changes, merged into main --no-ff: HEAD is the merge commit."""
        git(self.repo, "checkout", "-qb", "pr")
        if rename:
            (self.repo / rename[1]).parent.mkdir(parents=True, exist_ok=True)
            git(self.repo, "mv", *rename)
        self.write(files or {})
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "pr")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")

    def code(self, docs: str | None = None, event: str = "pull_request"):
        p = self.r.run(WORKFLOW, self.STEP, cwd=self.repo, EVENT=event, DOCS_PATHS=self.DOCS if docs is None else docs)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return self.r.outputs()["code"], p.stdout

    def test_a_docs_only_pr_skips_the_code_gates(self):
        self.pr({"docs/guide.md": "g2", "docs/new/page.md": "p", "README.md": "r2"})
        code, out = self.code()
        self.assertEqual(code, "false")
        self.assertIn("docs/new/page.md", out)
        self.assertIn("code changed: false", self.r.summary.read_text())

    def test_a_pr_that_touches_code_runs_everything_and_names_it(self):
        self.pr({"docs/guide.md": "g2", "src/app.py": "a2"})
        code, out = self.code()
        self.assertEqual(code, "true")
        self.assertIn("  src/app.py", out)
        self.assertNotIn("  docs/guide.md", out)

    def test_no_docs_paths_is_todays_behaviour(self):
        # Opt-in: without the input, a docs-only PR still runs every gate, and nothing is diffed.
        self.pr({"docs/guide.md": "g2"})
        for docs in ("", " \n "):
            with self.subTest(docs=docs):
                code, out = self.code(docs=docs)
                self.assertEqual(code, "true")
                self.assertIn("Every gate runs: no docs-paths", out)

    def test_a_push_runs_everything(self):
        self.pr({"docs/guide.md": "g2"})
        code, out = self.code(event="push")
        self.assertEqual(code, "true")
        self.assertIn("not a pull request (push)", out)

    def test_moving_code_into_docs_is_a_code_change(self):
        # Without --no-renames, git would list only docs/app.md, and the deleted source would pass as docs.
        self.pr(rename=("src/app.py", "docs/app.md"))
        code, out = self.code()
        self.assertEqual(code, "true")
        self.assertIn("  src/app.py", out)

    def test_moving_docs_within_docs_is_docs(self):
        self.pr(rename=("docs/guide.md", "docs/old/guide.md"))
        self.assertEqual(self.code()[0], "false")

    def test_an_empty_diff_runs_everything(self):
        self.pr()
        code, out = self.code()
        self.assertEqual(code, "true")
        self.assertIn("changes no file", out)

    def test_some_paths_are_never_docs_whatever_the_patterns_say(self):
        # `.airia/` is read by suites; workflows, tests and tool configuration change what the gates do.
        for path in (".airia/notes.md", ".github/workflows/ci.yml", "tests/data/case.md", "src/tests/case.json", "pyproject.toml",
                     "uv.lock", ".python-version", "docs/conf.py", "pytest.ini", "ruff.toml", ".coveragerc",
                     "requirements-dev.txt", "docker/Dockerfile"):
            with self.subTest(path=path):
                git(self.repo, "reset", "-q", "--hard", self.base)
                git(self.repo, "branch", "-qD", "pr") if "pr" in git(self.repo, "branch") else None
                self.pr({path: "changed"})
                code, out = self.code(docs="**")
                self.assertEqual(code, "true")
                self.assertIn(f"  {path}", out)

    def test_a_checkout_that_is_not_a_merge_commit_runs_everything(self):
        self.write({"docs/guide.md": "g2"})
        git(self.repo, "commit", "-qam", "straight onto main")
        code, out = self.code()
        self.assertEqual(code, "true")
        self.assertIn("::notice title=Every gate runs::", out)


class Gate(unittest.TestCase):
    STEP = "Every gate succeeded"

    def verdict(self, own: str, extra: str = "{}", code: str = "true", shards: str = "1"):
        r = Runner(self)
        p = r.run(WORKFLOW, self.STEP, OWN=own, EXTRA=extra, CODE=code, SHARDS=shards)
        return p, r

    def test_docs_only_passes_with_lint_and_test_skipped(self):
        p, r = self.verdict(needs(changes="success", lint="skipped", test="skipped", security="success"), code="false")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Docs only: lint and test were not run.", r.summary.read_text())
        self.assertIn("- test: skipped", r.summary.read_text())

    def test_a_skipped_test_on_a_code_change_fails(self):
        p, _ = self.verdict(needs(changes="success", lint="success", test="skipped", security="success"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Gate not passed::test: skipped", p.stdout)

    def test_a_failing_test_fails_even_a_docs_only_gate(self):
        # Only a skip is excused, and only for lint and test.
        p, _ = self.verdict(needs(changes="success", lint="skipped", test="failure", security="success"), code="false")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Gate not passed::test: failure", p.stdout)

    def test_docs_only_never_excuses_security_changes_or_an_extra_job(self):
        own = needs(changes="success", lint="skipped", test="skipped", security="skipped")
        p, _ = self.verdict(own, needs(e2e="skipped"), code="false")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("::error title=Gate not passed::"), 2)
        self.assertIn("security: skipped", p.stdout)
        self.assertIn("extra: e2e: skipped", p.stdout)

    def test_a_failed_changes_job_fails_the_gate(self):
        # No answer from `changes` (CODE empty) is not "docs only".
        p, _ = self.verdict(needs(changes="failure", lint="skipped", test="skipped", security="success"), code="")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("::error title=Gate not passed::"), 3)

    def test_all_green_is_green_and_listed(self):
        p, r = self.verdict(needs(lint="success", test="success", security="success"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("- security: success", r.summary.read_text())

    def test_one_failure_fails_it_by_name(self):
        p, _ = self.verdict(needs(lint="success", test="failure", security="success"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Gate not passed::test: failure", p.stdout)

    def test_a_skipped_or_cancelled_gate_is_not_a_pass(self):
        p, _ = self.verdict(needs(lint="skipped", test="cancelled", security="success"))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("::error title=Gate not passed::"), 2)

    def test_the_callers_extra_jobs_count_and_cannot_shadow_ours(self):
        own = needs(lint="failure", test="success", security="success")
        p, r = self.verdict(own, needs(lint="success", e2e="failure"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Gate not passed::lint: failure", p.stdout)
        self.assertIn("::error title=Gate not passed::extra: e2e: failure", p.stdout)
        self.assertIn("- extra: lint: success", r.summary.read_text())

    def test_extra_jobs_that_are_not_needs_json_fail(self):
        p, _ = self.verdict(needs(lint="success"), "e2e")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=extra-jobs::", p.stdout)


class ShardShape(unittest.TestCase):
    """airia-ci#168: `shards` splits `test`; at 1 the jobs, names and required checks are today's."""

    def test_shards_is_an_optional_number_defaulting_to_one(self):
        self.assertEqual(INPUTS["shards"]["type"], "number")
        self.assertEqual(INPUTS["shards"]["default"], 1)
        self.assertFalse(INPUTS["shards"]["required"])

    def test_at_one_shard_the_job_names_are_todays(self):
        # Expressions cannot be evaluated here, so this holds the structure: with the shard suffix expression removed (it is
        # empty unless `inputs.shards > 1`) the test job's name is the old literal, `gate` has no `name:` of its own (so its
        # check is still `ci / gate`), and the only job added is `coverage`, which is skipped at 1.
        suffix = "${{ inputs.shards > 1 && format(' shard {0}/{1}', matrix.shard, inputs.shards) || '' }}"
        name = JOBS["test"]["name"]
        self.assertTrue(name.endswith(suffix), name)
        self.assertEqual(name.removesuffix(suffix), "test ${{ matrix.python || '(.python-version)' }}")
        self.assertNotIn("name", JOBS["gate"])
        self.assertEqual(list(JOBS), ["changes", "lint", "test", "coverage", "security", "gate", "claude-review"])
        self.assertIn("inputs.shards > 1", JOBS["coverage"]["if"])

    def test_the_matrix_is_one_job_per_python_at_one_shard_and_n_at_n(self):
        table = re.search(r"fromJSON\('(.*)'\)\[inputs.shards\]", JOBS["test"]["strategy"]["matrix"]["shard"]).group(1)
        rows = json.loads(table)
        self.assertEqual(rows[1], [0], "one shard: the single index 0, not shown in the name")
        for n in range(2, 9):
            self.assertEqual(rows[n], list(range(1, n + 1)))
        self.assertNotIn("max-parallel", JOBS["test"]["strategy"])
        self.assertNotIn("timeout-minutes", JOBS["test"])

    def test_gate_waits_for_coverage_and_knows_the_shard_count(self):
        self.assertIn("coverage", JOBS["gate"]["needs"])
        self.assertEqual(JOBS["gate"]["steps"][0]["env"]["SHARDS"], "${{ inputs.shards }}")


class Shards(unittest.TestCase):
    STEP = "Pytest at the coverage floor"
    IDS = [f"tests/test_a.py::t{i}" for i in range(7)]

    def setUp(self):
        self.r = Runner(self)
        self.r.tool("pytest", FAKE_PYTEST)
        self.project = self.r.tmp / "project"
        self.project.mkdir()

    def run_step(self, **env):
        base = {"FLOOR": "95", "FAKE_PERCENT": "40", "DURATIONS": "25", "FAKE_COLLECT": "\n".join(self.IDS)}
        return self.r.run(WORKFLOW, self.STEP, cwd=self.project, AIRIA_SHARD=str(SHARD_PY), **{**base, **env})

    def calls(self) -> list[str]:
        f = self.r.bin / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def test_one_shard_calls_pytest_exactly_as_before_and_never_collects(self):
        p = self.run_step(SHARDS="1", SHARD="0", FAKE_PERCENT="96")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (call,) = self.calls()
        self.assertNotIn("--collect-only", call)
        self.assertNotIn("::", call)
        self.assertIn("--cov-fail-under=95", call)

    def test_a_shard_runs_its_files_at_n_4_and_holds_no_partial_floor(self):
        p = self.run_step(SHARDS="3", SHARD="1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        collect, run = self.calls()
        self.assertIn("--collect-only -q --no-cov", collect)
        self.assertTrue(run.startswith("pytest -n 4 "), run)
        self.assertIn("--cov-fail-under=0", run, "a slice cannot reach the floor; the coverage job holds it")
        self.assertTrue(run.endswith(" tests/test_a.py"), run)  # whole files, not node ids
        self.assertNotIn("::", run)

    FILES = {"tests/a.py": 3, "tests/b.py": 2, "tests/c.py": 2, "tests/d.py": 1}

    def test_the_shards_are_disjoint_and_together_the_whole_suite(self):
        listing = "\n".join(f"{f}::t{n}" for f, c in self.FILES.items() for n in range(c))
        seen = []
        for shard in (1, 2, 3):
            self.setUp()
            self.run_step(SHARDS="3", SHARD=str(shard), FAKE_COLLECT=listing)
            seen += [a for a in self.calls()[-1].split() if a.endswith(".py")] if len(self.calls()) > 1 else []
        self.assertEqual(sorted(seen), sorted(self.FILES))

    def test_a_shard_writes_its_coverage_data_where_the_upload_reads_it(self):
        p = self.run_step(SHARDS="2", SHARD="1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.r.tmp / "shard-cov").is_dir())
        up = next(s for s in JOBS["test"]["steps"] if s.get("name") == "Upload this shard's coverage data")
        self.assertEqual(up["with"]["path"], "${{ runner.temp }}/shard-cov/")
        self.assertIs(up["with"]["include-hidden-files"], True, ".coverage.N is a hidden file")

    def test_an_empty_slice_passes_without_running_pytest(self):
        p = self.run_step(SHARDS="4", SHARD="4", FAKE_COLLECT="a.py::t0\nb.py::t1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Shard 4 of 4 has no tests", p.stdout)
        self.assertEqual(len(self.calls()), 1, "collected, and nothing else")

    def test_noise_around_the_ids_is_not_an_id(self):
        noise = "\n".join(["a.py::t0", "", "=== warnings summary ===", "x.py:1: UserWarning: no", "1 test collected in 0.1s"])
        p = self.run_step(SHARDS="2", SHARD="1", FAKE_COLLECT=noise)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(self.calls()[1].endswith(" a.py"))

    ODD = ["t y.py::test_f[hello world]", "t y.py::test_g[a::b]", "u[1].py::test_h[x y::z w]", "u[1].py::plain"]

    def test_files_with_spaces_or_brackets_and_ids_with_colons_are_kept_whole_in_exactly_one_shard(self):
        # airia-ci#169 F1: a space in a parametrized id once dropped the test from every shard.
        listing = "\n".join(self.ODD + ["", f"{len(self.ODD)} tests collected in 0.02s"])
        landed = []
        for shard in (1, 2):
            self.setUp()
            argv = self.r.bin / "argv"
            self.r.tool("pytest", FAKE_PYTEST.replace('echo "pytest $*" >> "$(dirname "$0")/calls"',
                                                      'echo "pytest $*" >> "$(dirname "$0")/calls"; '
                                                      'for a in "$@"; do echo "[$a]" >> "$(dirname "$0")/argv"; done'))
            p = self.run_step(SHARDS="2", SHARD=str(shard), FAKE_COLLECT=listing)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            landed += [a[1:-1] for a in argv.read_text().splitlines() if a.endswith(".py]")]
        self.assertEqual(sorted(landed), ["t y.py", "u[1].py"])  # each file once, as one argument

    def test_the_count_guard_fails_when_ids_read_differ_from_pytests_count(self):
        for listing, expected in (("a.py::t0\nb.py::t1\n3 tests collected in 0.1s", 3),
                                  ("a.py::t0\nb.py::t1\n1 test collected in 0.1s", 1)):
            with self.subTest(expected=expected):
                self.setUp()
                p = self.run_step(SHARDS="2", SHARD="1", FAKE_COLLECT=listing)
                self.assertEqual(p.returncode, 1)
                self.assertIn(f"::error title=Collection count::pytest collected {expected} tests but 2 ids", p.stdout)
                self.assertEqual(len(self.calls()), 1, "no test ran")

    def test_the_count_guard_reads_the_deselected_form_and_passes_when_equal(self):
        p = self.run_step(SHARDS="2", SHARD="1", FAKE_COLLECT="a.py::t0\nb.py::t1\n2/5 tests collected (3 deselected) in 0.1s")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_per_file_counts_from_a_q_in_addopts_are_refused_not_run_as_everything(self):
        p = self.run_step(SHARDS="2", SHARD="1", FAKE_COLLECT="tests/test_a.py: 7")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Collection format::", p.stdout)
        self.assertEqual(len(self.calls()), 1)

    def test_a_collection_error_fails_the_shard(self):
        p = self.run_step(SHARDS="2", SHARD="1", FAKE_COLLECT_EXIT="2")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Collection failed::", p.stdout)

    def test_workers_above_four_or_auto_are_refused_with_shards(self):
        for workers in ("8", "auto"):
            with self.subTest(workers=workers):
                p = self.run_step(SHARDS="2", SHARD="1", WORKERS=workers)
                self.assertEqual(p.returncode, 1)
                self.assertIn("::error title=pytest-workers::", p.stdout)

    def test_a_smaller_worker_count_is_kept(self):
        p = self.run_step(SHARDS="2", SHARD="1", WORKERS="2")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(self.calls()[1].startswith("pytest -n 2 "))

    def test_a_failing_slice_fails_the_shard(self):
        p = self.run_step(SHARDS="2", SHARD="1", FAKE_PYTEST_EXIT="1")
        self.assertEqual(p.returncode, 1)


class ShardsCheck(unittest.TestCase):
    STEP = "Shards check"

    def run_step(self, shards, ratchet="false"):
        return Runner(self).run(WORKFLOW, self.STEP, SHARDS=shards, RATCHET=ratchet)

    def test_one_to_eight_pass(self):
        for n in "12345678":
            self.assertEqual(self.run_step(n).returncode, 0, n)

    def test_anything_else_is_refused_by_name(self):
        for bad in ("0", "9", "2.5", "", "x", "-1"):
            with self.subTest(shards=bad):
                p = self.run_step(bad)
                self.assertEqual(p.returncode, 1)
                self.assertIn("::error title=shards::", p.stdout)

    def test_the_debt_ratchet_cannot_be_sharded(self):
        p = self.run_step("2", "true")
        self.assertEqual(p.returncode, 1)
        self.assertIn("debt-ratchet", p.stdout)
        self.assertEqual(self.run_step("1", "true").returncode, 0)


class CombinedCoverage(unittest.TestCase):
    """The `coverage` job holds the floor on the shards' combined data (a fake `coverage`, the real jq)."""

    STEP = "The combined coverage holds the floor"
    FAKE_COVERAGE = """\
#!/bin/sh
echo "coverage $*" >> "$(dirname "$0")/calls"
[ "$1" = json ] && echo "{\\"totals\\": {\\"percent_covered\\": $FAKE_PERCENT}}" > "$3"
exit 0
"""

    def run_step(self, data: bool, percent: str = "96", floor: str = "95"):
        r = Runner(self)
        r.tool("coverage", self.FAKE_COVERAGE)
        if data:
            (r.tmp / "shard-cov").mkdir()
            (r.tmp / "shard-cov" / ".coverage.1").write_text("x")
        return r, r.run(WORKFLOW, self.STEP, FLOOR=floor, FAKE_PERCENT=percent)

    def test_combined_data_at_or_over_the_floor_passes(self):
        r, p = self.run_step(True, percent="95.0")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((r.bin / "calls").read_text().split()[:2], ["coverage", "combine"])

    def test_combined_data_under_the_floor_fails(self):
        _, p = self.run_step(True, percent="94.9")
        self.assertEqual(p.returncode, 1)

    def test_no_data_from_any_shard_fails_naming_the_fix(self):
        _, p = self.run_step(False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=No coverage data::", p.stdout)

    def test_the_job_downloads_what_the_shards_upload(self):
        up = next(s for s in JOBS["test"]["steps"] if s.get("name") == "Upload this shard's coverage data")
        down = next(s for s in JOBS["coverage"]["steps"] if s.get("uses", "").startswith("actions/download-artifact@"))
        self.assertEqual(up["with"]["name"], "coverage-${{ matrix.python || 'default' }}-${{ matrix.shard }}")
        self.assertEqual(down["with"]["pattern"], "coverage-${{ matrix.python || 'default' }}-*")
        self.assertIs(down["with"]["merge-multiple"], True)
        self.assertEqual(down["with"]["path"], "${{ runner.temp }}/shard-cov")


class ShardedGate(unittest.TestCase):
    """`gate` still reports one result for a sharded run: a red, skipped or missing coverage job fails it."""

    verdict = Gate.verdict
    STEP = Gate.STEP

    def test_a_sharded_run_needs_coverage_to_succeed(self):
        ok = needs(changes="success", lint="success", test="success", coverage="success", security="success")
        self.assertEqual(self.verdict(ok, shards="2")[0].returncode, 0)
        for result in ("skipped", "failure", "cancelled"):
            with self.subTest(coverage=result):
                p, _ = self.verdict(needs(changes="success", lint="success", test="success", coverage=result,
                                          security="success"), shards="2")
                self.assertEqual(p.returncode, 1)
                self.assertIn(f"::error title=Gate not passed::coverage: {result}", p.stdout)

    def test_a_failed_shard_fails_the_gate_through_test(self):
        p, _ = self.verdict(needs(changes="success", lint="success", test="failure", coverage="skipped",
                                  security="success"), shards="2")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Gate not passed::test: failure", p.stdout)

    def test_unsharded_the_coverage_skip_is_normal(self):
        p, _ = self.verdict(needs(changes="success", lint="success", test="success", coverage="skipped",
                                  security="success"), shards="1")
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_docs_only_excuses_the_coverage_skip(self):
        p, _ = self.verdict(needs(changes="success", lint="skipped", test="skipped", coverage="skipped",
                                  security="success"), code="false", shards="2")
        self.assertEqual(p.returncode, 0, p.stdout)


if __name__ == "__main__":
    unittest.main()
