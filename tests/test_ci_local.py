# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""templates/python/ci-local.sh: the gate's lint and test jobs on a laptop.

What these tests hold: it runs the gate's own commands (the ruff, type-check
and pytest lines are the gate's, read from reusable-python-gate.yml, so a
change there fails here until ci-local follows); every check runs even after
one fails, and any failure fails the run; pytest without a coverage figure
fails; `-n auto` is capped at 4 workers unless AIRIA_XDIST_WORKERS says
otherwise; a gate tool missing from the project environment stops it with the
fix. The ruff floor and baseline run through the gate's own toolchain.py,
fetched from main as the script is; `--onboard` fixes, formats and marks the rest `# noqa`
before it records the baseline, and `--write-baseline` only records it.
`--write-debt-baseline` measures as the gate's ratchet does and lowers .airia/debt-baseline.json
through the gate's debt.py, or refuses.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import re
import unittest
from unittest import mock

from steps import ROOT, Runner, step

SCRIPT = ROOT / "templates/python/ci-local.sh"
GATE = ROOT / ".github/workflows/reusable-python-gate.yml"

# A fake `uv`: logs each call with the xdist cap it saw; `run pytest` writes the
# JSON report unless FAKE_NO_REPORT; a call whose argv matches FAKE_FAIL fails.
FAKE_UV = """#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
with open(os.environ["FAKE_UV_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "workers": os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"),
                          "paths": os.environ.get("PATHS")}) + "\\n")
for a in args:
    if a.startswith("--cov-report=json:") and not os.environ.get("FAKE_NO_REPORT"):
        open(a.split(":", 1)[1], "w").write('{"totals": {"percent_covered": 95}}')
sys.exit(1 if os.environ.get("FAKE_FAIL") and re.search(os.environ["FAKE_FAIL"], " ".join(args)) else 0)
"""


FETCHED = (".github/actions/python-toolchain/toolchain.py", "templates/python/pyproject.toml", "pins.toml")
TOOLCHAIN = re.compile(r"^(/.+)/\.github/actions/python-toolchain/toolchain\.py$")


# Generated migrations are skipped, as in the gate (toolchain.py RUFF_SKIP).
SKIP = ["--extend-exclude", "migrations", "--force-exclude"]


class CiLocal(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self, [[f"contents/{f}\\?ref=main ", {"text": (ROOT / f).read_text()}] for f in FETCHED])
        self.r.tool("uv", FAKE_UV)
        self.repo = self.r.tmp / "app"
        (self.repo / ".venv/bin").mkdir(parents=True)
        for tool in ("ruff", "pytest", "pyright", "mypy"):
            self.r.tool(tool, "#!/bin/sh\n")
            (self.repo / ".venv/bin" / tool).symlink_to(self.r.bin / tool)
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'app'\n")
        self.log = self.r.tmp / "uv.jsonl"

    def run_it(self, *args, **env):
        p = self.r.script(SCRIPT, *args, cwd=self.repo, FAKE_UV_LOG=str(self.log), **env)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return p, calls

    def test_it_runs_the_gates_commands_in_order_and_passes(self):
        p, calls = self.run_it()
        self.assertEqual(p.returncode, 0, p.stderr)
        report = calls[-1]["args"][4].split(":", 1)[1]
        harness = TOOLCHAIN.match(calls[3]["args"][2]).group(1)
        self.assertEqual([c["args"] for c in calls], [
            ["sync", "--locked"],
            ["run", "ruff", "check", *SKIP, "."],
            ["run", "ruff", "format", "--check", *SKIP, "."],
            ["run", "python3", f"{harness}/.github/actions/python-toolchain/toolchain.py", "ruff"],
            ["run", "pyright", "--pythonpath", f"{self.repo.resolve()}/.venv/bin/python", "."],
            ["run", "pytest", "--durations=25", "--cov-report=term-missing", f"--cov-report=json:{report}",
             "--cov-fail-under=90"],
        ])
        self.assertEqual(calls[-1]["workers"], "4", "-n auto on a laptop: at most 4 workers")
        self.assertEqual(calls[3]["paths"], ".")
        # The gate's own check, from the ref the script came from, laid out as in airia-ci.
        fetched = [c["args"][1] for c in self.r.calls()]
        self.assertEqual(fetched, [f"repos/AIRIA-Systems-GmbH/airia-ci/contents/{f}?ref=main" for f in FETCHED])
        self.assertEqual(report.rsplit("/", 1)[0], harness, "one scratch directory, removed on exit")
        self.assertIn("lint and test pass as the gate runs them", p.stdout)

    def test_its_commands_are_the_gates_own(self):
        # The gate's lines, with its variable names, are ci-local's after `uv run`.
        ours = SCRIPT.read_text().replace("\\\n  ", "")
        names = {"$FLOOR": "$floor", "$DURATIONS": "$durations"}
        seen = 0
        ratchet_lines = []
        for name in ("Ruff", "Type check", "Pytest at the coverage floor"):
            in_ratchet = None  # the indent of the open `if [ "$RATCHET" = true ]; then`, else None
            for line in step(GATE, name)["run"].splitlines():
                indent = len(line) - len(line.lstrip())
                if in_ratchet is not None:
                    if indent == in_ratchet and line.strip() in ("else", "fi"):
                        in_ratchet = None
                    else:
                        # The ratchet's measuring commands are paired with `--write-debt-baseline`'s in
                        # test_the_debt_writer_measures_with_the_gates_ratchet_commands.
                        ratchet_lines.append(line.strip())
                        continue
                elif line.strip() == 'if [ "$RATCHET" = true ]; then':
                    in_ratchet = indent
                    continue
                cmd = line.strip().removeprefix("pyright) ").removeprefix("mypy-strict) ").removesuffix(" ;;")
                if re.match(r"(ruff|pyright|mypy|pytest) ", cmd):
                    for theirs, mine in names.items():
                        cmd = cmd.replace(theirs, mine)
                    seen += 1
                    with self.subTest(command=cmd):
                        self.assertIn(f"uv run {cmd.removesuffix(' || status=1')}", ours)
        self.assertEqual(seen, 5, "ruff check, ruff format, pyright, mypy, pytest")
        # The skip must not swallow a renamed line: the ratchet's pyright command is there, and is the one skipped.
        self.assertTrue(any(re.match(r"pyright --outputjson ", c) for c in ratchet_lines), ratchet_lines)
        # Likewise the test job's: pytest with -rfE is the ratchet's, and the plain pytest command beside it is checked.
        self.assertTrue(any(re.match(r"pytest .* -rfE ", c) for c in ratchet_lines), ratchet_lines)

    def test_mypy_import_contracts_paths_and_every_cpu_when_asked(self):
        (self.repo / "pyproject.toml").write_text("[tool.importlinter]\nroot_package = 'app'\n")
        # A cap the caller's shell already sets (4 on the maintainer's Mac) is not this test's to inherit.
        with mock.patch.dict(os.environ):
            os.environ.pop("PYTEST_XDIST_AUTO_NUM_WORKERS", None)
            p, calls = self.run_it(AIRIA_TYPE_CHECKER="mypy-strict", AIRIA_PATHS="src tests", AIRIA_XDIST_WORKERS="auto",
                                   AIRIA_COVERAGE_FLOOR="95", AIRIA_DURATIONS="0")
        self.assertEqual(p.returncode, 0, p.stderr)
        args = [c["args"] for c in calls]
        self.assertEqual(calls[3]["paths"], "src tests")
        self.assertIn(["run", "mypy", "--strict", "src", "tests"], args)
        self.assertIn(["run", "ruff", "check", *SKIP, "src", "tests"], args)
        self.assertIn(["run", "lint-imports"], args)
        self.assertIn("--cov-fail-under=95", args[-1])
        self.assertIn("--durations=0", args[-1])
        self.assertIsNone(calls[-1]["workers"], "auto: xdist counts the CPUs, as in CI")

    def test_a_failed_check_still_runs_the_rest_and_fails_the_run(self):
        p, calls = self.run_it(FAKE_FAIL="^run ruff check")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(calls[-1]["args"][1], "pytest")
        self.assertNotIn("pass as the gate runs them", p.stdout)

    def test_no_coverage_report_fails_with_the_fix(self):
        p, _ = self.run_it(FAKE_NO_REPORT="1")
        self.assertEqual(p.returncode, 1)
        self.assertIn('addopts = "--cov=<package>"', p.stderr)

    def test_a_gate_tool_outside_the_environment_stops_it_with_the_fix(self):
        (self.repo / ".venv/bin/pyright").unlink()
        p, calls = self.run_it()
        self.assertEqual(p.returncode, 2)
        self.assertIn("uv add --dev pyright==<pin>", p.stderr)
        self.assertEqual([c["args"] for c in calls], [["sync", "--locked"]])

    def test_onboarding_fixes_formats_marks_the_rest_then_records_the_baseline(self):
        p, calls = self.run_it("--onboard", AIRIA_PATHS="src")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([c["args"] for c in calls[1:5]], [
            ["run", "ruff", "check", "--fix", *SKIP, "src"],
            ["run", "ruff", "format", *SKIP, "src"],  # before the noqa comments, or they could fail format --check
            ["run", "ruff", "check", "--add-noqa", *SKIP, "src"],
            ["run", "ruff", "check", *SKIP, "src"],
        ])
        self.assertEqual(calls[6]["args"][3:], ["ruff", "--write-baseline"])

    def test_write_baseline_only_records_it(self):
        p, calls = self.run_it("--write-baseline")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("--add-noqa", [a for c in calls for a in c["args"]])
        self.assertEqual(calls[3]["args"][3:], ["ruff", "--write-baseline"])

    def test_a_red_floor_fails_the_run(self):
        p, _ = self.run_it(FAKE_FAIL="toolchain.py ruff")
        self.assertEqual(p.returncode, 1)

    def test_an_unknown_option_is_refused_before_anything_runs(self):
        p, calls = self.run_it("--baseline")
        self.assertEqual(p.returncode, 2)
        self.assertIn("unknown option --baseline", p.stderr)
        self.assertEqual(calls, [])

    def test_an_unknown_type_checker_is_refused(self):
        p, _ = self.run_it(AIRIA_TYPE_CHECKER="ty")
        self.assertEqual(p.returncode, 2)
        self.assertIn("no type checker ty", p.stderr)


# A fake `uv` for --write-debt-baseline: pyright and pytest print canned output (and pytest writes the coverage
# report) with chosen exit codes, and `run python3 <script>` runs the real script, which is debt.py as fetched.
WRITER_UV = """#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_UV_LOG"], "a") as log:
    log.write(json.dumps({"args": args}) + "\\n")
if args[:2] == ["run", "pyright"]:
    sys.stdout.write(open(os.environ["FAKE_PYRIGHT_OUT"]).read())
    sys.exit(int(os.environ.get("FAKE_PYRIGHT_EXIT", "1")))
if args[:2] == ["run", "pytest"]:
    sys.stdout.write(open(os.environ["FAKE_PYTEST_OUT"]).read())
    for a in args:
        if a.startswith("--cov-report=json:"):
            open(a.split(":", 1)[1], "w").write('{"totals": {"percent_covered": %s}}' % os.environ["FAKE_PERCENT"])
    sys.exit(int(os.environ.get("FAKE_PYTEST_EXIT", "1")))
if args[:2] == ["run", "python3"]:
    sys.exit(subprocess.run([sys.executable, *args[2:]]).returncode)
sys.exit(0)
"""

DEBT = ".github/actions/python-toolchain/debt.py"
DATES = {"version": 1, "set": "2026-11-06", "expires": "2027-02-04"}
SUMMARY = ("=========================== short test summary info ============================\n"
           "FAILED tests/test_a.py::test_x - boom\n=================== 1 failed, 3 passed in 0.12s ===================\n")


class CiLocalWriteDebtBaseline(unittest.TestCase):
    """`--write-debt-baseline`: lower .airia/debt-baseline.json to this machine's measurement, or refuse."""

    def setUp(self):
        self.r = Runner(self, [[f"contents/{DEBT}\\?ref=main ", {"text": (ROOT / DEBT).read_text()}]])
        self.r.tool("uv", WRITER_UV)
        self.repo = self.r.tmp / "app"
        (self.repo / ".venv/bin").mkdir(parents=True)
        (self.repo / ".airia").mkdir()
        for tool in ("ruff", "pytest", "pyright", "mypy"):
            self.r.tool(tool, "#!/bin/sh\n")
            (self.repo / ".venv/bin" / tool).symlink_to(self.r.bin / tool)
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'app'\n")
        self.file = self.repo / ".airia/debt-baseline.json"
        self.file.write_text(json.dumps({**DATES, "tests": {"failing": ["tests/test_a.py::test_x", "tests/test_b.py::test_old"]},
                                         "types": {"pyright": {"reportArgumentType": 3, "reportCallIssue": 2}},
                                         "coverage": {"floor": 41}}))
        self.log = self.r.tmp / "uv.jsonl"
        (self.r.tmp / "pytest.out").write_text(SUMMARY)
        self.pyright(["reportArgumentType", "reportArgumentType", "reportCallIssue", "reportCallIssue"])

    def pyright(self, rules):
        diagnostics = [{"severity": "error", "rule": r, "file": "a.py", "message": "m", "range": {"start": {"line": 0}}}
                       for r in rules]
        (self.r.tmp / "pyright.out").write_text(json.dumps({"generalDiagnostics": diagnostics}))

    def run_it(self, **env):
        p = self.r.script(SCRIPT, "--write-debt-baseline", cwd=self.repo, FAKE_UV_LOG=str(self.log),
                          FAKE_PYRIGHT_OUT=str(self.r.tmp / "pyright.out"), FAKE_PYTEST_OUT=str(self.r.tmp / "pytest.out"),
                          FAKE_PERCENT=env.pop("FAKE_PERCENT", "57.9"), **env)
        calls = [json.loads(line)["args"] for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return p, calls

    def test_it_measures_as_the_gate_does_and_lowers_the_file_keeping_the_dates(self):
        p, calls = self.run_it()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(calls[0], ["sync", "--locked"])
        self.assertEqual([c[1] for c in calls[1:]], ["pyright", "pytest", "python3"], "no ruff, no format, no import-linter")
        self.assertEqual(calls[1][2], "--outputjson")
        self.assertIn("-rfE", calls[2])
        self.assertIn("--cov-fail-under=0", calls[2], "a measurement holds no floor; the writer judges the figure")
        write = calls[3]
        self.assertEqual((write[3], write[4], write[6], write[7], write[9]), ("write", "--pyright", "--tests", "1", "--coverage"))
        self.assertEqual(write[5].rsplit("/", 1)[1], "pyright.json")
        written = json.loads(self.file.read_text())
        self.assertEqual(written["tests"], {"failing": ["tests/test_a.py::test_x"]})
        self.assertEqual(written["types"], {"pyright": {"reportArgumentType": 2, "reportCallIssue": 2}})
        self.assertEqual(written["coverage"], {"floor": 57})
        self.assertEqual((written["set"], written["expires"]), (DATES["set"], DATES["expires"]))
        self.assertEqual([c["args"][1] for c in self.r.calls()], [f"repos/AIRIA-Systems-GmbH/airia-ci/contents/{DEBT}?ref=main"],
                         "only debt.py is fetched, from the ref the script came from")

    def test_a_worse_measurement_is_refused_and_the_file_is_untouched(self):
        self.pyright(["reportArgumentType"] * 4)
        before = self.file.read_bytes()
        p, _ = self.run_it()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("pyright reportArgumentType rose from 3 to 4", p.stdout)
        self.assertEqual(self.file.read_bytes(), before)

    def test_a_coverage_below_the_floor_is_refused(self):
        before = self.file.read_bytes()
        p, _ = self.run_it(FAKE_PERCENT="30")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual(self.file.read_bytes(), before)

    def test_mypy_strict_skips_pyright_and_leaves_types_alone(self):
        p, calls = self.run_it(AIRIA_TYPE_CHECKER="mypy-strict")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([c[1] for c in calls[1:]], ["pytest", "python3"])
        self.assertNotIn("--pyright", calls[-1])
        self.assertEqual(json.loads(self.file.read_text())["types"], {"pyright": {"reportArgumentType": 3, "reportCallIssue": 2}})

    def test_pyright_that_cannot_run_stops_before_pytest(self):
        p, calls = self.run_it(FAKE_PYRIGHT_EXIT="2")
        self.assertEqual(p.returncode, 1)
        self.assertIn("pyright exited 2 without a report", p.stderr)
        self.assertNotIn("pytest", [c[1] for c in calls if len(c) > 1])

    def test_a_pytest_that_did_not_finish_is_not_a_measurement(self):
        before = self.file.read_bytes()
        p, _ = self.run_it(FAKE_PYTEST_EXIT="2")
        self.assertEqual(p.returncode, 1)
        self.assertIn("pytest exited 2", p.stdout)
        self.assertEqual(self.file.read_bytes(), before)

    def test_the_debt_writer_measures_with_the_gates_ratchet_commands(self):
        """The ratchet's pyright and pytest lines in the gate are the writer's, `uv run` in front, so the figures
        it writes are the ones the gate will see. The one difference is deliberate: no coverage floor (see below)."""
        ours = SCRIPT.read_text().replace("\\\n  ", "")
        lines = [line.strip() for name in ("Type check", "Pytest at the coverage floor")
                 for line in step(GATE, name)["run"].splitlines()]
        pyright = [line for line in lines if line.startswith("pyright --outputjson ")]
        pytest = [line for line in lines if re.match(r"pytest .* -rfE ", line)]
        self.assertEqual((len(pyright), len(pytest)), (1, 1), lines)
        self.assertIn("uv run " + pyright[0].split(" > ")[0], ours)
        cmd = pytest[0].split(" 2>&1")[0].replace("$DURATIONS", "$durations").replace('"--cov-fail-under=$FLOOR"',
                                                                                         '"--cov-fail-under=0"')
        self.assertIn("uv run " + cmd, ours)


if __name__ == "__main__":
    unittest.main()
