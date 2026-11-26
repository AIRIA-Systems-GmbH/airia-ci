# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The gate's named shards, the debt ratchet across shards, peak memory and [tool.coverage.paths] (airia-ci#181).

What these tests hold: `rest` ignores exactly the groups' paths, and a group that could leave a test in no shard is
refused; a shard never runs above 4 workers; a shard under the ratchet judges nothing, and the combine job judges the
union once, red when any expected shard left no result or one did not finish; measuring memory never changes pytest's
exit status; shard data from another workspace path combines through the caller's own [tool.coverage.paths].

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steps import ROOT, Runner, run_block

TOOLCHAIN = ROOT / ".github/actions/python-toolchain"
WORKFLOW = ROOT / ".github/workflows/reusable-python-gate.yml"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLCHAIN / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shard = load("shard")
debt = load("debt")
peak_rss = load("peak_rss")


@contextlib.contextmanager
def checkout(*paths: str):
    """A scratch checkout holding `paths` (a name ending in `/` is a directory), as the working directory."""
    with tempfile.TemporaryDirectory() as tmp:
        for path in paths:
            f = Path(tmp) / path
            if path.endswith("/"):
                f.mkdir(parents=True, exist_ok=True)
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("")
        with contextlib.chdir(tmp):
            yield Path(tmp)


def captured(fn, *args, **env) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, env):
        code = fn(*args)
    return code, out.getvalue()


class Groups(unittest.TestCase):
    TREE = ("tests/engine/", "tests/api/", "tests/cli/", "tests/test_top.py", "my tests/a/")
    GROUPS = "engine: tests/engine\napi: tests/api tests/cli\n"

    def plan(self, n: int, groups: str = GROUPS, workers: str = "", default: str = ""):
        with checkout(*self.TREE):
            return shard.group_plan(groups, workers, default, n)

    def test_each_group_runs_its_paths_in_order_then_rest_ignores_all_of_them(self):
        self.assertEqual(self.plan(1), ("engine", 4, ["tests/engine"]))
        self.assertEqual(self.plan(2), ("api", 4, ["tests/api", "tests/cli"]))
        # --ignore, not a list of what is left: a directory added tomorrow is in `rest` with nobody editing anything.
        self.assertEqual(self.plan(3), ("rest", 4, ["--ignore=tests/engine", "--ignore=tests/api", "--ignore=tests/cli"]))

    def test_a_quoted_path_with_a_space_stays_one_argument(self):
        self.assertEqual(self.plan(1, 'spaced: "my tests/a"')[2], ["my tests/a"])

    def test_workers_come_from_the_shards_line_then_pytest_workers_then_four(self):
        workers = "engine: 3\nrest: 1"
        self.assertEqual([self.plan(n, workers=workers, default="2")[1] for n in (1, 2, 3)], [3, 2, 1])
        self.assertEqual([self.plan(n, workers=workers)[1] for n in (1, 2, 3)], [3, 4, 1])

    def test_workers_above_four_or_not_a_count_are_refused(self):
        for value in ("5", "0", "auto", "10", "-1", "2.5"):
            with self.subTest(workers=value), self.assertRaisesRegex(shard.Refused, "from 1 to 4"):
                self.plan(1, workers=f"engine: {value}")

    def test_workers_for_a_shard_that_does_not_exist_are_refused(self):
        with self.assertRaisesRegex(shard.Refused, "no shard is called 'engin'; the shards are engine, api, rest"):
            self.plan(1, workers="engin: 2")

    def test_a_group_that_could_leave_a_test_in_no_shard_is_refused(self):
        # `rest` ignores the whole path; a filtered group would run only part of it.
        for value in ("tests/engine -m slow", "tests/engine -k fast", "--ignore=tests/api",
                      "tests/test_top.py::test_one"):
            with self.subTest(value=value), self.assertRaisesRegex(shard.Refused, "is not a path"):
                self.plan(1, f"g: {value}")

    def test_bad_lines_are_refused_by_what_is_wrong(self):
        for groups, why in (("tests/engine", "each line is `name: value`"), ("g:", "each line is"),
                            ("a b: tests/api", "each line is"), ("rest: tests/api", "automatic shard"),
                            ("g: tests/api\ng: tests/cli", "'g' is named twice"),
                            ("g: tests/apii", "no file or directory 'tests/apii'"),
                            ('g: "tests/api', "No closing quotation"),
                            ("a: tests\nb: tests/engine", "overlaps a path of a"),
                            ("a: tests/engine\nb: tests", "overlaps a path of a"),
                            ("a: tests/api\nb: tests/api/", "overlaps a path of a")):
            with self.subTest(groups=groups), self.assertRaisesRegex(shard.Refused, why):
                self.plan(1, groups)

    def test_a_shard_outside_the_groups_is_refused(self):
        for n in (0, 4):
            with self.subTest(shard=n), self.assertRaisesRegex(shard.Refused, "outside 1..3"):
                self.plan(n)

    def test_the_command_prints_name_workers_then_one_argument_per_line(self):
        with checkout(*self.TREE):
            code, out = captured(shard.main, ["group", "2"], SHARD_GROUPS=self.GROUPS, SHARD_WORKERS="api: 2",
                                 WORKERS="")
            self.assertEqual((code, out), (0, "api\n2\ntests/api\ntests/cli\n"))
            code, out = captured(shard.main, ["group", "x"], SHARD_GROUPS=self.GROUPS)
            self.assertEqual(code, 1)
            self.assertTrue(out.startswith("::error title=Shard groups::"), out)


# A pytest that prints $FAKE_SUMMARY where a real `-rfE` run prints its summary block, and exits $FAKE_PYTEST_EXIT.
FAKE_PYTEST = """\
#!/bin/sh
echo "pytest $*" >> "$(dirname "$0")/calls"
for a in "$@"; do
  case "$a" in --cov-report=json:*) echo '{"totals": {"percent_covered": 40}}' > "${a#--cov-report=json:}";; esac
done
if [ -n "$FAKE_SUMMARY" ]; then
  echo "=========================== short test summary info ============================"
  printf '%s\\n' "$FAKE_SUMMARY"
  echo "============================== 1 failed in 0.1s ==============================="
fi
exit "${FAKE_PYTEST_EXIT:-0}"
"""


class PytestStep(unittest.TestCase):
    """The shipped pytest step in a shard, with the real shard.py, debt.py and peak_rss.py."""

    STEP = "Pytest at the coverage floor"

    def setUp(self):
        self.r = Runner(self)
        self.r.tool("pytest", FAKE_PYTEST)
        self.project = self.r.tmp / "project"
        for d in ("tests/engine", "tests/api"):
            (self.project / d).mkdir(parents=True)

    def run_step(self, shard_no: int, **env):
        base = {"FLOOR": "95", "DURATIONS": "25", "SHARDS": "3", "SHARD": str(shard_no), "SCOPE": "all",
                "SHARD_GROUPS": "engine: tests/engine\napi: tests/api\n", "SHARD_WORKERS": "", "WORKERS": "",
                "RATCHET": "false"}
        return self.r.run(WORKFLOW, self.STEP, cwd=self.project, AIRIA_SHARD=str(TOOLCHAIN / "shard.py"),
                          AIRIA_DEBT=str(TOOLCHAIN / "debt.py"), AIRIA_PEAK_RSS=str(TOOLCHAIN / "peak_rss.py"),
                          **{**base, **env})

    def calls(self) -> list[str]:
        f = self.r.bin / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def result(self, shard_no: int) -> dict:
        return json.loads((self.r.tmp / "shard-cov" / f"debt-shard.{shard_no}.json").read_text())

    def test_a_group_shard_runs_its_paths_at_no_partial_floor_and_never_collects(self):
        p = self.run_step(2, SHARD_WORKERS="api: 3")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (call,) = self.calls()
        self.assertTrue(call.startswith("pytest -n 3 --durations=25 "), call)
        self.assertTrue(call.endswith(" tests/api"), call)
        self.assertIn("--cov-fail-under=0", call)
        self.assertIn("Shard 2 of 3 (api) runs at -n 3: tests/api", p.stdout)

    def test_the_rest_shard_runs_the_suite_without_the_groups_paths(self):
        p = self.run_step(3)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (call,) = self.calls()
        self.assertTrue(call.startswith("pytest -n 4 "), call)
        self.assertTrue(call.endswith(" --ignore=tests/engine --ignore=tests/api"), call)

    def test_rest_with_nothing_left_passes_but_a_group_with_nothing_fails(self):
        for ratchet in ("false", "true"):
            with self.subTest(ratchet=ratchet):
                p = self.run_step(3, FAKE_PYTEST_EXIT="5", RATCHET=ratchet)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn("rest has no tests", p.stdout)
                if ratchet == "true":
                    self.assertEqual(self.result(3), {"status": 0, "failing": []}, "an empty rest leaves its result")
                self.assertNotEqual(self.run_step(1, FAKE_PYTEST_EXIT="5", RATCHET=ratchet).returncode, 0)

    def test_a_bad_group_fails_the_shard_before_pytest(self):
        p = self.run_step(1, SHARD_GROUPS="engine: tests/engine -m slow")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Shard groups::", p.stdout)
        self.assertEqual(self.calls(), [])

    def test_pytests_exit_status_survives_the_memory_wrapper(self):
        for status in (1, 2, 3):
            with self.subTest(status=status):
                self.assertEqual(self.run_step(1, FAKE_PYTEST_EXIT=str(status)).returncode, status)

    def test_a_shard_job_adds_its_peak_memory_to_the_summary_even_when_pytest_fails(self):
        self.run_step(1, FAKE_PYTEST_EXIT="1", AIRIA_PROC=str(self.r.tmp / "no-proc"))
        self.assertIn("Shard 1/3 peak resident memory of the pytest process tree", self.r.summary.read_text())
        self.assertIn("not sampled", self.r.summary.read_text())

    def test_an_unsharded_job_is_not_wrapped(self):
        p = self.run_step(0, SHARDS="1", SHARD_GROUPS="", FLOOR="30")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(self.r.summary.exists())
        self.assertFalse((self.r.tmp / "shard-cov").exists())

    def test_data_left_by_an_earlier_job_is_not_uploaded_again(self):
        (self.r.tmp / "shard-cov").mkdir()
        (self.r.tmp / "shard-cov" / "debt-shard.2.json").write_text('{"status": 0, "failing": []}')
        self.run_step(1)
        self.assertEqual(list((self.r.tmp / "shard-cov").iterdir()), [])

    def test_under_the_ratchet_a_shard_records_its_ids_and_judges_nothing(self):
        # No baseline file exists here: judging in the shard would fail on that alone.
        p = self.run_step(1, RATCHET="true", FAKE_PYTEST_EXIT="1",
                          FAKE_SUMMARY="FAILED tests/engine/test_a.py::t[x - y] - assert 0")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.result(1), {"status": 1, "failing": ["tests/engine/test_a.py::t[x - y]"]})
        (call,) = self.calls()
        self.assertIn(" -rfE ", call)
        self.assertTrue(call.endswith(" tests/engine"), "the slice, not the whole suite: " + call)

    def test_under_the_ratchet_a_run_that_did_not_finish_fails_the_shard(self):
        for status in ("1", "2", "4", "139"):  # exit 1 here has no FAILED line
            with self.subTest(status=status):
                p = self.run_step(1, RATCHET="true", FAKE_PYTEST_EXIT=status)
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn("::error title=Pytest::", p.stdout)
                self.assertFalse((self.r.tmp / "shard-cov" / "debt-shard.1.json").exists())

    def test_under_the_ratchet_an_empty_by_file_shard_still_leaves_a_result(self):
        self.r.tool("pytest", '#!/bin/sh\necho "a.py::t0"\n')
        p = self.run_step(2, RATCHET="true", SHARD_GROUPS="")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("has no tests", p.stdout)
        self.assertEqual(self.result(2), {"status": 0, "failing": []})

    def test_a_records_run_with_groups_runs_in_shard_1_only(self):
        p = self.run_step(2, SCOPE="records", RECORDS_TESTS="-m records")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.calls(), [])
        p = self.run_step(1, SCOPE="records", RECORDS_TESTS="-m records")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(self.calls()[0].endswith(" -m records"), self.calls())


BASELINE = {"version": 1, "set": "2000-01-01", "expires": "2000-03-01",
            "tests": {"failing": ["a.py::known", "b.py::also_known"]}}
IN_DATE = "2000-01-02"


class RatchetAcrossShards(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.baseline = self.tmp / "baseline.json"
        self.baseline.write_text(json.dumps(BASELINE))

    def put(self, n: int, *failing: str, raw: str | None = None):
        text = raw if raw is not None else json.dumps({"status": int(bool(failing)), "failing": list(failing)})
        (self.tmp / f"debt-shard.{n}.json").write_text(text)

    def judge(self, shards: int, today: str = IN_DATE, baseline: Path | None = None) -> tuple[int, str]:
        argv = ["--file", str(baseline or self.baseline), "--today", today, "tests-shards", str(shards), str(self.tmp)]
        return captured(debt.main, argv, GITHUB_STEP_SUMMARY="")

    def test_baselined_failures_spread_over_shards_pass_and_none_reads_as_fixed(self):
        self.put(1, "a.py::known")
        self.put(2, "b.py::also_known")
        self.put(3)
        code, out = self.judge(3)
        self.assertEqual(code, 0, out)
        self.assertIn("3 shard results: highest pytest exit status 1, 2 failing test id(s)", out)
        self.assertNotIn("did not fail", out)

    def test_a_new_failure_in_any_one_shard_fails_it_by_name(self):
        self.put(1, "a.py::known")
        self.put(2, "b.py::also_known", "c.py::new")
        code, out = self.judge(2)
        self.assertEqual(code, 1)
        self.assertIn("::error title=Test outside the debt baseline::c.py::new", out)

    def test_a_baselined_test_no_shard_failed_is_noticed_once(self):
        self.put(1, "a.py::known")
        self.put(2)
        code, out = self.judge(2)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("::notice title=Debt baseline can tighten::"), 1)
        self.assertIn("b.py::also_known", out)

    def test_an_expected_shard_with_no_result_fails_even_when_the_rest_are_clean(self):
        self.put(1)
        self.put(3)
        code, out = self.judge(3)
        self.assertEqual(code, 1)
        self.assertIn("::error title=Shard result::debt-shard.2.json: this shard left no result", out)

    def test_one_shards_ids_cannot_hide_a_result_that_did_not_finish(self):
        self.put(1, "a.py::known")
        for raw in ('{"status": 1, "failing": []}', '{"status": 2, "failing": []}',
                    '{"status": 0, "failing": ["a.py::known"]}', '{"status": "1", "failing": ["x"]}',
                    '{"status": 1, "failing": "x"}', '{"status": 0}', "[]", "not json"):
            with self.subTest(result=raw):
                self.put(2, raw=raw)
                code, out = self.judge(2)
                self.assertEqual(code, 1, out)
                self.assertIn("::error title=Shard result::debt-shard.2.json", out)

    def test_no_shards_at_all_is_not_a_pass(self):
        self.assertEqual(self.judge(0)[0], 1)

    def test_past_expiry_the_standard_applies_to_the_union(self):
        self.put(1, "a.py::known")
        self.assertEqual(self.judge(1, today="2000-06-01")[0], 1)

    def test_an_unusable_baseline_fails(self):
        self.put(1)
        code, out = self.judge(1, baseline=self.tmp / "missing.json")
        self.assertEqual(code, 1)
        self.assertIn("::error title=Debt baseline::", out)

    def test_record_writes_only_a_finished_run(self):
        output = self.tmp / "pytest.out"
        output.write_text("== short test summary info ==\nFAILED a.py::t - boom\nERROR b.py::u\n== 2 failed ==\n")
        out = self.tmp / "r.json"
        self.assertEqual(captured(debt.main, ["record", "--out", str(out), "1", str(output)])[0], 0)
        self.assertEqual(json.loads(out.read_text()), {"status": 1, "failing": ["a.py::t", "b.py::u"]})
        out.unlink()
        self.assertEqual(captured(debt.main, ["record", "--out", str(out), "3", str(output)])[0], 1)
        self.assertFalse(out.exists())
        code, said = captured(debt.main, ["record", "--out", str(out), "0", str(self.tmp / "no-output")])
        self.assertEqual(code, 1)
        self.assertIn("cannot record this shard's result", said)


class CombineStepWithTheRatchet(unittest.TestCase):
    STEP = "The shards' failing tests are within the debt baseline"

    def run_step(self, ratchet: str, results: dict[int, list[str]]):
        r = Runner(self)
        (r.tmp / ".airia").mkdir()
        today = {"set": "2000-01-01", "expires": "2000-03-01"}
        (r.tmp / ".airia/debt-baseline.json").write_text(json.dumps({**BASELINE, **today}))
        (r.tmp / "shard-cov").mkdir()
        for n, failing in results.items():
            (r.tmp / "shard-cov" / f"debt-shard.{n}.json").write_text(
                json.dumps({"status": int(bool(failing)), "failing": failing}))
        return r.run(WORKFLOW, self.STEP, RATCHET=ratchet, SHARDS="2", AIRIA_DEBT=str(TOOLCHAIN / "debt.py"))

    def test_without_the_ratchet_the_step_says_so_and_reads_nothing(self):
        p = self.run_step("false", {})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("No debt baseline", p.stdout)

    def test_with_the_ratchet_it_judges_every_expected_shard(self):
        # The file's dates are long past, so the standard applies: any failing id is red, none is green.
        self.assertEqual(self.run_step("true", {1: [], 2: []}).returncode, 0)
        p = self.run_step("true", {1: ["a.py::known"], 2: []})
        self.assertEqual(p.returncode, 1)
        self.assertIn("a.py::known fails", p.stdout)
        p = self.run_step("true", {1: []})
        self.assertEqual(p.returncode, 1)
        self.assertIn("debt-shard.2.json: this shard left no result", p.stdout)

    def test_the_combine_job_clears_old_data_before_it_downloads(self):
        r = Runner(self)
        (r.tmp / "shard-cov").mkdir()
        (r.tmp / "shard-cov" / "debt-shard.1.json").write_text("{}")
        p = r.run(WORKFLOW, "No shard data from an earlier job")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((r.tmp / "shard-cov").exists())


def fake_proc(root: Path, table: dict[int, tuple[int, int | None]]) -> Path:
    """A /proc with `{pid: (parent, VmRSS KiB or None for none)}`."""
    for pid, (parent, kib) in table.items():
        (root / str(pid)).mkdir(parents=True, exist_ok=True)
        rss = f"VmRSS:\t  {kib} kB\n" if kib is not None else ""
        (root / str(pid) / "status").write_text(f"Name:\tx\nPPid:\t{parent}\n{rss}")
    return root


class PeakMemory(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.summary = self.tmp / "summary"
        self.proc = self.tmp / "proc"

    def test_the_tree_is_the_command_and_every_descendant_and_nothing_else(self):
        fake_proc(self.proc, {10: (1, 100), 11: (10, 200), 12: (11, 300), 13: (10, None), 20: (1, 5000)})
        (self.proc / "self").mkdir()
        (self.proc / "99").mkdir()  # ended while it was read: no status
        self.assertEqual(peak_rss.tree_kib(10, self.proc), 600)

    def test_a_parent_cycle_ends(self):
        fake_proc(self.proc, {10: (11, 1), 11: (10, 2)})
        self.assertEqual(peak_rss.tree_kib(10, self.proc), 3)

    def run_main(self, argv: list[str], proc: Path | None = None, summary: Path | None = None) -> tuple[int, str]:
        with mock.patch.object(peak_rss, "INTERVAL", 0.02):
            return captured(peak_rss.main, argv, AIRIA_PROC=str(proc or self.proc), SHARD="2", SHARDS="5",
                            GITHUB_STEP_SUMMARY=str(summary or self.summary))

    def test_the_highest_sample_is_reported(self):
        # The child writes its own entry, grows, then shrinks: the peak is what is printed.
        script = ('import os, pathlib, time\n'
                  f'd = pathlib.Path({str(self.proc)!r}) / str(os.getpid()); d.mkdir(parents=True)\n'
                  'for kib in (1024, 4096, 2048):\n'
                  '    (d / "new").write_text(f"PPid:\\t1\\nVmRSS:\\t{kib} kB\\n"); os.replace(d / "new", d / "status")\n'
                  '    time.sleep(0.4)\n')
        self.proc.mkdir()
        code, out = self.run_main([sys.executable, "-c", script])
        self.assertEqual(code, 0, out)
        self.assertTrue(self.summary.read_text().startswith("Shard 2/5 peak resident memory"))
        self.assertIn(": 4 MiB", self.summary.read_text())

    def test_the_hosts_own_proc_is_read_where_there_is_one(self):
        # No skip: on Linux (the runners and the coverage image) the real /proc must give a figure, elsewhere none.
        code, out = self.run_main([sys.executable, "-c", "x = bytearray(30_000_000); import time; time.sleep(0.5)"],
                                  proc=Path("/proc"))
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r": \d+ MiB$" if Path("/proc/self/status").exists() else r": not sampled$")

    def test_without_proc_the_status_passes_through_and_the_figure_says_so(self):
        code, out = self.run_main([sys.executable, "-c", "raise SystemExit(3)"], proc=self.tmp / "absent")
        self.assertEqual(code, 3)
        self.assertIn("not sampled", out)

    def test_a_summary_that_cannot_be_written_never_changes_the_status(self):
        code, out = self.run_main([sys.executable, "-c", "pass"], summary=self.tmp)  # a directory
        self.assertEqual(code, 0)
        self.assertIn("::warning title=Peak memory::", out)

    def test_a_command_killed_by_a_signal_exits_as_a_shell_reports_it(self):
        code, _ = self.run_main([sys.executable, "-c", "import os; os.kill(os.getpid(), 9)"])
        self.assertEqual(code, 137)

    def test_a_command_that_does_not_exist_is_127(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_main(["airia-no-such-command"])[0], 127)


@unittest.skipUnless(importlib.util.find_spec("coverage"), "needs coverage.py (the pinned coverage image has it)")
class CoveragePaths(unittest.TestCase):
    """The real coverage.py through the shipped combine step: data written under other workspaces, no relative_files."""

    STEP = "The combined coverage holds the floor"
    PYPROJECT = '[tool.coverage.run]\nsource = ["src"]\n'
    PATHS = '[tool.coverage.paths]\nsource = ["src/", "*/src/"]\n'

    def combine(self, pyproject: str):
        from coverage import CoverageData

        r = Runner(self)
        project = r.tmp / "project"
        (project / "src").mkdir(parents=True)
        (project / "src/mod.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
        (project / "pyproject.toml").write_text(pyproject)
        (r.tmp / "shard-cov").mkdir()
        # Half the lines in each shard, each under its own runner's workspace: only the mapped union reaches 100 %.
        for n, lines in ((1, [1, 2]), (2, [3, 4])):
            data = CoverageData(basename=str(r.tmp / "shard-cov" / f".coverage.{n}"))
            data.add_lines({f"/runners/{n}/_work/repo/repo/src/mod.py": lines})
            data.write()
        r.tool("coverage", f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m coverage "$@"\n')
        # This suite's own coverage settings must not reach the child, which has to read the caller's pyproject.toml.
        env = {k: v for k, v in r.env(FLOOR="100").items() if not k.startswith(("COVERAGE_", "COV_CORE_"))}
        return run_block(WORKFLOW, self.STEP, env, project)

    def test_shard_data_from_another_workspace_combines_through_paths(self):
        p = self.combine(self.PYPROJECT + self.PATHS)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertRegex(p.stdout, r"src/mod\.py\s+4\s+0\s+100%")

    def test_without_paths_the_same_data_does_not_pass(self):
        # The control: it is [tool.coverage.paths] that maps the data, not something the step does by itself.
        self.assertNotEqual(self.combine(self.PYPROJECT).returncode, 0)


if __name__ == "__main__":
    unittest.main()
