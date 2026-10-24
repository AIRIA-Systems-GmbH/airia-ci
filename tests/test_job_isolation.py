# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The job-isolation action's shipped shell: a job's `git config --global` must not touch the shared HOME.

Persistent runners share one HOME, so two jobs writing ~/.gitconfig race for its
lock (AIRIA-process #159). The action's step is run as GitHub runs it, then a
later step is simulated by reading GITHUB_ENV into a git call's environment.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import re
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


ANSIBLE = "Give the job a private Ansible home"
PREFIX = "Give the job a Docker name prefix"
# Docker container/network/volume names and compose project names: lowercase, [a-z0-9_-], letter first.
DOCKER_NAME = re.compile(r"[a-z][a-z0-9_-]*")


class AnsibleHome(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)

    def isolate(self):
        done = self.r.run(ACTION, ANSIBLE)
        self.assertEqual(done.returncode, 0, done.stderr)
        return dict(line.split("=", 1) for line in self.r.github_env.read_text().splitlines())

    def test_ansible_home_and_local_temp_are_exported_below_runner_temp(self):
        home = str(self.r.tmp / "ansible")
        self.assertEqual(self.isolate(), {"ANSIBLE_HOME": home, "ANSIBLE_LOCAL_TEMP": home + "/tmp"})

    def test_the_directories_exist(self):
        self.isolate()
        self.assertTrue((self.r.tmp / "ansible/tmp").is_dir())

    def test_they_start_empty_whatever_an_earlier_job_left(self):
        (self.r.tmp / "ansible/tmp").mkdir(parents=True)
        (self.r.tmp / "ansible/collections").write_text("leftover")
        (self.r.tmp / "ansible/tmp/x").write_text("leftover")
        self.isolate()
        self.assertEqual(list((self.r.tmp / "ansible").rglob("*")), [self.r.tmp / "ansible/tmp"])


class DockerPrefix(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)

    def prefix(self, job="build", run="123", attempt="1", runner="orlando-org-1"):
        self.r.github_env.write_text("")
        done = self.r.run(ACTION, PREFIX, GITHUB_JOB=job, GITHUB_RUN_ID=run, GITHUB_RUN_ATTEMPT=attempt,
                          RUNNER_NAME=runner)
        self.assertEqual(done.returncode, 0, done.stderr)
        name, value = self.r.github_env.read_text().strip().split("=", 1)
        self.assertEqual(name, "CI_JOB_PREFIX")
        return value

    def test_it_carries_run_attempt_and_job(self):
        self.assertTrue(self.prefix(job="build", run="123", attempt="2").startswith("ci-123-2-build-"))

    def test_it_is_a_valid_docker_name_for_awkward_jobs(self):
        for job in ("Build Image", "unit.tests (3.12)", "1-first", "ÄÖ", "a/b:c", "x" * 200, "UPPER_case-1", ""):
            with self.subTest(job=job):
                value = self.prefix(job=job)
                self.assertRegex(value, DOCKER_NAME.pattern + r"\Z")
                self.assertLessEqual(len(value), 64)

    def test_a_runner_name_with_dots_and_spaces_is_still_valid(self):
        self.assertRegex(self.prefix(runner="My Runner.01 / x"), DOCKER_NAME.pattern + r"\Z")

    def test_two_attempts_of_one_job_differ(self):
        self.assertNotEqual(self.prefix(attempt="1"), self.prefix(attempt="2"))

    def test_two_runs_differ(self):
        self.assertNotEqual(self.prefix(run="1"), self.prefix(run="2"))

    def test_matrix_legs_on_different_runners_differ(self):
        self.assertNotEqual(self.prefix(runner="orlando-org-1"), self.prefix(runner="orlando-org-2"))

    def test_it_is_stable_for_the_same_job_on_the_same_runner(self):
        self.assertEqual(self.prefix(), self.prefix())


if __name__ == "__main__":
    unittest.main()
