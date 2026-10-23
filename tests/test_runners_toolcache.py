# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""runners/set-toolcache.sh and runners/toolcache-python.sh: one tool cache per runner (#35).

What these tests hold: two runners never share a tool cache directory (a shared one lets two jobs
write the same uv binary, which fails the other job with "Text file busy"); re-running changes
nothing and says so; other .env lines survive; and the Python seed reaches every runner's cache.
`uv` and `python3` are fakes, the scripts are the shipped files, directories are scratch ones.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import unittest

from steps import ROOT, Runner

SET = ROOT / "runners/set-toolcache.sh"
PYTHON = ROOT / "runners/toolcache-python.sh"
REGISTER = ROOT / "runners/register.sh"


class Base(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        self.home = self.r.tmp / "home"
        self.cache = self.r.tmp / "tools"
        self.home.mkdir()
        self.cache.mkdir()

    def runner_dir(self, name, env=None, registered=True):
        d = self.home / name
        d.mkdir()
        if registered:
            (d / ".runner").write_text("{}")
        if env is not None:
            (d / ".env").write_text(env)
        return d

    def set_toolcache(self, *args):
        return self.r.script(SET, *args, RUNNER_HOME=str(self.home), TOOLCACHE_ROOT=str(self.cache))


class SetToolcache(Base):
    def test_each_runner_gets_its_own_cache_directory(self):
        a, b = self.runner_dir("r-o-r-1"), self.runner_dir("r-o-r-2")
        p = self.set_toolcache()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((a / ".env").read_text(), f"AGENT_TOOLSDIRECTORY={self.cache}/r-o-r-1\n")
        self.assertEqual((b / ".env").read_text(), f"AGENT_TOOLSDIRECTORY={self.cache}/r-o-r-2\n")
        self.assertTrue((self.cache / "r-o-r-1").is_dir() and (self.cache / "r-o-r-2").is_dir())
        self.assertEqual(p.stdout.count("restart needed"), 2)

    def test_a_shared_setting_is_replaced_and_other_lines_stay(self):
        a = self.runner_dir("org-orlando-2", "LANG=en_US.UTF-8\nAGENT_TOOLSDIRECTORY=/opt/hostedtoolcache\n")
        self.assertEqual(self.set_toolcache().returncode, 0)
        self.assertEqual(
            (a / ".env").read_text(), f"LANG=en_US.UTF-8\nAGENT_TOOLSDIRECTORY={self.cache}/org-orlando-2\n"
        )

    def test_a_second_run_changes_nothing_and_says_so(self):
        a = self.runner_dir("r-o-r-1")
        self.set_toolcache()
        before = (a / ".env").read_text()
        p = self.set_toolcache()
        self.assertEqual((a / ".env").read_text(), before)
        self.assertIn("unchanged", p.stdout)
        self.assertNotIn("restart needed", p.stdout)
        self.assertFalse((a / ".env.new").exists())

    def test_a_directory_without_a_runner_is_left_alone(self):
        stray = self.runner_dir("not-a-runner", registered=False)
        self.set_toolcache()
        self.assertFalse((stray / ".env").exists())

    def test_a_named_directory_that_is_missing_fails(self):
        p = self.set_toolcache(str(self.home / "nope"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no such runner directory", p.stderr)


FAKE_PYTHON = """#!/usr/bin/env bash
case "$1" in -c) echo 3.12.5 ;; --version) echo "Python 3.12.5" ;; esac
"""


class ToolcachePython(Base):
    def setUp(self):
        super().setUp()
        src = self.r.tmp / "uvpy" / "bin"
        src.mkdir(parents=True)
        py = src / "python3"
        py.write_text(FAKE_PYTHON)
        py.chmod(0o755)
        self.r.tool("uv", f'#!/usr/bin/env bash\n[ "$2" = find ] && echo {py}\nexit 0\n')

    def seed(self):
        return self.r.script(
            PYTHON,
            RUNNER_HOME=str(self.home),
            UV=str(self.r.bin / "uv"),
            UV_PYTHON_INSTALL_DIR=str(self.r.tmp / "uv-python"),
        )

    def test_every_runners_cache_gets_the_interpreter(self):
        a = self.runner_dir("r-o-r-1", f"AGENT_TOOLSDIRECTORY={self.cache}/r-o-r-1\n")
        b = self.runner_dir("r-o-r-2", f"AGENT_TOOLSDIRECTORY={self.cache}/r-o-r-2\n")
        p = self.seed()
        self.assertEqual(p.returncode, 0, p.stderr)
        for d in (a, b):
            base = self.cache / d.name / "Python/3.12.5"
            self.assertTrue((base / "x64/bin/python3").exists())
            self.assertTrue((base / "x64.complete").exists())
        self.assertEqual(p.stdout.count("installed 3.12.5"), 2)

    def test_runners_sharing_a_cache_seed_it_once(self):
        self.runner_dir("r-o-r-1", f"AGENT_TOOLSDIRECTORY={self.cache}/shared\n")
        self.runner_dir("r-o-r-2", f"AGENT_TOOLSDIRECTORY={self.cache}/shared\n")
        p = self.seed()
        self.assertEqual(p.stdout.count("installed 3.12.5"), 1)

    def test_a_second_run_finds_it_present(self):
        self.runner_dir("r-o-r-1", f"AGENT_TOOLSDIRECTORY={self.cache}/r-o-r-1\n")
        self.seed()
        p = self.seed()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("already present", p.stdout)

    def test_no_runner_with_a_tool_cache_fails_with_the_fix(self):
        self.runner_dir("r-o-r-1", "LANG=C\n")
        p = self.seed()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("run set-toolcache.sh first", p.stderr)


class RegisterPushesTheScript(unittest.TestCase):
    def test_set_toolcache_goes_into_the_container_with_add_runner(self):
        r = Runner(self, [["registration-token", {"text": "tok\n"}], ["actions/runners --jq", {"text": "x\n"}]])
        log = r.tmp / "calls.log"
        r.tool("scp", f'#!/usr/bin/env bash\necho "scp $*" >> {log}\n')
        # only the call that gets the token piped in reads stdin (as in test_runners_register.FAKE_SSH)
        r.tool(
            "ssh",
            f'#!/usr/bin/env bash\necho "ssh $*" >> {log}\ncase "$*" in *"-- /root/add-runner.sh \'"*) cat >/dev/null ;; esac\n',
        )
        conf = r.tmp / "runners.env"
        conf.write_text("THOR_SSH=root@thor.invalid\nTHOR_CT=100\n")
        p = r.script(REGISTER, "o/r", "1", AIRIA_CI_RUNNERS_ENV=str(conf))
        self.assertEqual(p.returncode, 0, p.stderr)
        calls = log.read_text()
        self.assertIn("runners/add-runner.sh", calls)
        self.assertIn("runners/set-toolcache.sh", calls)
        self.assertIn("pct push '100' /tmp/set-toolcache.sh /root/set-toolcache.sh", calls)


if __name__ == "__main__":
    unittest.main()
