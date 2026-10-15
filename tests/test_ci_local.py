# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""templates/python/ci-local.sh: the gate's lint and test jobs on a laptop.

What these tests hold: it runs the gate's own commands (the ruff, type-check
and pytest lines are the gate's, read from reusable-python-gate.yml, so a
change there fails here until ci-local follows); every check runs even after
one fails, and any failure fails the run; pytest without a coverage figure
fails; `-n auto` is capped at 4 workers unless AIRIA_XDIST_WORKERS says
otherwise; a gate tool missing from the project environment stops it with the
fix.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import re
import unittest

from steps import ROOT, Runner, step

SCRIPT = ROOT / "templates/python/ci-local.sh"
GATE = ROOT / ".github/workflows/reusable-python-gate.yml"

# A fake `uv`: logs each call with the xdist cap it saw; `run pytest` writes the
# JSON report unless FAKE_NO_REPORT; a call whose argv matches FAKE_FAIL fails.
FAKE_UV = """#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
with open(os.environ["FAKE_UV_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "workers": os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS")}) + "\\n")
for a in args:
    if a.startswith("--cov-report=json:") and not os.environ.get("FAKE_NO_REPORT"):
        open(a.split(":", 1)[1], "w").write('{"totals": {"percent_covered": 95}}')
sys.exit(1 if os.environ.get("FAKE_FAIL") and re.search(os.environ["FAKE_FAIL"], " ".join(args)) else 0)
"""


class CiLocal(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        self.r.tool("uv", FAKE_UV)
        self.repo = self.r.tmp / "app"
        (self.repo / ".venv/bin").mkdir(parents=True)
        for tool in ("ruff", "pytest", "pyright", "mypy"):
            self.r.tool(tool, "#!/bin/sh\n")
            (self.repo / ".venv/bin" / tool).symlink_to(self.r.bin / tool)
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'app'\n")
        self.log = self.r.tmp / "uv.jsonl"

    def run_it(self, **env):
        p = self.r.script(SCRIPT, cwd=self.repo, FAKE_UV_LOG=str(self.log), **env)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return p, calls

    def test_it_runs_the_gates_commands_in_order_and_passes(self):
        p, calls = self.run_it()
        self.assertEqual(p.returncode, 0, p.stderr)
        report = calls[-1]["args"][4].split(":", 1)[1]
        self.assertEqual([c["args"] for c in calls], [
            ["sync", "--locked"],
            ["run", "ruff", "check", "."],
            ["run", "ruff", "format", "--check", "."],
            ["run", "pyright", "--pythonpath", f"{self.repo.resolve()}/.venv/bin/python", "."],
            ["run", "pytest", "--durations=25", "--cov-report=term-missing", f"--cov-report=json:{report}",
             "--cov-fail-under=90"],
        ])
        self.assertEqual(calls[-1]["workers"], "4", "-n auto on a laptop: at most 4 workers")
        self.assertIn("lint and test pass as the gate runs them", p.stdout)

    def test_its_commands_are_the_gates_own(self):
        # The gate's lines, with its variable names, are ci-local's after `uv run`.
        ours = SCRIPT.read_text().replace("\\\n  ", "")
        names = {"$FLOOR": "$floor", "$DURATIONS": "$durations"}
        seen = 0
        for name in ("Ruff", "Type check", "Pytest at the coverage floor"):
            for line in step(GATE, name)["run"].splitlines():
                cmd = line.strip().removeprefix("pyright) ").removeprefix("mypy-strict) ").removesuffix(" ;;")
                if re.match(r"(ruff|pyright|mypy|pytest) ", cmd):
                    for theirs, mine in names.items():
                        cmd = cmd.replace(theirs, mine)
                    seen += 1
                    with self.subTest(command=cmd):
                        self.assertIn(f"uv run {cmd.removesuffix(' || status=1')}", ours)
        self.assertEqual(seen, 5, "ruff check, ruff format, pyright, mypy, pytest")

    def test_mypy_import_contracts_paths_and_every_cpu_when_asked(self):
        (self.repo / "pyproject.toml").write_text("[tool.importlinter]\nroot_package = 'app'\n")
        p, calls = self.run_it(AIRIA_TYPE_CHECKER="mypy-strict", AIRIA_PATHS="src tests", AIRIA_XDIST_WORKERS="auto",
                               AIRIA_COVERAGE_FLOOR="95", AIRIA_DURATIONS="0")
        self.assertEqual(p.returncode, 0, p.stderr)
        args = [c["args"] for c in calls]
        self.assertIn(["run", "mypy", "--strict", "src", "tests"], args)
        self.assertIn(["run", "ruff", "check", "src", "tests"], args)
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

    def test_an_unknown_type_checker_is_refused(self):
        p, _ = self.run_it(AIRIA_TYPE_CHECKER="ty")
        self.assertEqual(p.returncode, 2)
        self.assertIn("no type checker ty", p.stderr)


if __name__ == "__main__":
    unittest.main()
