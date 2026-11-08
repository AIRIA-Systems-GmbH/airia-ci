# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""debt.py check: the debt baseline is trusted only when it is well formed and in date.

What these tests hold (design/gate-debt-ratchet.md, sections 3 and 4): with the
ratchet on, a baseline that is missing, unreadable or malformed fails loud with
an `::error`, never a silent fallback to the standard or a silent pass; the
horizon is capped at 90 days so the first date cannot be set far away; past
`expires` the gate is red and names the date, and the last 14 days warn.
`debt.py compare` (amendment A, section 3) holds the head's file to the base
branch's copy: it may only tighten.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from steps import ROOT, Runner
from test_python_gate import FAKE_PYTEST

SCRIPT = ROOT / ".github/actions/python-toolchain/debt.py"
spec = importlib.util.spec_from_file_location("debt", SCRIPT)
debt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(debt)

SET = datetime.date(2026, 11, 6)
TODAY = "2026-11-07"
GOOD = {
    "version": 1,
    "set": "2026-11-06",
    "expires": "2027-02-04",
    "tests": {"failing": ["tests/test_a.py::test_x", "tests/test_b.py::TestC::test_y[1]"]},
    "types": {"pyright": {"reportAttributeAccessIssue": 120, "reportArgumentType": 31}},
    "coverage": {"floor": 41},
}


class Check(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "debt-baseline.json"

    def run_check(self, content=GOOD, today=TODAY):
        """(exit code, output) of `debt.py check` on `content`: a str is written as it is, None writes no file."""
        if isinstance(content, str):
            self.path.write_text(content)
        elif content is not None:
            self.path.write_text(json.dumps(content))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = debt.main(["--file", str(self.path), "--today", today, "check"])
        return code, out.getvalue()

    def fails(self, content, *needles):
        code, out = self.run_check(content)
        self.assertEqual(code, 1, out)
        self.assertIn("::error", out)
        for n in needles:
            self.assertIn(n, out)

    def with_(self, **changes):
        return {**GOOD, **changes}

    def test_a_valid_file_passes_silently(self):
        """The happy path must not nag: a consumer inside its window sees no annotation."""
        self.assertEqual(self.run_check(), (0, ""))

    def test_a_file_with_only_the_required_keys_passes(self):
        """Every section is optional: an absent one means the standard for that gate."""
        self.assertEqual(self.run_check({"version": 1, "set": "2026-11-06", "expires": "2027-02-04"}), (0, ""))

    def test_a_missing_file_fails_loud(self):
        """With the ratchet on, a deleted file must not quietly mean 'no debt allowed' or 'no check'."""
        code, out = self.run_check(None)
        self.assertEqual(code, 1)
        self.assertIn("::error", out)
        self.assertIn("is missing", out)

    def test_invalid_json_fails_loud(self):
        """A half-edited file must be reported as such, not parsed leniently."""
        self.fails('{"version": 1,', "not valid JSON")

    def test_a_top_level_that_is_not_an_object_fails(self):
        """`[]` is valid JSON; it must not reach the key checks and crash with a traceback."""
        self.fails([], "top level must be an object")

    def test_an_unknown_key_fails(self):
        """A typo such as `expire` must not silently leave a section unenforced."""
        self.fails(self.with_(expire="2027-01-01"), "unknown key `expire`")

    def test_an_unknown_key_in_a_section_fails(self):
        """The same typo guard holds inside the sections."""
        self.fails(self.with_(coverage={"floor": 41, "flor": 3}), "unknown key `coverage.flor`")
        self.fails(self.with_(tests={"failing": [], "x": 1}), "unknown key `tests.x`")
        self.fails(self.with_(types={"mypy": {}}), "unknown key `types.mypy`")

    def test_wrong_types_fail(self):
        """A count given as a string would compare wrongly later; refuse it at load."""
        cases = [
            (self.with_(tests=[]), "`tests` must be an object"),
            (self.with_(tests={"failing": "tests/a.py::t"}), "`tests.failing` must be a list"),
            (self.with_(tests={"failing": [1]}), "`tests.failing` must be a list"),
            (self.with_(types={"pyright": []}), "`types.pyright` must be an object"),
            (self.with_(types={"pyright": {"r": "3"}}), "`types.pyright.r` must be an integer"),
            (self.with_(types={"pyright": {"r": True}}), "`types.pyright.r` must be an integer"),
            (self.with_(coverage={"floor": "41"}), "`coverage.floor` must be a number"),
            (self.with_(coverage={"floor": True}), "`coverage.floor` must be a number"),
            (self.with_(set=20261106), "`set` must be a string"),
        ]
        for content, needle in cases:
            with self.subTest(needle=needle, content=content):
                self.fails(content, needle)

    def test_a_version_other_than_one_fails(self):
        """A future or absent version means a format this gate does not understand."""
        for v in (2, 0, "1", 1.0, True):
            with self.subTest(version=v):
                self.fails(self.with_(version=v), "`version` must be 1")
        bad = dict(GOOD)
        del bad["version"]
        self.fails(bad, "`version` is missing")

    def test_a_malformed_date_fails(self):
        """A date that parses loosely (`2026-1-5`) or not at all must not pick an expiry by accident."""
        for bad in ("2027-02-30", "2027-2-4", "04.02.2027", "", "2027-02-04T00:00:00"):
            with self.subTest(expires=bad):
                self.fails(self.with_(expires=bad), "`expires` is not a date")
        self.fails(self.with_(set="soon"), "`set` is not a date")

    def test_a_missing_date_fails(self):
        """Without `expires` the exception would never end."""
        for name in ("set", "expires"):
            bad = dict(GOOD)
            del bad[name]
            with self.subTest(missing=name):
                self.fails(bad, f"`{name}` is missing")

    def test_expires_must_be_after_set(self):
        """Equal or earlier dates are a zero-length or backwards window, not a baseline."""
        self.fails(self.with_(expires="2026-11-06"), "must be after `set`")
        self.fails(self.with_(expires="2026-11-05"), "must be after `set`")

    def test_a_horizon_of_90_days_passes_and_91_fails(self):
        """The cap is what stops the first date being set far away; the boundary is 90 inclusive."""
        ninety = (SET + datetime.timedelta(days=90)).isoformat()
        self.assertEqual(self.run_check(self.with_(expires=ninety)), (0, ""))
        ninety_one = (SET + datetime.timedelta(days=91)).isoformat()
        self.fails(self.with_(expires=ninety_one), "horizon is 91 days", "maximum is 90")

    def test_a_negative_count_fails(self):
        """A negative rule count would offset another rule's errors in any later sum."""
        self.fails(self.with_(types={"pyright": {"r": -1}}), "`types.pyright.r` is negative")

    def test_a_coverage_floor_outside_0_to_100_fails(self):
        """A floor over 100 can never be met and a negative one is no floor; 0 and 100 are legal."""
        self.fails(self.with_(coverage={"floor": -0.1}), "within [0, 100]")
        self.fails(self.with_(coverage={"floor": 100.1}), "within [0, 100]")
        for ok in (0, 100, 41.5):
            with self.subTest(floor=ok):
                self.assertEqual(self.run_check(self.with_(coverage={"floor": ok})), (0, ""))

    def test_a_duplicate_test_id_fails(self):
        """A repeated id is a sign of a bad merge; a set comparison would hide it."""
        self.fails(self.with_(tests={"failing": ["a::t", "b::t", "a::t"]}), "duplicate test id", "a::t")

    def test_every_reason_is_reported_at_once(self):
        """A red gate names all its causes, not one per push."""
        code, out = self.run_check(self.with_(version=2, bogus=1, coverage={"floor": 101}))
        self.assertEqual(code, 1)
        self.assertEqual(out.count("::error"), 3, out)

    def test_set_in_the_future_fails(self):
        """A far-future `set` would carry `expires` (capped only relative to `set`) out of reach: the baseline would never expire."""
        self.fails(self.with_(set="2026-11-08"), "after the run date")
        self.fails(self.with_(set="2030-01-01", expires="2030-03-31"), "after the run date", "2030-01-01")

    def test_set_today_passes(self):
        """The day the file is written is the day it is set; refusing it would break the first commit."""
        self.assertEqual(self.run_check(self.with_(set=TODAY, expires="2027-02-05")), (0, ""))

    def test_an_expired_file_is_red_and_names_the_date(self):
        """Past `expires` the standard applies again; the run must say which date ended the exception."""
        code, out = self.run_check(today="2027-02-05")
        self.assertEqual(code, 1)
        self.assertIn("::error", out)
        self.assertIn("expired on 2027-02-04", out)

    def test_the_expiry_day_itself_is_still_valid(self):
        """`expires` is the last good day; a gate that bites a day early breaks a consumer inside its window."""
        code, out = self.run_check(today="2027-02-04")
        self.assertEqual(code, 0, out)
        self.assertIn("::warning", out)
        self.assertIn("0 day(s) left", out)

    def test_a_warning_from_14_days_before_expiry(self):
        """The warning gives a consumer two weeks to get to the standard; 15 days out is still silent."""
        code, out = self.run_check(today="2027-01-21")
        self.assertEqual(code, 0, out)
        self.assertIn("::warning", out)
        self.assertIn("2027-02-04: 14 day(s) left", out)
        self.assertNotIn("::error", out)
        self.assertEqual(self.run_check(today="2027-01-20"), (0, ""))

    def test_today_defaults_to_the_utc_date(self):
        """Without `--today` the real date is used: this file expired long ago by the time anyone reads this."""
        self.path.write_text(json.dumps(self.with_(set="2000-01-01", expires="2000-02-01")))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(debt.main(["--file", str(self.path), "check"]), 1)
        self.assertIn("expired on 2000-02-01", out.getvalue())

    def test_the_default_path_is_the_airia_baseline(self):
        """The ratchet has one fixed path (design section 4), not a path input."""
        self.assertEqual(debt.DEFAULT_FILE, ".airia/debt-baseline.json")


class Compare(unittest.TestCase):
    """debt.py compare: on a pull request the baseline may only tighten against the base's copy (amendment A).

    Without this, one commit could raise a figure and move the expiry together, and
    the dated, shrinking exception would be neither dated nor shrinking.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "base.json"
        self.head = Path(tmp.name) / "head.json"

    def run_compare(self, base, head, today=TODAY, event=None):
        """(exit code, output) of `debt.py compare`; `base` None writes no base file, a str is written as it is."""
        for path, content in ((self.base, base), (self.head, head)):
            if isinstance(content, str):
                path.write_text(content)
            elif content is not None:
                path.write_text(json.dumps(content))
        argv = ["--today", today, "compare"] + (["--event", event] if event else []) + [str(self.base), str(self.head)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = debt.main(argv)
        return code, out.getvalue()

    def fails(self, base, head, *needles, **kw):
        code, out = self.run_compare(base, head, **kw)
        self.assertEqual(code, 1, out)
        self.assertIn("::error", out)
        for n in needles:
            self.assertIn(n, out)
        return out

    def head_with(self, **changes):
        return {**GOOD, **changes}

    def test_an_unchanged_file_passes_silently(self):
        """Most PRs do not touch the baseline; they must see no annotation from it."""
        self.assertEqual(self.run_compare(GOOD, GOOD), (0, ""))

    def test_a_raised_count_fails(self):
        """A rule's count rising is new type debt carried in under the old exception."""
        head = self.head_with(types={"pyright": {"reportAttributeAccessIssue": 121, "reportArgumentType": 31}})
        self.fails(GOOD, head, "`types.pyright.reportAttributeAccessIssue` rose from 120 to 121")

    def test_a_rule_absent_at_base_counts_as_zero(self):
        """A rule the base does not list is held at 0: adding it is a raise, not a new allowance."""
        head = self.head_with(types={"pyright": {**GOOD["types"]["pyright"], "reportCallIssue": 1}})
        self.fails(GOOD, head, "`types.pyright.reportCallIssue` rose from 0 to 1")
        no_types = {k: v for k, v in GOOD.items() if k != "types"}
        self.fails(no_types, GOOD, "reportArgumentType` rose from 0 to 31")

    def test_a_new_test_id_fails(self):
        """A new failure must not hide in the baseline, even when another one is fixed in the same PR."""
        head = self.head_with(tests={"failing": ["tests/test_a.py::test_x", "tests/test_new.py::test_z"]})
        out = self.fails(GOOD, head, "gained a test id", "tests/test_new.py::test_z")
        self.assertNotIn("test_y", out)

    def test_a_lower_floor_fails(self):
        """The coverage floor only rises; a lower one lets coverage slide under the exception."""
        self.fails(GOOD, self.head_with(coverage={"floor": 40.5}), "`coverage.floor` fell from 41 to 40.5")

    def test_a_floor_added_where_the_base_has_none_fails(self):
        """A base without a floor is at the standard; adding one is how a floor under 90 would sneak in."""
        no_cov = {k: v for k, v in GOOD.items() if k != "coverage"}
        self.fails(no_cov, GOOD, "`coverage.floor` (41) is new")

    def test_a_later_expires_fails(self):
        """A renewable date is not an expiry (amendment B): the end may only come sooner."""
        base = self.head_with(expires="2027-01-01")
        self.fails(base, GOOD, "`expires` moved later, from 2027-01-01 to 2027-02-04")

    def test_a_later_set_fails(self):
        """A later `set` would let a later `expires` pass the 90-day horizon: the window would slide."""
        self.fails(GOOD, self.head_with(set="2026-11-07"), "`set` moved later, from 2026-11-06 to 2026-11-07")

    def test_every_cause_is_reported_at_once(self):
        """A red gate names all its causes, not one per push."""
        head = self.head_with(
            set="2026-11-07", expires="2027-02-05", coverage={"floor": 40},
            tests={"failing": ["tests/new.py::t"]}, types={"pyright": {"reportArgumentType": 32}})
        code, out = self.run_compare(GOOD, head)
        self.assertEqual(code, 1)
        self.assertEqual(out.count("::error"), 5, out)

    def test_each_tightened_pair_passes(self):
        """Paying debt down must never be red, or nobody would commit the lower figure."""
        cases = {
            "an id dropped": self.head_with(tests={"failing": ["tests/test_a.py::test_x"]}),
            "a count lower": self.head_with(types={"pyright": {"reportAttributeAccessIssue": 119, "reportArgumentType": 31}}),
            "a rule dropped": self.head_with(types={"pyright": {"reportArgumentType": 31}}),
            "a floor higher": self.head_with(coverage={"floor": 41.5}),
            "an earlier expires": self.head_with(expires="2027-01-01"),
            "an earlier set": self.head_with(set="2026-11-05", expires="2027-02-03"),
            "sections dropped": {"version": 1, "set": "2026-11-06", "expires": "2027-02-04"},
        }
        for name, head in cases.items():
            with self.subTest(name):
                self.assertEqual(self.run_compare(GOOD, head), (0, ""))

    def test_a_first_file_set_today_passes(self):
        """The PR that adds the file is written today; refusing it would make the ratchet unadoptable."""
        self.assertEqual(self.run_compare(None, GOOD, today="2026-11-06"), (0, ""))

    def test_a_first_file_set_one_day_off_passes(self):
        """1 day either way is tolerated: the author's date and the runner's UTC date can differ by one."""
        self.assertEqual(self.run_compare(None, GOOD, today="2026-11-07"), (0, ""))
        self.assertEqual(self.run_compare(None, GOOD, today="2026-11-05"), (0, ""))

    def test_a_first_file_set_two_days_off_fails(self):
        """A `set` far from the run date would buy days past the 90-day horizon: fail loud on either side."""
        self.fails(None, GOOD, "first file", "`set` (2026-11-06)", "run date (2026-11-08)", today="2026-11-08")
        self.fails(None, GOOD, "first file", "run date (2026-11-04)", today="2026-11-04")

    def test_the_first_file_rule_applies_to_any_event_but_push(self):
        """Only a push is relaxed; an unknown event gets the strict rule, the safe direction."""
        self.fails(None, GOOD, "first file", today="2026-11-20", event="pull_request")
        self.fails(None, GOOD, "first file", today="2026-11-20", event="workflow_dispatch")

    def test_no_base_on_a_push_passes_with_a_notice(self):
        """The merge of the PR that added the file can land days after its run; that push must not go red.

        The PR was held to the first-file rule on its own run date, and `check` holds
        the horizon on every run (design section 3), so the push only says there was nothing to compare.
        """
        code, out = self.run_compare(None, GOOD, today="2026-11-20", event="push")
        self.assertEqual(code, 0, out)
        self.assertIn("::notice", out)
        self.assertIn("nothing to compare", out)
        self.assertNotIn("::error", out)

    def test_a_push_with_a_base_is_still_compared(self):
        """A direct push to the default branch must not raise a figure the PR route would refuse."""
        self.fails(self.head_with(expires="2027-01-01"), GOOD, "`expires` moved later", event="push")

    def test_both_files_are_validated_first(self):
        """Comparing a malformed file would crash or compare nonsense; each error names the file it is in."""
        out = self.fails(GOOD, self.head_with(version=2), "head: `version` must be 1")
        self.assertNotIn("base:", out)
        self.fails('{"version":', GOOD, "base: ", "not valid JSON")
        code, out = self.run_compare({"version": 2}, self.head_with(coverage={"floor": 101}))
        self.assertEqual(code, 1)
        self.assertIn("head: `coverage.floor` must be within", out)
        self.assertIn("base: `version` must be 1", out)
        self.assertNotIn("moved later", out)

    def test_a_missing_head_fails_loud(self):
        """Only the base may be absent: with the ratchet on, the head's file is required."""
        self.fails(GOOD, None, "head: ", "is missing")


class Dumps(unittest.TestCase):
    def test_the_canonical_text_round_trips_through_check(self):
        """Section 5 hands the consumer replacement JSON; it must be exactly what `check` accepts, in stable order."""
        scrambled = {
            "coverage": {"floor": 41}, "expires": "2027-02-04", "set": "2026-11-06", "version": 1,
            "types": {"pyright": {"b": 1, "a": 2}}, "tests": {"failing": ["z::t", "a::t"]},
        }
        text = debt.dumps(scrambled)
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(
            list(json.loads(text)), ["version", "set", "expires", "tests", "types", "coverage"])
        self.assertEqual(json.loads(text)["tests"]["failing"], ["a::t", "z::t"])
        self.assertEqual(list(json.loads(text)["types"]["pyright"]), ["a", "b"])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "b.json"
            path.write_text(text)
            self.assertEqual(debt.load(path), json.loads(text))
        self.assertEqual(debt.dumps({"version": 1, "set": "2026-11-06", "expires": "2027-02-04"}).count("\n"), 5)

    def test_empty_sections_are_kept_empty(self):
        """A section present with nothing in it stays valid after the writer."""
        text = debt.dumps({"version": 1, "set": "a", "expires": "b", "tests": {}, "types": {}})
        self.assertEqual(json.loads(text)["tests"], {"failing": []})
        self.assertEqual(json.loads(text)["types"], {"pyright": {}})


GATE = ROOT / ".github/workflows/reusable-python-gate.yml"
TOOLCHAIN = ROOT / ".github/actions/python-toolchain/toolchain.py"
FLOOR_STEP = "Pytest at the coverage floor"


class CoverageFloor(unittest.TestCase):
    """Slice 3: the toolchain turns the baseline into the floor, and the sliced pytest step holds it.

    Each case runs the shipped `toolchain.py standard` and then the shipped `Pytest at the coverage floor`
    block with the floor the first printed, so what is proved is the wiring, not a copy of it.
    """

    def setUp(self):
        self.r = Runner(self)
        self.r.tool("pytest", FAKE_PYTEST)
        self.project = self.r.tmp / "project"
        (self.project / ".airia").mkdir(parents=True)
        (self.project / ".python-version").write_text("3.12\n")
        self.file = self.project / ".airia/debt-baseline.json"

    def baseline(self, floor, **changes):
        content = {"version": 1, "set": "2026-11-06", "expires": "2027-02-04", **changes}
        if floor is not None:
            content["coverage"] = {"floor": floor}
        self.file.write_text(json.dumps(content))

    def standard(self, ratchet="true", requested="90", today=TODAY):
        return subprocess.run(
            [sys.executable, str(TOOLCHAIN), "standard"], cwd=self.project, capture_output=True, text=True,
            env=self.r.env(TYPE_CHECKER="pyright", COVERAGE_FLOOR=requested, DEBT_RATCHET=ratchet,
                           DEBT_TODAY=today, PYTHON_VERSION="", GITHUB_REPOSITORY="o/r"))

    def pytest_step(self, floor, percent):
        (self.r.bin / "calls").unlink(missing_ok=True)
        return self.r.run(GATE, FLOOR_STEP, cwd=self.project, FLOOR=floor, FAKE_PERCENT=percent, DURATIONS="25")

    def test_a_baseline_floor_under_90_is_the_floor_pytest_holds(self):
        """The point of the ratchet: a consumer with known debt is green at its measured figure, red under it.

        0 and 41 are floors the `coverage-floor` input refuses; only a valid baseline may carry them. 90 is the
        standard itself: the baseline must not be able to loosen it."""
        for floor, below, above in ((0, None, "0.0"), (41, "40.9", "41.0"), (90, "89.9", "90.0")):
            with self.subTest(floor=floor):
                self.baseline(floor)
                p = self.standard()
                self.assertEqual(p.returncode, 0, p.stdout)
                self.assertEqual(self.r.outputs()["coverage-floor"], str(floor))
                self.r.output.unlink()
                if below is not None:
                    low = self.pytest_step(str(floor), below)
                    self.assertEqual(low.returncode, 1, low.stdout)
                high = self.pytest_step(str(floor), above)
                self.assertEqual(high.returncode, 0, high.stdout + high.stderr)
                self.assertIn(f"--cov-fail-under={floor}", (self.r.bin / "calls").read_text())

    def test_a_floor_of_zero_still_needs_a_coverage_report(self):
        """Floor 0 must not mean "not measured": a suite with no report fails whatever the floor says."""
        self.baseline(0)
        self.standard()
        p = self.pytest_step("0", "")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=No coverage figure::", p.stdout)

    def test_with_the_input_off_the_file_is_ignored_and_said_so(self):
        """Default `false` leaves every existing caller as today; a stray file must neither lower the floor
        nor fail the run, and the notice tells its owner why nothing happened."""
        self.baseline(41)
        p = self.standard(ratchet="false")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertNotIn("coverage-floor", self.r.outputs())
        self.assertIn("::notice title=Debt baseline::", p.stdout)
        self.file.write_text("not json")
        self.assertEqual(self.standard(ratchet="false").returncode, 0)

    def test_with_the_input_off_and_no_file_nothing_is_said(self):
        p = self.standard(ratchet="false")
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_with_the_input_on_a_missing_file_fails_loud(self):
        """Never a fallback to the standard: a deleted file with the input on is a red build, not a quiet 90."""
        p = self.standard()
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=AIRIA standard::debt baseline:", p.stdout)
        self.assertIn("is missing", p.stdout)
        self.assertNotIn("coverage-floor", self.r.outputs())

    def test_with_the_input_on_a_malformed_file_fails_loud(self):
        """A floor read from a file that does not validate would be a floor anyone could type."""
        self.file.write_text("{")
        p = self.standard()
        self.assertEqual(p.returncode, 1)
        self.assertIn("not valid JSON", p.stdout)
        self.baseline(101)
        p = self.standard()
        self.assertEqual(p.returncode, 1)
        self.assertIn("must be within [0, 100]", p.stdout)
        self.assertEqual(self.r.outputs(), {})

    def test_the_coverage_floor_input_is_refused_under_90_in_both_modes(self):
        """Only the baseline can go under 90; a valid baseline must not open the input as well."""
        self.baseline(41)
        for ratchet in ("true", "false"):
            with self.subTest(ratchet=ratchet):
                p = self.standard(ratchet=ratchet, requested="41")
                self.assertEqual(p.returncode, 1)
                self.assertIn("below the standard's 90", p.stdout)
                self.assertEqual(self.r.outputs(), {})

    def test_a_raised_input_is_kept_over_a_lower_baseline_floor(self):
        """A project that asked for 95 gave that promise itself; a baseline of 41 must not talk it down."""
        self.baseline(41)
        self.assertEqual(self.standard(requested="95").returncode, 0)
        self.assertEqual(self.r.outputs()["coverage-floor"], "95")

    def test_a_baseline_without_a_coverage_section_leaves_the_standard(self):
        """Every section is optional; an absent one means the standard for that gate."""
        self.baseline(None)
        self.assertEqual(self.standard().returncode, 0)
        self.assertEqual(self.r.outputs()["coverage-floor"], "90")

    def test_past_the_expiry_the_standard_floor_applies(self):
        """The date is a hard stop: after it the 41 is gone and the 90 is back (`check` is what turns red)."""
        self.baseline(41)
        self.assertEqual(self.standard(today="2027-02-04").returncode, 0)
        self.assertEqual(self.r.outputs()["coverage-floor"], "41")
        self.r.output.unlink()
        self.assertEqual(self.standard(today="2027-02-05").returncode, 0)
        self.assertEqual(self.r.outputs()["coverage-floor"], "90")

    def test_the_action_checks_the_file_only_with_the_input_on(self):
        """The action's own step runs `debt.py check`, which is the red past the expiry."""
        action = ROOT / ".github/actions/python-toolchain/action.yml"
        now = datetime.datetime.now(datetime.UTC).date()
        env = dict(GITHUB_ACTION_PATH=str(action.parent))
        self.baseline(41, set=now.isoformat(), expires=(now + datetime.timedelta(days=60)).isoformat())
        p = self.r.run(action, "Check the debt baseline", cwd=self.project, **env)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.baseline(41, set="2026-01-01", expires="2026-01-02")
        p = self.r.run(action, "Check the debt baseline", cwd=self.project, **env)
        self.assertEqual(p.returncode, 1)
        self.assertIn("expired", p.stdout)
        step = next(s for s in yaml.safe_load(action.read_text())["runs"]["steps"] if s.get("name") == "Check the debt baseline")
        self.assertEqual(step["if"], "inputs.debt-ratchet == 'true'")


class CompareStep(unittest.TestCase):
    """The lint job's `Debt baseline only tightens` step: the base is HEAD^1 of the PR's merge commit."""

    def setUp(self):
        self.r = Runner(self)
        self.project = self.r.tmp / "project"
        (self.project / ".airia").mkdir(parents=True)
        self.today = datetime.datetime.now(datetime.UTC).date()
        self.file = self.project / ".airia/debt-baseline.json"
        self.git("init", "-q", "-b", "base")

    def git(self, *args):
        env = {**self.r.env(), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"}
        p = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=self.project, env=env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def content(self, floor, days=30, **extra):
        return json.dumps({"version": 1, "set": self.today.isoformat(),
                           "expires": (self.today + datetime.timedelta(days=days)).isoformat(),
                           "coverage": {"floor": floor}, **extra})

    def merge(self, base: str | None, head: str):
        """A pull request's merge commit: HEAD^1 is the base with `base` committed, HEAD has `head`."""
        (self.project / "README").write_text("x")
        if base is not None:
            self.file.write_text(base)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("checkout", "-q", "-b", "pr")
        self.file.write_text(head)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "pr")
        self.git("checkout", "-q", "base")
        (self.project / "other").write_text("y")  # the base moved on, so the merge is a real one
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "moved")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "pr")

    def run_step(self, ratchet="true", event="pull_request"):
        return self.r.run(GATE, "Debt baseline only tightens", cwd=self.project, RATCHET=ratchet, EVENT=event,
                          AIRIA_DEBT=str(SCRIPT))

    def test_a_raise_against_the_base_fails_on_a_pull_request(self):
        """The file alone cannot tell a raise from a first write: only the base's copy can (amendment A)."""
        self.merge(self.content(60), self.content(41))
        p = self.run_step()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("`coverage.floor` fell from 60 to 41", p.stdout)

    def test_a_new_failing_id_against_the_base_fails(self):
        self.merge(self.content(41), self.content(41, tests={"failing": ["tests/t.py::a"]}))
        p = self.run_step()
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("gained a test id", p.stdout)

    def test_a_tighter_head_passes(self):
        self.merge(self.content(41), self.content(55))
        self.assertEqual(self.run_step().returncode, 0)

    def test_a_first_file_passes_when_set_is_today(self):
        self.merge(None, self.content(41))
        self.assertEqual(self.run_step().returncode, 0)

    def test_a_first_file_with_a_far_future_set_fails(self):
        """Otherwise a first file could buy extra days by dating itself ahead."""
        head = json.loads(self.content(41))
        head["set"] = (self.today + datetime.timedelta(days=10)).isoformat()
        head["expires"] = (self.today + datetime.timedelta(days=40)).isoformat()
        self.merge(None, json.dumps(head))
        self.assertEqual(self.run_step().returncode, 1)

    def test_a_missing_head_file_fails_loud(self):
        """Deleting the file with the input on is red, not a skipped comparison."""
        self.merge(self.content(41), self.content(42))
        self.file.unlink()
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "delete")
        p = self.run_step()
        self.assertEqual(p.returncode, 1)
        self.assertIn("head:", p.stdout)

    def test_with_the_input_off_nothing_is_compared(self):
        """Default `false` changes nothing for existing callers, even with a raised file in the tree."""
        self.merge(self.content(60), self.content(41))
        p = self.run_step(ratchet="false")
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("::error", p.stdout)

    def test_on_a_push_nothing_is_compared(self):
        self.merge(self.content(60), self.content(41))
        self.assertEqual(self.run_step(event="push").returncode, 0)

    def test_a_checkout_without_a_first_parent_fails_loud(self):
        """A shallow single-commit checkout would otherwise pass every raise unseen."""
        (self.project / "README").write_text("x")
        self.file.write_text(self.content(41))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "only")
        p = self.run_step()
        self.assertEqual(p.returncode, 1)
        self.assertIn("no first parent", p.stdout)


# A pyright that prints $FAKE_PYRIGHT_REPORT (a file) as --outputjson does, and records its argv.
FAKE_PYRIGHT = """\
#!/bin/sh
echo "pyright $*" >> "$(dirname "$0")/calls"
cat "$FAKE_PYRIGHT_REPORT"
exit "${FAKE_PYRIGHT_EXIT:-0}"
"""


def diagnostic(rule="reportArgumentType", severity="error", **extra):
    d = {"file": "/p/src/a.py", "severity": severity, "message": "bad", "range": {"start": {"line": 3, "character": 0}}}
    if rule is not None:
        d["rule"] = rule
    return {**d, **extra}


class PyrightStep(unittest.TestCase):
    """The lint job's `Type check` step with the ratchet on: pyright's errors per rule against `types.pyright`."""

    def setUp(self):
        self.r = Runner(self)
        self.r.tool("pyright", FAKE_PYRIGHT)
        self.r.tool("mypy", "#!/bin/sh\necho \"mypy $*\" >> \"$(dirname \"$0\")/calls\"\nexit \"${FAKE_MYPY_EXIT:-0}\"\n")
        self.project = self.r.tmp / "project"
        (self.project / ".airia").mkdir(parents=True)
        self.report = self.r.tmp / "report.json"
        self.today = datetime.datetime.now(datetime.UTC).date()

    def baseline(self, pyright_rules=None, **extra):
        data = {"version": 1, "set": self.today.isoformat(),
                "expires": (self.today + datetime.timedelta(days=60)).isoformat(), **extra}
        if pyright_rules is not None:
            data["types"] = {"pyright": pyright_rules}
        (self.project / ".airia/debt-baseline.json").write_text(json.dumps(data))

    def run_step(self, diagnostics, exit_code="1", checker="pyright", ratchet="true", **env):
        text = diagnostics if isinstance(diagnostics, str) else json.dumps({"generalDiagnostics": diagnostics})
        self.report.write_text(text)
        return self.r.run(GATE, "Type check", cwd=self.project, PATHS="src", TYPE_CHECKER=checker, RATCHET=ratchet,
                          AIRIA_DEBT=str(SCRIPT), FAKE_PYRIGHT_REPORT=str(self.report), FAKE_PYRIGHT_EXIT=exit_code, **env)

    def calls(self):
        f = self.r.bin / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def test_a_rule_above_its_baseline_fails_and_names_the_rule_and_the_error(self):
        self.baseline({"reportArgumentType": 1})
        p = self.run_step([diagnostic(), diagnostic(line=1)])
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("reportArgumentType: 2 errors, the baseline allows 1", p.stdout)
        self.assertIn("/p/src/a.py:4 bad (reportArgumentType)", p.stdout)

    def test_a_rule_at_its_baseline_passes_silently(self):
        self.baseline({"reportArgumentType": 2})
        p = self.run_step([diagnostic(), diagnostic()])
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_a_rule_below_its_baseline_passes_and_reports_the_tightening(self):
        self.baseline({"reportArgumentType": 3, "reportAttributeAccessIssue": 2})
        p = self.run_step([diagnostic(), diagnostic(rule="reportAttributeAccessIssue"),
                           diagnostic(rule="reportAttributeAccessIssue")])
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("::notice", p.stdout)
        self.assertIn("reportArgumentType 3 -> 1", p.stdout)
        # Slice 6 appends the full replacement JSON, which holds the unchanged rule; what *improved* must not name it.
        self.assertNotIn("reportAttributeAccessIssue", p.stdout.split(". Replacement ")[0])

    def test_a_rule_that_vanished_is_a_tightening(self):
        self.baseline({"reportArgumentType": 2})
        p = self.run_step([], exit_code="0")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("reportArgumentType 2 -> 0", p.stdout)

    def test_the_notice_carries_a_replacement_that_round_trips_through_check_and_the_job_summary_holds_it_too(self):
        """Section 5: the consumer commits the notice's JSON, so it must be a file `check` accepts, equal to what
        the writer would produce, and the summary must carry the same text for a person to copy."""
        self.baseline({"reportArgumentType": 3, "reportAttributeAccessIssue": 2})
        summary_file = self.r.tmp / "summary.md"
        p = self.run_step([diagnostic(), diagnostic(rule="reportAttributeAccessIssue"),
                           diagnostic(rule="reportAttributeAccessIssue")], GITHUB_STEP_SUMMARY=str(summary_file))
        self.assertEqual(p.returncode, 0, p.stdout)
        replacement = notice_json(p.stdout)
        self.assertEqual(replacement["types"]["pyright"], {"reportArgumentType": 1, "reportAttributeAccessIssue": 2})
        self.assertEqual((replacement["set"], replacement["expires"]),
                         (self.today.isoformat(), (self.today + datetime.timedelta(days=60)).isoformat()))
        committed = self.r.tmp / "committed.json"
        committed.write_text(json.dumps(replacement))
        self.assertEqual(debt.main(["--file", str(committed), "--today", self.today.isoformat(), "check"]), 0)
        text = summary_file.read_text()
        self.assertIn("reportArgumentType 3 -> 1", text)
        self.assertEqual(json.loads(text.split("```json\n")[1].split("```")[0]), replacement)

    def test_no_notice_and_no_summary_when_nothing_improved(self):
        self.baseline({"reportArgumentType": 1})
        summary_file = self.r.tmp / "summary.md"
        p = self.run_step([diagnostic()], GITHUB_STEP_SUMMARY=str(summary_file))
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        self.assertFalse(summary_file.exists())

    def test_a_rule_absent_from_the_file_counts_as_zero(self):
        """One rule falling must not pay for another rising from nothing."""
        self.baseline({"reportArgumentType": 5})
        p = self.run_step([diagnostic(rule="reportCallIssue")])
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("reportCallIssue: 1 errors, the baseline allows 0", p.stdout)

    def test_a_file_without_a_types_section_allows_no_error(self):
        self.baseline(None)
        self.assertEqual(self.run_step([diagnostic()]).returncode, 1)

    def test_an_error_with_no_rule_cannot_be_baselined(self):
        self.baseline({"reportArgumentType": 9})
        p = self.run_step([diagnostic(rule=None, message="syntax")])
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("(no rule): 1 errors", p.stdout)

    def test_a_non_error_severity_is_ignored(self):
        self.baseline({})
        p = self.run_step([diagnostic(severity="warning"), diagnostic(severity="information"),
                           diagnostic(severity="hint")], exit_code="0")
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_output_that_is_not_json_fails_loud(self):
        """A crashed or truncated report must not read as 'no errors'."""
        self.baseline({})
        p = self.run_step("Traceback (most recent call last):", exit_code="0")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Debt baseline::", p.stdout)
        self.assertIn("not pyright's JSON output", p.stdout)

    def test_json_without_diagnostics_fails_loud(self):
        self.baseline({})
        for text in ("[]", '{"summary": {}}', '{"generalDiagnostics": [1]}'):
            with self.subTest(text=text):
                p = self.run_step(text, exit_code="0")
                self.assertEqual(p.returncode, 1)
                self.assertIn("not pyright's JSON output", p.stdout)

    def test_pyright_failing_to_run_fails_even_with_a_valid_report(self):
        self.baseline({"reportArgumentType": 9})
        p = self.run_step([], exit_code="2")
        self.assertEqual(p.returncode, 1)
        self.assertIn("pyright exited 2", p.stdout)

    def test_a_missing_baseline_fails_loud(self):
        p = self.run_step([], exit_code="0")
        self.assertEqual(p.returncode, 1)
        self.assertIn("is missing", p.stdout)

    def test_past_the_expiry_the_baselined_counts_no_longer_apply(self):
        self.baseline({"reportArgumentType": 5}, set="2026-01-01", expires="2026-01-02")
        p = self.run_step([diagnostic()])
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("the baseline allows 0", p.stdout)

    def test_pyright_runs_with_the_json_flag_against_the_project_environment(self):
        self.baseline({})
        self.run_step([], exit_code="0")
        self.assertEqual(self.calls(), [f"pyright --outputjson --pythonpath {self.project.resolve()}/.venv/bin/python src"])

    def test_with_the_input_off_pyright_exit_code_decides_and_the_file_is_ignored(self):
        """Byte-for-byte as today: no --outputjson, no baseline read, pyright's own exit code."""
        self.baseline({"reportArgumentType": 9})
        self.assertEqual(self.run_step([diagnostic()], exit_code="1", ratchet="false").returncode, 1)
        self.assertEqual(self.run_step([diagnostic()], exit_code="0", ratchet="false").returncode, 0)
        self.assertNotIn("--outputjson", " ".join(self.calls()))

    def test_mypy_strict_with_a_types_section_fails_loud_before_mypy_runs(self):
        self.baseline({"reportArgumentType": 1})
        p = self.run_step([], checker="mypy-strict")
        self.assertEqual(p.returncode, 1)
        self.assertIn("mypy-strict", p.stdout)
        self.assertEqual(self.calls(), [])

    def test_mypy_strict_without_a_types_section_runs_as_before(self):
        self.baseline(None, coverage={"floor": 41})
        self.assertEqual(self.run_step([], checker="mypy-strict").returncode, 0)
        self.assertEqual(self.calls(), ["mypy --strict src"])
        self.assertEqual(self.run_step([], checker="mypy-strict", ratchet="false").returncode, 0)

    def test_mypy_strict_with_an_unreadable_baseline_fails_loud(self):
        p = self.run_step([], checker="mypy-strict")
        self.assertEqual(p.returncode, 1)
        self.assertIn("is missing", p.stdout)
        self.assertEqual(self.calls(), [])


# A pytest that prints the file $FAKE_PYTEST_OUT as pytest's terminal output, writes the JSON coverage report it was
# asked for at $FAKE_PERCENT, records its argv, and exits with $FAKE_PYTEST_EXIT.
FAKE_PYTEST_OUTPUT = """\
#!/bin/sh
echo "pytest $*" >> "$(dirname "$0")/calls"
[ -n "$FAKE_PYTEST_OUT" ] && cat "$FAKE_PYTEST_OUT"
for a in "$@"; do
  case "$a" in --cov-report=json:*) [ -n "$FAKE_PERCENT" ] && echo "{\\"totals\\": {\\"percent_covered\\": $FAKE_PERCENT}}" > "${a#--cov-report=json:}";; esac
done
exit "${FAKE_PYTEST_EXIT:-0}"
"""


def summary(*lines, tail="2 failed, 3 passed in 0.12s"):
    """What `pytest -rfE` ends with: the short test summary block, then the totals line."""
    return "\n".join([
        "=================================== FAILURES ===================================",
        "FAILED inside a failure report, which is not the summary",
        "=========================== short test summary info ============================",
        *lines,
        f"=================== {tail} ===================", ""])


PARAM_ID = "tests/test_c.py::TestC::test_p[a b - c [x] d]"


class TestsStep(unittest.TestCase):
    """The test job's `Pytest at the coverage floor` step with the ratchet on: failing ids against `tests.failing`.

    Each case runs the shipped block with a fake pytest that prints a canned `-rfE` output and exits with a
    chosen code, so the parse, the exit-code rules and the wiring to the coverage floor are the shipped ones.
    """

    def setUp(self):
        self.r = Runner(self)
        self.r.tool("pytest", FAKE_PYTEST_OUTPUT)
        self.project = self.r.tmp / "project"
        (self.project / ".airia").mkdir(parents=True)
        self.out = self.r.tmp / "pytest-out.txt"
        self.today = datetime.datetime.now(datetime.UTC).date()

    def baseline(self, failing=None, **extra):
        data = {"version": 1, "set": self.today.isoformat(),
                "expires": (self.today + datetime.timedelta(days=60)).isoformat(), **extra}
        if failing is not None:
            data["tests"] = {"failing": failing}
        (self.project / ".airia/debt-baseline.json").write_text(json.dumps(data))

    def run_step(self, output, code="1", ratchet="true", percent="95", floor="90", **env):
        self.out.write_text(output)
        (self.r.bin / "calls").unlink(missing_ok=True)
        return self.r.run(GATE, FLOOR_STEP, cwd=self.project, FLOOR=floor, DURATIONS="25", RATCHET=ratchet,
                          AIRIA_DEBT=str(SCRIPT), FAKE_PYTEST_OUT=str(self.out), FAKE_PYTEST_EXIT=code,
                          FAKE_PERCENT=percent, **env)

    def test_the_notice_carries_ids_and_coverage_as_a_replacement_that_round_trips_through_check(self):
        """A fixed id and a coverage figure above the floor tighten together; the notice's JSON is a valid file,
        and the summary holds the same one for a person to copy."""
        self.baseline(["tests/test_a.py::test_x", "tests/test_b.py::test_old"], coverage={"floor": 41})
        summary_file = self.r.tmp / "summary.md"
        p = self.run_step(summary("FAILED tests/test_a.py::test_x - boom"), percent="57.9", floor="41",
                          GITHUB_STEP_SUMMARY=str(summary_file))
        self.assertEqual(p.returncode, 0, p.stdout)
        replacement = notice_json(p.stdout)
        self.assertEqual(replacement["tests"], {"failing": ["tests/test_a.py::test_x"]})
        self.assertEqual(replacement["coverage"], {"floor": 57})
        self.assertIn("coverage floor 41 -> 57", p.stdout.split(". Replacement ")[0])
        committed = self.r.tmp / "committed.json"
        committed.write_text(json.dumps(replacement))
        self.assertEqual(debt.main(["--file", str(committed), "--today", self.today.isoformat(), "check"]), 0)
        text = summary_file.read_text()
        self.assertIn("test tests/test_b.py::test_old no longer fails", text)
        self.assertEqual(json.loads(text.split("```json\n")[1].split("```")[0]), replacement)

    def test_a_coverage_above_the_floor_alone_is_noticed(self):
        self.baseline([], coverage={"floor": 41})
        p = self.run_step("", code="0", percent="60.2", floor="41")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(notice_json(p.stdout)["coverage"], {"floor": 60})

    def test_a_coverage_that_only_reaches_the_floor_is_not_noticed(self):
        self.baseline([], coverage={"floor": 41})
        p = self.run_step("", code="0", percent="41.9", floor="41")
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_a_new_failure_gives_no_replacement_even_when_coverage_rose(self):
        self.baseline(["tests/test_a.py::test_x"], coverage={"floor": 41})
        p = self.run_step(summary("FAILED tests/test_n.py::test_new - boom"), percent="80")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertNotIn("::notice", p.stdout)

    def test_a_failing_id_in_the_baseline_passes(self):
        self.baseline(["tests/test_a.py::test_x"])
        p = self.run_step(summary("FAILED tests/test_a.py::test_x - AssertionError: 1 != 2"))
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertNotIn("::error", p.stdout)
        self.assertNotIn("::notice", p.stdout)

    def test_a_new_failing_id_fails_even_when_a_baselined_one_got_fixed(self):
        """The point of ids over a count: the fixed test must not pay for the new failure."""
        self.baseline(["tests/test_a.py::test_x", "tests/test_b.py::test_old"])
        p = self.run_step(summary("FAILED tests/test_a.py::test_x - boom", "FAILED tests/test_b.py::test_new - boom"))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("::error title=Test outside the debt baseline::tests/test_b.py::test_new fails", p.stdout)
        self.assertNotIn("test_x fails", p.stdout)
        self.assertNotIn("::notice", p.stdout)

    def test_a_baselined_id_that_now_passes_passes_the_gate_and_is_noticed(self):
        self.baseline(["tests/test_a.py::test_x", "tests/test_b.py::test_old"])
        p = self.run_step(summary("FAILED tests/test_a.py::test_x - boom"))
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("::notice title=Debt baseline can tighten::1 baselined test(s) did not fail: "
                      "tests/test_b.py::test_old", p.stdout)
        # Slice 6 appends the full replacement JSON, which holds the still-failing id; what *improved* must not name it.
        self.assertNotIn("test_a.py::test_x", p.stdout.split("::notice")[1].split(". Replacement ")[0])

    def test_an_erroring_id_counts_like_a_failing_one(self):
        """A fixture error or a collection error is a red test too: ERROR lines are held to the baseline."""
        self.baseline(["tests/test_a.py::test_x"])
        p = self.run_step(summary("FAILED tests/test_a.py::test_x - boom",
                                  "ERROR tests/test_b.py::test_setup - RuntimeError: fixture",
                                  "ERROR tests/test_broken.py - ImportError: no module"))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("tests/test_b.py::test_setup fails and is not in tests.failing", p.stdout)
        self.assertIn("tests/test_broken.py fails and is not in tests.failing", p.stdout)
        self.baseline(["tests/test_a.py::test_x", "tests/test_b.py::test_setup", "tests/test_broken.py"])
        self.assertEqual(self.run_step(summary("FAILED tests/test_a.py::test_x - boom",
                                               "ERROR tests/test_b.py::test_setup - RuntimeError: fixture",
                                               "ERROR tests/test_broken.py - ImportError: no module")).returncode, 0)

    def test_parametrised_ids_keep_their_brackets_spaces_and_dashes(self):
        """The id is cut at the ` - ` that starts the message, not at one inside the `[...]` of a parameter."""
        ids = ["tests/test_b.py::TestC::test_y[1]", "tests/test_b.py::test_z[hello world]", PARAM_ID,
               "tests/test_d.py::test_q[]", "tests/test_d.py::test_r[a]b]"]
        self.baseline(ids)
        lines = [f"FAILED {i} - AssertionError: assert 'x - y' == 'z'" for i in ids[:4]]
        lines.append(f"FAILED {ids[4]}")  # no message at all
        p = self.run_step(summary(*lines))
        self.assertEqual(p.returncode, 0, p.stdout)
        # No error and no notice: every id was read whole, found in the baseline, and none of them is unused.
        self.assertNotIn("::error", p.stdout)
        self.assertNotIn("::notice", p.stdout)

    def test_an_id_that_differs_only_in_its_parameter_is_a_new_id(self):
        self.baseline(["tests/test_b.py::test_z[1]"])
        p = self.run_step(summary("FAILED tests/test_b.py::test_z[2] - boom"))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("tests/test_b.py::test_z[2] fails", p.stdout)

    def test_only_the_summary_block_counts(self):
        """A test that prints FAILED, or the failure report above the summary, can neither add nor hide an id."""
        self.baseline(["tests/test_a.py::test_x"])
        noisy = "FAILED tests/test_zz.py::test_printed - from stdout\n" + summary("FAILED tests/test_a.py::test_x - boom")
        p = self.run_step(noisy + "FAILED tests/test_after.py::t - after the block\n")
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_colour_codes_are_stripped(self):
        self.baseline(["tests/test_a.py::test_x"])
        """A caller with FORCE_COLOR / PY_COLORS still gets its ids read: the escape codes are not part of an id."""
        coloured = summary("\x1b[31mFAILED\x1b[0m tests/test_a.py::test_x - \x1b[31mboom\x1b[0m")
        p = self.run_step(coloured)
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_exit_codes_2_to_5_fail_outright_even_with_every_id_baselined(self):
        """Interrupted, internal error, usage error, nothing collected: not a run the baseline can judge."""
        self.baseline(["tests/test_a.py::test_x"])
        for code, what in (("2", "was interrupted"), ("3", "hit an internal error"), ("4", "was used wrongly"),
                           ("5", "collected no tests")):
            with self.subTest(code=code):
                p = self.run_step(summary("FAILED tests/test_a.py::test_x - boom"), code=code)
                self.assertEqual(p.returncode, 1, p.stdout)
                self.assertIn(f"pytest exited {code} and {what}", p.stdout)

    def test_any_other_exit_code_fails_too(self):
        self.baseline([])
        p = self.run_step("", code="139")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("pytest exited 139 and failed to run", p.stdout)

    def test_exit_1_with_no_parsed_id_fails(self):
        """A crash (or a coverage miss with nothing failing) is not 'no failures', whatever the baseline holds."""
        self.baseline([])
        for output in ("", "Traceback (most recent call last):\n", summary(tail="1 passed in 0.1s"),
                       "FAILED tests/test_a.py::test_x - outside any summary\n"):
            with self.subTest(output=output[:30]):
                p = self.run_step(output, code="1")
                self.assertEqual(p.returncode, 1, p.stdout)
                self.assertIn("exited 1 but no FAILED or ERROR line", p.stdout)

    def test_exit_0_passes_with_a_non_empty_baseline_and_notices_the_tightening(self):
        self.baseline(["tests/test_a.py::test_x"])
        p = self.run_step(summary(tail="5 passed in 0.1s"), code="0")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("tests/test_a.py::test_x", p.stdout)
        self.assertIn("::notice", p.stdout)

    def test_exit_0_ignores_failed_looking_lines(self):
        """Exit 0 means pytest saw no failure; a printed line must not turn a green run red."""
        self.baseline([])
        self.assertEqual(self.run_step(summary("FAILED tests/test_a.py::test_x - printed"), code="0").returncode, 0)

    def test_a_file_without_a_tests_section_allows_no_failure(self):
        self.baseline(None, coverage={"floor": 41})
        p = self.run_step(summary("FAILED tests/test_a.py::test_x - boom"))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("tests/test_a.py::test_x fails", p.stdout)

    def test_past_the_expiry_no_baselined_failure_is_allowed(self):
        self.baseline(["tests/test_a.py::test_x"], set="2026-01-01", expires="2026-01-02")
        p = self.run_step(summary("FAILED tests/test_a.py::test_x - boom"))
        self.assertEqual(p.returncode, 1, p.stdout)

    def test_a_missing_or_invalid_baseline_fails_loud_and_never_falls_back(self):
        p = self.run_step(summary("FAILED tests/test_a.py::test_x - boom"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Debt baseline::", p.stdout)
        self.assertIn("is missing", p.stdout)
        (self.project / ".airia/debt-baseline.json").write_text("{")
        self.assertIn("not valid JSON", self.run_step(summary(tail="1 passed"), code="0").stdout)

    def test_the_coverage_floor_still_holds_when_every_failing_id_is_baselined(self):
        """Failing tests lower coverage; the ids passing must not let a figure under the floor through."""
        self.baseline(["tests/test_a.py::test_x"])
        out = summary("FAILED tests/test_a.py::test_x - boom")
        low = self.run_step(out, floor="41", percent="40.9")
        self.assertEqual(low.returncode, 1, low.stdout)
        self.assertEqual(self.run_step(out, floor="41", percent="41.0").returncode, 0)
        self.assertEqual(self.run_step(out, floor="41", percent="").returncode, 1)
        self.assertIn("::error title=No coverage figure::", self.run_step(out, floor="41", percent="").stdout)

    def test_pytest_runs_once_with_rfE_and_the_same_floor_flags(self):
        self.baseline(["tests/test_a.py::test_x"])
        self.run_step(summary("FAILED tests/test_a.py::test_x - boom"), floor="41", percent="95")
        report = self.r.tmp / "coverage.json"
        self.assertEqual((self.r.bin / "calls").read_text().splitlines(), [
            f"pytest --durations=25 -rfE --cov-report=term-missing --cov-report=json:{report} --cov-fail-under=41"])

    def test_pytests_output_still_reaches_the_log(self):
        self.baseline(["tests/test_a.py::test_x"])
        p = self.run_step(summary("FAILED tests/test_a.py::test_x - boom"))
        self.assertIn("FAILED tests/test_a.py::test_x - boom", p.stdout)

    def test_with_the_ratchet_off_pytest_runs_as_before_and_ids_are_not_judged(self):
        """Default false: no -rfE, no baseline read (none exists here), and any failure is a red step."""
        p = self.run_step(summary("FAILED tests/test_a.py::test_x - boom"), ratchet="false", floor="90", percent="95")
        self.assertEqual(p.returncode, 1)
        report = self.r.tmp / "coverage.json"
        self.assertEqual((self.r.bin / "calls").read_text().splitlines(), [
            f"pytest --durations=25 --cov-report=term-missing --cov-report=json:{report} --cov-fail-under=90"])
        self.assertNotIn("Debt baseline", p.stdout)
        self.assertEqual(self.run_step("", code="0", ratchet="false").returncode, 0)

    def test_a_matrix_of_two_pythons_is_compared_one_by_one(self):
        """Each Python is its own job and runs the step on its own output: 3.13 failing an id 3.12 does not
        fails 3.13 only, and a baseline id failing on one Python only is held for the one that fails it."""
        matrix = yaml.safe_load(GATE.read_text())["jobs"]["test"]["strategy"]
        self.assertFalse(matrix["fail-fast"])
        self.assertIn("python", matrix["matrix"])
        self.baseline(["tests/test_a.py::test_x"])
        py312 = self.run_step(summary("FAILED tests/test_a.py::test_x - boom"))
        py313 = self.run_step(summary("FAILED tests/test_a.py::test_x - boom",
                                      "FAILED tests/test_a.py::test_only_313 - boom"))
        self.assertEqual(py312.returncode, 0, py312.stdout)
        self.assertEqual(py313.returncode, 1, py313.stdout)
        self.assertIn("test_only_313 fails", py313.stdout)
        # Neither run leaves state the other reads: 3.12 again, after 3.13 failed, is still green.
        self.assertEqual(self.run_step(summary("FAILED tests/test_a.py::test_x - boom")).returncode, 0)


def notice_json(stdout: str) -> dict:
    """The replacement JSON a `Debt baseline can tighten` notice carries (one line, after the last `: `)."""
    lines = [line for line in stdout.splitlines() if line.startswith("::notice title=Debt baseline can tighten::")]
    assert len(lines) == 1, stdout
    return json.loads(lines[0][lines[0].index(": {") + 2:])


class Writer(unittest.TestCase):
    """debt.py write (what `ci-local.sh --write-debt-baseline` runs) and `tighten`, design section 5.

    The writer takes the minimum of the old file and the measurement, drops what is fixed, keeps the dates,
    and refuses a measurement that is worse: a person commits the file, and it must never be a way to raise.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "debt-baseline.json"
        self.path.write_text(json.dumps(GOOD))

    def measure(self, rules=None, pytest=None, percent=None):
        """The arguments of `debt.py write` for a measurement; each part is optional."""
        args = []
        if rules is not None:
            report = self.dir / "pyright.json"
            report.write_text(json.dumps({"generalDiagnostics": [
                diagnostic(rule=rule) for rule, n in rules.items() for _ in range(n)]}))
            args += ["--pyright", str(report)]
        if pytest is not None:
            status, lines = pytest
            out = self.dir / "pytest.out"
            out.write_text(summary(*[f"FAILED {i} - boom" for i in lines]))
            args += ["--tests", str(status), str(out)]
        if percent is not None:
            report = self.dir / "coverage.json"
            report.write_text(json.dumps({"totals": {"percent_covered": percent}}))
            args += ["--coverage", str(report)]
        return args

    def write(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = debt.main(["--file", str(self.path), "write", *args])
        return code, out.getvalue()

    def refuses(self, *args, needle):
        before = self.path.read_bytes()
        code, out = self.write(*args)
        self.assertEqual(code, 1, out)
        self.assertIn(needle, out)
        self.assertEqual(self.path.read_bytes(), before, "a refusal writes nothing")

    def test_a_rule_above_its_baseline_is_refused_and_the_file_is_untouched(self):
        self.refuses(*self.measure(rules={"reportAttributeAccessIssue": 121, "reportArgumentType": 31}),
                     needle="pyright reportAttributeAccessIssue rose from 120 to 121")

    def test_a_rule_absent_from_the_file_counts_as_zero_so_a_new_one_is_refused(self):
        self.refuses(*self.measure(rules={"reportCallIssue": 1}), needle="pyright reportCallIssue rose from 0 to 1")

    def test_a_new_failing_id_is_refused(self):
        self.refuses(*self.measure(pytest=(1, ["tests/test_a.py::test_x", "tests/test_z.py::test_new"])),
                     needle="tests/test_z.py::test_new fails and is not in tests.failing")

    def test_a_coverage_below_the_floor_is_refused(self):
        self.refuses(*self.measure(percent=40.99), needle="coverage 40.99% is below the baseline floor 41")

    def test_one_figure_falling_never_pays_for_another_rising(self):
        """A mixed measurement is refused whole: the fixed rule is not written while another rule rose."""
        self.refuses(*self.measure(rules={"reportAttributeAccessIssue": 1, "reportArgumentType": 32}),
                     needle="pyright reportArgumentType rose from 31 to 32")

    def test_it_lowers_every_figure_drops_what_is_fixed_and_keeps_the_dates(self):
        code, out = self.write(*self.measure(rules={"reportAttributeAccessIssue": 100},
                                             pytest=(1, ["tests/test_a.py::test_x"]), percent=57.9))
        self.assertEqual(code, 0, out)
        written = json.loads(self.path.read_text())
        self.assertEqual(written["types"]["pyright"], {"reportAttributeAccessIssue": 100},
                         "reportArgumentType fell to 0 and is dropped")
        self.assertEqual(written["tests"]["failing"], ["tests/test_a.py::test_x"], "the fixed id is dropped")
        self.assertEqual(written["coverage"]["floor"], 57, "whole points, floored: 57.9 holds 57")
        self.assertEqual((written["version"], written["set"], written["expires"]), (1, GOOD["set"], GOOD["expires"]))

    def test_what_it_writes_is_canonical_valid_and_passes_compare_against_the_old_file(self):
        old = self.dir / "old.json"
        old.write_text(self.path.read_text())
        self.assertEqual(self.write(*self.measure(rules={"reportAttributeAccessIssue": 3}, percent=60))[0], 0)
        self.assertEqual(self.path.read_text(), debt.dumps(json.loads(self.path.read_text())))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(debt.main(["--today", TODAY, "compare", str(old), str(self.path)]), 0, out.getvalue())
            self.assertEqual(debt.main(["--file", str(self.path), "--today", TODAY, "check"]), 0, out.getvalue())

    def test_a_measurement_equal_to_the_file_changes_nothing(self):
        before = self.path.read_bytes()
        code, out = self.write(*self.measure(rules={"reportAttributeAccessIssue": 120, "reportArgumentType": 31},
                                             pytest=(1, GOOD["tests"]["failing"]), percent=41.5))
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to write", out)
        self.assertEqual(self.path.read_bytes(), before)

    def test_it_never_adds_a_section_the_file_does_not_hold(self):
        """An absent section is the standard for that gate: writing one would loosen it, not tighten it."""
        self.path.write_text(json.dumps({"version": 1, "set": GOOD["set"], "expires": GOOD["expires"],
                                         "coverage": {"floor": 41}}))
        code, out = self.write(*self.measure(rules={"reportCallIssue": 5}, pytest=(1, ["tests/t.py::a"]), percent=70))
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(self.path.read_text()),
                         {"version": 1, "set": GOOD["set"], "expires": GOOD["expires"], "coverage": {"floor": 70}})

    def test_a_coverage_floor_is_capped_at_100(self):
        self.assertEqual(self.write(*self.measure(percent=100.0))[0], 0)
        self.assertEqual(json.loads(self.path.read_text())["coverage"]["floor"], 100)

    def test_a_pytest_run_that_did_not_finish_is_not_a_measurement(self):
        self.refuses(*self.measure(pytest=(2, [])), needle="pytest exited 2 and was interrupted")
        self.refuses(*self.measure(pytest=(1, [])), needle="no FAILED or ERROR line")

    def test_pytest_exit_0_means_every_baselined_id_is_fixed(self):
        self.assertEqual(self.write(*self.measure(pytest=(0, [])))[0], 0)
        self.assertEqual(json.loads(self.path.read_text())["tests"], {"failing": []})

    def test_a_pyright_report_that_is_not_json_is_refused(self):
        bad = self.dir / "bad.json"
        bad.write_text("not json")
        self.refuses("--pyright", str(bad), needle="not pyright's JSON output")

    def test_a_missing_coverage_report_is_refused_not_read_as_zero(self):
        self.refuses("--coverage", str(self.dir / "absent.json"), needle="holds no coverage figure")

    def test_an_invalid_or_missing_baseline_is_refused(self):
        self.path.write_text('{"version": 2}')
        self.assertEqual(self.write(*self.measure(percent=90))[0], 1)
        self.path.unlink()
        code, out = self.write(*self.measure(percent=90))
        self.assertEqual(code, 1)
        self.assertIn("is missing", out)


if __name__ == "__main__":
    unittest.main()
