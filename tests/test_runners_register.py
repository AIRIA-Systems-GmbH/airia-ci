# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""runners/register.sh: how it treats the grep that keeps token lines out of its output.

What these tests hold: the filter tolerates grep finding nothing to print
(exit 1, every line was a token line) and nothing else. A failing `gh` or
`ssh` fails the run, and so does a grep error (exit 2), which `|| true` on
that line would swallow; a registration that broke would otherwise look
registered. `ssh`, `scp` and `gh` are fakes, the script is the shipped file.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import unittest

from steps import ROOT, Runner

SCRIPT = ROOT / "runners/register.sh"

# The pool container's add-runner.sh output is whatever the test sets in FAKE_SSH_OUT; only the
# call that pipes a token in (the one running add-runner.sh with arguments) reads stdin and prints it.
FAKE_SSH = """#!/usr/bin/env bash
case "$*" in
  *"-- /root/add-runner.sh '"*) cat >/dev/null; printf '%s' "${FAKE_SSH_OUT-}"; exit "${FAKE_SSH_EXIT:-0}" ;;
esac
"""

# grep that fails with the status in FAKE_GREP_EXIT, else the real one. It reads its input first:
# a grep that quit early would hand ssh a SIGPIPE and fail the pipeline for that reason instead.
FAKE_GREP = """#!/usr/bin/env bash
[ -n "${FAKE_GREP_EXIT:-}" ] && { cat >/dev/null; exit "$FAKE_GREP_EXIT"; }
exec %s "$@"
"""

RULES = [
    ["registration-token", {"text": "tok\n"}],
    ["actions/runners --jq", {"text": "thor-1  online\n"}],
]


class RegisterFilter(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self, RULES)
        self.r.tool("ssh", FAKE_SSH)
        self.r.tool("scp", "#!/usr/bin/env bash\nexit 0\n")
        self.r.tool("grep", FAKE_GREP % shutil.which("grep"))
        conf = self.r.tmp / "runners.env"
        conf.write_text("THOR_SSH=root@thor.invalid\nTHOR_CT=100\n")
        self.conf = str(conf)

    def register(self, **env):
        return self.r.script(SCRIPT, "o/r", "1", AIRIA_CI_RUNNERS_ENV=self.conf, **env)

    def test_output_with_no_token_line_is_registered(self):
        p = self.register(FAKE_SSH_OUT="Runner added\ntoken abc\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Runner added", p.stdout)
        self.assertNotIn("token abc", p.stdout)
        self.assertIn("thor-1  online", p.stdout)

    def test_output_that_is_all_token_lines_is_still_registered(self):
        p = self.register(FAKE_SSH_OUT="token abc\nSecret def\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("abc", p.stdout)
        self.assertIn("thor-1  online", p.stdout)

    def test_a_failing_gh_fails_the_run(self):
        self.r.rules([["registration-token", {"exit": 1, "stderr": "HTTP 403\n"}]])
        p = self.register(FAKE_SSH_OUT="Runner added\n")
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("registered runners", p.stdout)

    def test_a_failing_ssh_fails_the_run(self):
        p = self.register(FAKE_SSH_OUT="Runner added\n", FAKE_SSH_EXIT="255")
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("registered runners", p.stdout)

    def test_a_grep_error_fails_the_run(self):
        p = self.register(FAKE_SSH_OUT="Runner added\n", FAKE_GREP_EXIT="2")
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("registered runners", p.stdout)


if __name__ == "__main__":
    unittest.main()
