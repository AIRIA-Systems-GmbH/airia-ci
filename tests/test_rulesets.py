# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""scripts/rulesets.py against a fake `gh`.

What these tests hold (Andrea's ruling 16, 2026-11-19: exceptions only for very
good reasons, so nothing implicit): a repository equal to the baseline passes; a
departure nobody listed fails the run; a listed exception passes; an exception
without a reason is refused; the check only ever reads.

Run: python3 -m unittest discover -s tests
"""

import contextlib
import copy
import importlib.util
import io
import json
import os
import unittest
from unittest import mock

from steps import ROOT, Runner

spec = importlib.util.spec_from_file_location("rulesets", ROOT / "scripts/rulesets.py")
rulesets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rulesets)

BASELINE = json.loads((ROOT / "rulesets/baseline.json").read_text())


def live(checks=("gate",), **changes):
    """A repository's live main ruleset: the baseline with these checks, then `changes` applied."""
    body = copy.deepcopy(BASELINE)
    body["id"] = 1
    for rule in body["rules"]:
        if rule["type"] == "required_status_checks":
            rule["parameters"]["required_status_checks"] = [{"context": c} for c in checks]
    body.update(changes)
    return body


def toml(*exceptions, checks='["gate"]', aggregate="gate"):
    return f'aggregate_check = "{aggregate}"\n[repos."o/r"]\nrulesets = [1]\nrequired_checks = {checks}\n' + "".join(exceptions)


def exception(**fields):
    lines = "".join(f"{k} = {json.dumps(v)}\n" for k, v in fields.items())
    return f"[[repos.\"o/r\".exceptions]]\n{lines}"


class Check(unittest.TestCase):
    def run_main(self, text, live_body):
        r = Runner(self, [
            ["repos/o/r/rulesets/1", {"json": live_body}],
            ["repos/o/r/rulesets$", {"json": [{"id": 1}]}],
        ])
        return self.finish(r, text)

    def finish(self, r, text):
        path = r.tmp / "exceptions.toml"
        path.write_text(text)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, r.env()), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rulesets.main(["--exceptions", str(path)])
        return code, out.getvalue(), err.getvalue(), r


class Baseline(Check):
    def test_a_repository_equal_to_the_baseline_passes(self):
        code, out, _, _ = self.run_main(toml(), live())
        self.assertEqual(code, 0)
        self.assertIn("o/r\tok\t0 exceptions", out)

    def test_the_ruleset_payload_of_a_user_repository_without_dismissal_restriction_is_no_departure(self):
        # Portfolio-Dashboard and CryptoStudio return the pull_request parameters without it.
        code, _, _, _ = self.run_main(toml(), live())
        self.assertEqual(code, 0)
        body = live()
        for rule in body["rules"]:
            if rule["type"] == "pull_request":
                rule["parameters"]["dismissal_restriction"] = {"allowed_actors": [], "enabled": False}
        self.assertEqual(self.run_main(toml(), body)[0], 0)


class UnlistedDeparture(Check):
    def test_a_missing_rule_fails_the_run_and_names_the_rule(self):
        body = live()
        body["rules"] = [r for r in body["rules"] if r["type"] != "required_signatures"]
        code, out, _, _ = self.run_main(toml(), body)
        self.assertEqual(code, 1)
        self.assertIn("o/r\tDRIFT", out)
        self.assertIn("rule required_signatures is missing", out)

    def test_a_weaker_pull_request_parameter_fails_the_run(self):
        body = live()
        for rule in body["rules"]:
            if rule["type"] == "pull_request":
                rule["parameters"]["required_review_thread_resolution"] = False
        code, out, _, _ = self.run_main(toml(), body)
        self.assertEqual(code, 1)
        self.assertIn("rule pull_request parameters", out)

    def test_a_bypass_actor_a_changed_ref_or_an_inactive_ruleset_each_fail(self):
        for change, text in (({"bypass_actors": [{"actor_id": 5}]}, "bypass_actors"),
                             ({"enforcement": "evaluate"}, "enforcement"),
                             ({"conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}}}, "conditions")):
            with self.subTest(text):
                code, out, _, _ = self.run_main(toml(), live(**change))
                self.assertEqual(code, 1)
                self.assertIn(text, out)

    def test_a_check_nobody_listed_fails_the_run(self):
        code, out, _, _ = self.run_main(toml(exception(kind="required_checks", reason="own jobs"),
                                             checks='["a"]'), live(checks=("a", "b")))
        self.assertEqual(code, 1)
        self.assertIn("rule required_status_checks parameters", out)

    def test_strict_checks_turned_on_fail_the_run(self):
        body = live()
        for rule in body["rules"]:
            if rule["type"] == "required_status_checks":
                rule["parameters"]["strict_required_status_checks_policy"] = True
        self.assertEqual(self.run_main(toml(), body)[0], 1)

    def test_a_rule_the_baseline_does_not_have_fails_the_run(self):
        body = live()
        body["rules"].append({"type": "update"})
        code, out, _, _ = self.run_main(toml(), body)
        self.assertEqual(code, 1)
        self.assertIn("rule update is live and not in the baseline", out)

    def test_checks_that_are_not_the_aggregate_need_an_exception(self):
        code, out, _, _ = self.run_main(toml(checks='["own"]'), live(checks=("own",)))
        self.assertEqual(code, 1)
        self.assertIn("no `required_checks` exception says why", out)

    def test_no_aggregate_yet_means_every_repository_needs_the_exception(self):
        code, out, _, _ = self.run_main(toml(aggregate=""), live())
        self.assertEqual(code, 1)
        self.assertIn("no `required_checks` exception says why", out)

    def test_an_exception_that_no_longer_matches_is_drift(self):
        # The PR rule is still live, but its omission is listed: the listing is stale.
        text = toml(exception(kind="omit_rule", rule="pull_request", reason="direct writers"))
        code, out, _, _ = self.run_main(text, live())
        self.assertEqual(code, 1)
        self.assertIn("rule pull_request is live and not in the baseline", out)

    def test_an_unlisted_ruleset_and_a_listed_one_that_is_gone_fail_the_run(self):
        r = Runner(self, [
            ["repos/o/r/rulesets/1", {"json": live()}],
            ["repos/o/r/rulesets$", {"json": [{"id": 1}, {"id": 9}]}],
        ])
        code, out, _, _ = self.finish(r, toml())
        self.assertEqual(code, 1)
        self.assertIn("ruleset 9 exists and is not listed", out)
        r = Runner(self, [["repos/o/r/rulesets$", {"json": []}]])
        code, out, _, _ = self.finish(r, toml())
        self.assertEqual(code, 1)
        self.assertIn("ruleset 1 is listed and does not exist", out)


class ListedException(Check):
    def test_a_listed_omitted_rule_ref_and_check_list_pass(self):
        body = live(checks=("own",), conditions={"ref_name": {"include": ["refs/heads/main"], "exclude": []}})
        body["rules"] = [r for r in body["rules"] if r["type"] != "required_signatures"]
        text = toml(exception(kind="required_checks", reason="own jobs"),
                    exception(kind="omit_rule", rule="required_signatures", reason="direct writers"),
                    exception(kind="ref_include", include=["refs/heads/main"], reason="same branch"),
                    checks='["own"]', aggregate="")
        code, out, _, _ = self.run_main(text, body)
        self.assertEqual(code, 0, out)
        self.assertIn("3 exceptions", out)

    def test_no_checks_means_no_status_check_rule(self):
        body = live(checks=())
        body["rules"] = [r for r in body["rules"] if r["type"] != "required_status_checks"]
        text = toml(exception(kind="required_checks", reason="no CI yet"), checks="[]")
        self.assertEqual(self.run_main(text, body)[0], 0)

    def test_an_addition_passes_while_active_and_fails_when_not(self):
        text = toml(exception(kind="addition", ruleset=2, reason="release tags"))
        for enforcement, code in (("active", 0), ("disabled", 1)):
            with self.subTest(enforcement):
                r = Runner(self, [
                    ["repos/o/r/rulesets/1", {"json": live()}],
                    ["repos/o/r/rulesets/2", {"json": {"id": 2, "enforcement": enforcement}}],
                    ["repos/o/r/rulesets$", {"json": [{"id": 1}, {"id": 2}]}],
                ])
                self.assertEqual(self.finish(r, text)[0], code)


class Refusals(Check):
    def test_an_exception_without_a_reason_is_refused_and_nothing_is_read(self):
        for reason in ({}, {"reason": "  "}):
            with self.subTest(reason):
                text = toml(exception(kind="omit_rule", rule="pull_request", **reason))
                code, out, err, r = self.run_main(text, live())
                self.assertEqual(code, 2)
                self.assertIn("omit_rule exception has no reason", err)
                self.assertEqual(r.calls(), [])
                self.assertEqual(out, "")

    def test_an_exception_of_an_unknown_kind_is_refused(self):
        code, _, err, _ = self.run_main(toml(exception(kind="whatever", reason="x")), live())
        self.assertEqual(code, 2)
        self.assertIn("unknown kind 'whatever'", err)

    def test_a_repository_without_its_ruleset_ids_or_checks_is_refused(self):
        r = Runner(self)
        code, _, err, _ = self.finish(r, '[repos."o/r"]\nrequired_checks = []\n')
        self.assertEqual(code, 2)
        self.assertIn("no `rulesets` listed", err)

    def test_an_unreadable_ruleset_is_an_error_not_a_pass(self):
        r = Runner(self, [["repos/o/r/rulesets", {"exit": 1, "stderr": "HTTP 403"}]])
        code, _, err, _ = self.finish(r, toml())
        self.assertEqual(code, 2)
        self.assertIn("cannot read repos/o/r/rulesets: HTTP 403", err)


class ReadOnly(Check):
    def test_every_gh_call_is_a_plain_get(self):
        _, _, _, r = self.run_main(toml(), live())
        calls = r.calls()
        self.assertTrue(calls)
        for c in calls:
            self.assertEqual(c["args"][0], "api")
            self.assertEqual(len(c["args"]), 2, c["args"])  # no -X, --method, -f, -F, --input


class Shipped(unittest.TestCase):
    def test_the_shipped_exceptions_are_complete_and_every_one_has_a_reason(self):
        data = rulesets.load_exceptions(ROOT / "rulesets/exceptions.toml")
        self.assertEqual(len(data["repos"]), 11)
        for entry in data["repos"].values():
            self.assertTrue(entry["rulesets"])
            self.assertTrue(any(e["kind"] == "required_checks" for e in entry["exceptions"]))


if __name__ == "__main__":
    unittest.main()
