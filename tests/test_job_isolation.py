# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The job-isolation action's shipped shell: a job's `git config --global` must not touch the shared HOME.

Persistent runners share one HOME, so two jobs writing ~/.gitconfig race for its
lock (AIRIA-process #159). The action's step is run as GitHub runs it, then a
later step is simulated by reading GITHUB_ENV into a git call's environment.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import subprocess
import unittest

from steps import ROOT, Runner

ACTION = ROOT / ".github/actions/job-isolation/action.yml"
STEP = "Give the job a private git global config"


class GitConfig(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        self.home = self.r.tmp / "home"
        self.home.mkdir()

    def isolate(self):
        done = self.r.run(ACTION, STEP, HOME=str(self.home))
        self.assertEqual(done.returncode, 0, done.stderr)
        return dict(line.split("=", 1) for line in self.r.github_env.read_text().splitlines())

    def git(self, exported, *args):
        return subprocess.run(["git", *args], env={**self.r.env(HOME=str(self.home)), **exported},
                              capture_output=True, text=True)

    def test_a_later_step_is_pointed_at_a_file_under_runner_temp(self):
        exported = self.isolate()
        self.assertEqual(exported, {"GIT_CONFIG_GLOBAL": str(self.r.tmp / "gitconfig")})

    def test_a_global_write_lands_in_the_private_file_and_not_in_home(self):
        exported = self.isolate()
        done = self.git(exported, "config", "--global", "user.email", "ci@airia.invalid")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ci@airia.invalid", (self.r.tmp / "gitconfig").read_text())
        self.assertFalse((self.home / ".gitconfig").exists())

    def test_a_lock_another_job_holds_on_the_shared_home_does_not_block_the_write(self):
        (self.home / ".gitconfig.lock").write_text("")
        without = self.git({}, "config", "--global", "user.name", "x")
        self.assertNotEqual(without.returncode, 0)
        self.assertIn("could not lock config file", without.stderr)
        done = self.git(self.isolate(), "config", "--global", "user.name", "x")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_private_file_starts_empty_whatever_an_earlier_job_left(self):
        (self.r.tmp / "gitconfig").write_text("[user]\n\tname = leftover\n")
        exported = self.isolate()
        self.assertEqual(self.git(exported, "config", "--global", "--list").stdout, "")


if __name__ == "__main__":
    unittest.main()
