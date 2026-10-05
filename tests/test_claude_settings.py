# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The one Claude permission policy both shared jobs run under.

Both run on the Thor production host. What these tests hold: Bash is an
allowlist in both profiles (never a bare `Bash`), the escape hatches are
denied so no extra-allow can reopen them, the review cannot write, and nothing
outside the checkout is granted.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from steps import ROOT, Runner, bare, heredoc

ACTION = ROOT / ".github/actions/claude-settings/action.yml"
STEP = "Compose the Claude settings"
ESCAPE_HATCHES = ["Bash(curl:*)", "Bash(env:*)", "Bash(printenv:*)", "Bash(gh api:*)", "Bash(bash -c:*)",
                  "Bash(python3 -c:*)", "Bash(uv run python:*)", "Bash(git push --force:*)", "Bash(gh secret:*)"]


class Compose(unittest.TestCase):
    def compose(self, profile, allow="", deny=""):
        r = Runner(self)
        out = r.tmp / "settings.json"
        p = subprocess.run([sys.executable, str(heredoc(ACTION, STEP)), str(out)],
                           env=bare(PROFILE=profile, EXTRA_ALLOW=allow, EXTRA_DENY=deny),
                           capture_output=True, text=True)
        return p, json.loads(out.read_text()) if out.exists() else None

    def settings(self, profile, **kw):
        p, s = self.compose(profile, **kw)
        self.assertEqual(p.returncode, 0, p.stderr)
        return s


class BothProfiles(Compose):
    def test_bash_is_an_allowlist_never_a_bare_bash(self):
        for profile in ("review", "respond"):
            allow = self.settings(profile)["permissions"]["allow"]
            bash = [r for r in allow if r.startswith("Bash")]
            self.assertNotIn("Bash", allow)
            self.assertTrue(all(r == "Bash(pwd)" or (r.startswith("Bash(") and r.endswith(":*)")) for r in bash), bash)
            self.assertFalse([r for r in bash if r in ("Bash(*)", "Bash(:*)")])

    def test_the_escape_hatches_are_denied_so_an_extra_allow_cannot_reopen_them(self):
        for profile in ("review", "respond"):
            s = self.settings(profile, allow="Bash(curl:*)")
            for rule in ESCAPE_HATCHES:
                self.assertIn(rule, s["permissions"]["deny"], f"{profile}: {rule}")

    def test_nothing_outside_the_checkout_and_no_checks_api(self):
        # ci-results/ is staged inside the checkout; a grant would expose the
        # rest of $RUNNER_TEMP, credentials included. `gh pr checks` can only
        # fail without `checks: read` (AIRIA-process PR #74).
        for profile in ("review", "respond"):
            s = self.settings(profile)
            self.assertNotIn("additionalDirectories", s["permissions"])
            self.assertNotIn("Bash(gh pr checks:*)", s["permissions"]["allow"])

    def test_the_gate_runners_are_allowed_with_and_without_sync(self):
        allow = self.settings("review")["permissions"]["allow"]
        for tool in ("pytest", "ruff", "pyright", "mypy", "lint-imports", "reuse"):
            self.assertIn(f"Bash(uv run {tool}:*)", allow)
            self.assertIn(f"Bash(uv run --no-sync {tool}:*)", allow)

    def test_headless_runs_get_no_background_work_and_long_timeouts(self):
        s = self.settings("review")
        self.assertEqual(s["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")
        self.assertEqual(s["env"]["BASH_MAX_TIMEOUT_MS"], "1500000")
        self.assertEqual(s["sandbox"], {"enabled": False})


class ReviewProfile(Compose):
    def test_the_review_cannot_write_commit_push_or_comment(self):
        s = self.settings("review")
        for tool in ("Write", "Edit", "Bash(git commit:*)", "Bash(git push:*)"):
            self.assertNotIn(tool, s["permissions"]["allow"])
        for tool in ("Write", "Edit", "NotebookEdit", "Agent", "Bash(git push:*)", "Bash(gh pr comment:*)"):
            self.assertIn(tool, s["permissions"]["deny"])


class RespondProfile(Compose):
    def test_respond_can_edit_and_commit_but_never_force_push(self):
        s = self.settings("respond")
        for tool in ("Write", "Edit", "Bash(git commit:*)", "Bash(git push:*)", "Bash(git add:*)"):
            self.assertIn(tool, s["permissions"]["allow"])
        self.assertNotIn("Write", s["permissions"]["deny"])
        self.assertIn("Bash(git push --force:*)", s["permissions"]["deny"])
        self.assertIn("Bash(gh pr merge:*)", s["permissions"]["deny"])


class Extras(Compose):
    def test_extras_are_appended_one_per_line_blank_lines_dropped(self):
        s = self.settings("review", allow="\n  Bash(cargo test:*)  \n\n", deny="Bash(my-money-cli:*)\n")
        self.assertEqual(s["permissions"]["allow"][-1], "Bash(cargo test:*)")
        self.assertEqual(s["permissions"]["deny"][-1], "Bash(my-money-cli:*)")

    def test_extra_allow_may_not_grant_a_bare_bash(self):
        # A `*` matches any text anywhere in a rule, so a rule with no fixed
        # command before its first `*` runs any program: `Bash(* --version)`
        # matches `bash -c '...' --version` (Claude Code's permissions docs).
        for spelling in ("Bash", "Bash(*)", "Bash(:*)", "Bash(**)", "Bash( * )", "Bash(* --version)", "Bash(*:*)"):
            p, s = self.compose("review", allow=f"Bash(cargo test:*)\n{spelling}")
            self.assertNotEqual(p.returncode, 0, spelling)
            self.assertIn("may not grant a bare Bash", p.stderr)
            self.assertIsNone(s, "no settings file may be written")

    def test_a_gate_rule_with_a_fixed_command_is_still_allowed(self):
        rules = ["Bash(cargo test:*)", "Bash(npm run *)", "Bash(make lint)", "Bash(pytest*)"]
        s = self.settings("review", allow="\n".join(rules))
        self.assertEqual(s["permissions"]["allow"][-len(rules):], rules)

    def test_an_unknown_profile_is_refused(self):
        p, s = self.compose("admin")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("profile must be review or respond, not 'admin'", p.stderr)
        self.assertIsNone(s)


class TheStep(unittest.TestCase):
    def test_the_path_output_names_the_file_it_wrote(self):
        r = Runner(self)
        p = r.run(ACTION, STEP, PROFILE="review", EXTRA_ALLOW="Bash(cargo test:*)", EXTRA_DENY="")
        self.assertEqual(p.returncode, 0, p.stderr)
        path = r.outputs()["path"]
        self.assertEqual(path, str(r.tmp / "claude-settings-review.json"))
        self.assertIn("Bash(cargo test:*)", json.loads(Path(path).read_text())["permissions"]["allow"])
        self.assertIn("+ Bash(cargo test:*)", p.stdout)

    def test_a_refused_policy_fails_the_step_and_names_no_path(self):
        r = Runner(self)
        p = r.run(ACTION, STEP, PROFILE="review", EXTRA_ALLOW="Bash", EXTRA_DENY="")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(r.outputs(), {})


if __name__ == "__main__":
    unittest.main()
