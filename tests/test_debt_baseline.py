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
import tempfile
import unittest
from pathlib import Path

from steps import ROOT

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


if __name__ == "__main__":
    unittest.main()
