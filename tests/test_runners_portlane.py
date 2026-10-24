# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""runners/set-portlane.sh: one port lane per runner, set in its .env (#53).

What these tests hold: a runner's lane is 10000+1000*i .. 10999+1000*i for the index it is given;
no lane reaches the ephemeral range (32768) or passes 21999; two runners never hold the same lane
(a shared lane lets two concurrent jobs bind the same port); re-running changes nothing and says
so; other .env lines survive. `id` and `chown` are fakes, the script is the shipped file, the
runner directories are scratch ones.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import subprocess
import unittest

from steps import ROOT, Runner

SET = ROOT / "runners/set-portlane.sh"
REGISTER = ROOT / "runners/register.sh"


class SetPortlane(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        self.home = self.r.tmp / "home"
        self.home.mkdir()
        self.chowns = self.r.tmp / "chown.log"
        self.r.tool("id", '#!/usr/bin/env bash\n[ "$1" = -u ] && { echo 0; exit 0; }\nexec /usr/bin/id "$@"\n')
        self.r.tool("chown", f'#!/usr/bin/env bash\necho "$*" >> {self.chowns}\n')

    def runner_dir(self, name, env=None):
        d = self.home / name
        d.mkdir()
        if env is not None:
            (d / ".env").write_text(env)
        return d

    def test_the_lane_follows_the_index(self):
        a, b = self.runner_dir("r-o-r-1"), self.runner_dir("r-o-r-2")
        p = self.r.script(SET, str(a), "0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.r.script(SET, str(b), "3").returncode, 0)
        self.assertEqual((a / ".env").read_text(), "CI_PORT_LANE_START=10000\nCI_PORT_LANE_END=10999\n")
        self.assertEqual((b / ".env").read_text(), "CI_PORT_LANE_START=13000\nCI_PORT_LANE_END=13999\n")
        self.assertIn("restart needed", p.stdout)

    def test_the_last_lane_ends_below_the_ephemeral_range(self):
        a = self.runner_dir("r-o-r-1")
        self.assertEqual(self.r.script(SET, str(a), "11").returncode, 0)
        self.assertEqual((a / ".env").read_text(), "CI_PORT_LANE_START=21000\nCI_PORT_LANE_END=21999\n")

    def test_an_index_of_twelve_or_more_is_refused_and_writes_nothing(self):
        a = self.runner_dir("r-o-r-1")
        for index in ("12", "99"):
            p = self.r.script(SET, str(a), index)
            self.assertEqual(p.returncode, 2)
            self.assertIn("out of range", p.stderr)
        self.assertFalse((a / ".env").exists())

    def test_a_non_numeric_index_is_refused(self):
        a = self.runner_dir("r-o-r-1")
        for index in ("x", "-1", "1.5", ""):
            p = self.r.script(SET, str(a), index)
            self.assertEqual(p.returncode, 2, index)
            self.assertIn("index must be a number", p.stderr)

    def test_a_zero_padded_index_is_read_as_decimal(self):
        a = self.runner_dir("r-o-r-1")
        self.assertEqual(self.r.script(SET, str(a), "08").returncode, 0)
        self.assertIn("CI_PORT_LANE_START=18000\n", (a / ".env").read_text())

    def test_two_runners_cannot_hold_the_same_lane(self):
        a, b = self.runner_dir("r-o-r-1"), self.runner_dir("r-o-r-2")
        self.assertEqual(self.r.script(SET, str(a), "2").returncode, 0)
        p = self.r.script(SET, str(b), "2")
        self.assertEqual(p.returncode, 1)
        self.assertIn("already held by", p.stderr)
        self.assertFalse((b / ".env").exists())

    def test_a_runner_can_be_given_its_own_lane_again(self):
        a = self.runner_dir("r-o-r-1")
        self.r.script(SET, str(a), "2")
        p = self.r.script(SET, str(a), "2")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_other_lines_stay_and_a_changed_lane_is_replaced(self):
        a = self.runner_dir("r-o-r-1", "LANG=C\nCI_PORT_LANE_START=10000\nCI_PORT_LANE_END=10999\nX=1\n")
        p = self.r.script(SET, str(a), "1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((a / ".env").read_text(), "LANG=C\nX=1\nCI_PORT_LANE_START=11000\nCI_PORT_LANE_END=11999\n")

    def test_a_second_run_changes_nothing_and_says_so(self):
        a = self.runner_dir("r-o-r-1")
        self.r.script(SET, str(a), "4")
        before = (a / ".env").read_text()
        p = self.r.script(SET, str(a), "4")
        self.assertEqual((a / ".env").read_text(), before)
        self.assertIn("unchanged", p.stdout)
        self.assertNotIn("restart needed", p.stdout)
        self.assertFalse((a / ".env.new").exists())

    def test_the_env_file_is_handed_to_the_runner_user(self):
        a = self.runner_dir("r-o-r-1")
        self.r.script(SET, str(a), "0")
        self.assertEqual(self.chowns.read_text().splitlines(), [f"runner:runner {a}/.env"])

    def test_a_missing_directory_fails(self):
        p = self.r.script(SET, str(self.home / "nope"), "0")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no such runner directory", p.stderr)

    def test_the_wrong_number_of_arguments_prints_the_usage(self):
        p = self.r.script(SET, str(self.home))
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage:", p.stderr)

    def test_a_non_root_run_leaves_ownership_alone(self):
        self.r.tool("id", "#!/usr/bin/env bash\necho 1000\n")
        a = self.runner_dir("r-o-r-1")
        self.assertEqual(self.r.script(SET, str(a), "0").returncode, 0)
        self.assertFalse(self.chowns.exists())


    def test_check_validates_without_writing_and_without_the_directory(self):
        held = self.runner_dir("r-o-r-1")
        self.r.script(SET, str(held), "2")
        before = (held / ".env").read_text()
        new = self.home / "r-o-r-2"  # not installed yet
        self.assertEqual(self.r.script(SET, "--check", str(new), "3").returncode, 0)
        self.assertFalse(new.exists())
        p = self.r.script(SET, "--check", str(new), "2")
        self.assertEqual(p.returncode, 1)
        self.assertIn("already held by", p.stderr)
        self.assertEqual(self.r.script(SET, "--check", str(new), "12").returncode, 2)
        self.assertEqual((held / ".env").read_text(), before)


ADD = ROOT / "runners/add-runner.sh"


class AddRunnerRefusesALaneBeforeRegistering(unittest.TestCase):
    """config.sh registers the runner on GitHub: a refused lane must come before it (F1 of #87)."""

    def setUp(self):
        self.r = Runner(self)
        self.home = self.r.tmp / "home"
        self.home.mkdir()
        self.registered = self.r.tmp / "config.log"
        stage = self.r.tmp / "stage"
        stage.mkdir()
        (stage / "config.sh").write_text(f'#!/usr/bin/env bash\necho "$*" >> {self.registered}\n')
        (stage / "svc.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        for f in ("config.sh", "svc.sh"):
            (stage / f).chmod(0o755)
        self.tarball = self.r.tmp / "runner.tar.gz"
        subprocess.run(["tar", "czf", str(self.tarball), "-C", str(stage), "config.sh", "svc.sh"], check=True)
        self.r.tool("sudo", '#!/usr/bin/env bash\nshift 2\nexec "$@"\n')
        self.r.tool("chown", "#!/usr/bin/env bash\nexit 0\n")
        self.r.tool("id", '#!/usr/bin/env bash\n[ "$1" = -u ] && { echo 1000; exit 0; }\nexec /usr/bin/id "$@"\n')

    def add(self, *args):
        p = subprocess.run(
            ["bash", str(ADD), "o/r", *args],
            input="tok\n",
            capture_output=True,
            text=True,
            env=self.r.env(
                RUNNER_HOME=str(self.home), RUNNER_TARBALL=str(self.tarball), TOOLCACHE_ROOT=str(self.r.tmp / "tools")
            ),
        )
        return p

    def test_a_lane_that_is_accepted_registers_and_sets_it(self):
        p = self.add("1", "", "4")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(self.registered.exists())
        self.assertIn("CI_PORT_LANE_START=14000", (self.home / "r-o-r-1/.env").read_text())

    def test_a_lane_past_eleven_registers_nothing(self):
        p = self.add("1", "", "12")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("out of range", p.stderr)
        self.assertFalse(self.registered.exists())
        self.assertFalse((self.home / "r-o-r-1").exists())

    def test_a_lane_another_runner_holds_registers_nothing(self):
        other = self.home / "r-x-y-1"
        other.mkdir()
        (other / ".env").write_text("CI_PORT_LANE_START=14000\nCI_PORT_LANE_END=14999\n")
        p = self.add("1", "", "4")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("already held by", p.stderr)
        self.assertFalse(self.registered.exists())
        self.assertFalse((self.home / "r-o-r-1").exists())


class RegisterRefusesLanesBeforeRegistering(unittest.TestCase):
    def run_register(self, *args):
        r = Runner(self, [["registration-token", {"text": "tok\n"}], ["actions/runners --jq", {"text": "x\n"}]])
        log = r.tmp / "calls.log"
        r.tool("scp", f'#!/usr/bin/env bash\necho "scp $*" >> {log}\n')
        r.tool("ssh", f'#!/usr/bin/env bash\necho "ssh $*" >> {log}\ncat >/dev/null\n')
        conf = r.tmp / "runners.env"
        conf.write_text("THOR_SSH=root@thor.invalid\nTHOR_CT=100\n")
        p = r.script(REGISTER, "o/r", *args, AIRIA_CI_RUNNERS_ENV=str(conf))
        return p, log

    def test_lanes_past_eleven_are_refused_before_anything_is_pushed(self):
        p, log = self.run_register("3", "10")
        self.assertEqual(p.returncode, 2)
        self.assertIn("past lane 11", p.stderr)
        self.assertFalse(log.exists())

    def test_the_last_lane_is_accepted(self):
        p, log = self.run_register("2", "10")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("'2' '' '11'", log.read_text())

    def test_a_non_numeric_first_lane_is_refused_before_anything_is_pushed(self):
        p, log = self.run_register("1", "x")
        self.assertEqual(p.returncode, 2)
        self.assertIn("must be numbers", p.stderr)
        self.assertFalse(log.exists())


class RegisterPassesTheLane(unittest.TestCase):
    def run_register(self, *args):
        r = Runner(self, [["registration-token", {"text": "tok\n"}], ["actions/runners --jq", {"text": "x\n"}]])
        log = r.tmp / "calls.log"
        r.tool("scp", f'#!/usr/bin/env bash\necho "scp $*" >> {log}\n')
        r.tool(
            "ssh",
            f'#!/usr/bin/env bash\necho "ssh $*" >> {log}\ncase "$*" in *"-- /root/add-runner.sh \'"*) cat >/dev/null ;; esac\n',
        )
        conf = r.tmp / "runners.env"
        conf.write_text("THOR_SSH=root@thor.invalid\nTHOR_CT=100\n")
        p = r.script(REGISTER, "o/r", *args, AIRIA_CI_RUNNERS_ENV=str(conf))
        self.assertEqual(p.returncode, 0, p.stderr)
        return log.read_text()

    def test_the_script_goes_into_the_container_and_each_runner_gets_the_next_lane(self):
        calls = self.run_register("2", "5")
        self.assertIn("runners/set-portlane.sh", calls)
        self.assertIn("pct push '100' /tmp/set-portlane.sh /root/set-portlane.sh", calls)
        self.assertIn("/root/add-runner.sh 'o/r' '1' '' '5'", calls)
        self.assertIn("/root/add-runner.sh 'o/r' '2' '' '6'", calls)

    def test_without_a_first_lane_no_lane_is_passed(self):
        calls = self.run_register("1")
        self.assertIn("/root/add-runner.sh 'o/r' '1' '' ''", calls)


if __name__ == "__main__":
    unittest.main()
