# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The claude-cli action's shipped shell: a daily-refreshed Claude Code cache.

Its promise is that it cannot break a job: every failure keeps the previous
build, and with no build at all the `path` output is empty so
claude-code-action installs its own. A fake `curl` serves a fake installer.

GNU-only (`mv -T`, `flock`), like the Thor runners: skipped elsewhere, never
in tests/coverage.sh, which refuses a skip.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import unittest

from steps import ROOT, Runner

ACTION = ROOT / ".github/actions/claude-cli/action.yml"
STEP = "Resolve the cached Claude Code"
GNU = shutil.which("flock") is not None and subprocess.run(
    ["mv", "-T", "--help"], capture_output=True).returncode == 0

# Installs a `claude` that reports $FAKE_VERSION, or fails --version once it
# has been copied into versions/ when FAKE_BROKEN is set.
INSTALLER = """\
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/claude"
cat > "$HOME/.local/share/claude/claude" <<EOF
#!/bin/sh
case "\\$0" in */versions/*) [ -n "$FAKE_BROKEN" ] && exit 1 ;; esac
echo "$FAKE_VERSION (Claude Code)"
EOF
chmod +x "$HOME/.local/share/claude/claude"
ln -s "$HOME/.local/share/claude/claude" "$HOME/.local/bin/claude"
"""

CURL = """\
#!/bin/sh
echo "$@" >> "$FAKE_CURL_LOG"
[ -n "$FAKE_CURL_FAIL" ] && { echo "curl: (6) Could not resolve host" >&2; exit 6; }
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && { cp "$FAKE_INSTALLER" "$2"; exit 0; }; shift; done
exit 2
"""


@unittest.skipUnless(GNU, "needs GNU mv -T and flock, as on the Linux runners")
class Cache(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        self.r.tool("curl", CURL)
        (self.r.tmp / "install.sh").write_text(INSTALLER)
        self.root = self.r.tmp / "cache/claude"
        self.curl_log = self.r.tmp / "curl.log"

    def resolve(self, version="2.1.300", **env):
        self.r.output.unlink(missing_ok=True)
        p = self.r.run(ACTION, STEP, CLAUDE_CLI_CACHE=str(self.root), FAKE_VERSION=version,
                       FAKE_INSTALLER=str(self.r.tmp / "install.sh"), FAKE_CURL_LOG=str(self.curl_log), **env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p, self.r.outputs()

    def downloads(self):
        return len(self.curl_log.read_text().splitlines()) if self.curl_log.exists() else 0

    def age_check(self, minutes):
        t = time.time() - minutes * 60
        os.utime(self.root / ".checked", (t, t))

    def test_a_fresh_cache_installs_and_hands_over_the_binary(self):
        p, out = self.resolve()
        self.assertEqual(out, {"path": str((self.root / "versions/2.1.300").resolve()), "version": "2.1.300"})
        self.assertEqual(os.readlink(self.root / "current"), "versions/2.1.300")
        self.assertIn("Claude Code: 2.1.300 (Claude Code) at", p.stdout)

    def test_a_check_less_than_a_day_old_does_not_download(self):
        self.resolve()
        self.age_check(60 * 23)
        _, out = self.resolve(version="2.1.301")
        self.assertEqual((self.downloads(), out["version"]), (1, "2.1.300"))

    def test_a_day_old_check_refreshes_to_the_new_release(self):
        self.resolve()
        self.age_check(60 * 25)
        _, out = self.resolve(version="2.1.301")
        self.assertEqual((self.downloads(), out["version"]), (2, "2.1.301"))

    def test_only_the_three_newest_releases_are_kept(self):
        for i, v in enumerate(("2.1.1", "2.1.2", "2.1.3", "2.1.4")):
            self.resolve(version=v)
            t = time.time() - (10 - i) * 3600
            os.utime(self.root / "versions" / v, (t, t))
            self.age_check(60 * 25)
        self.assertEqual(sorted(os.listdir(self.root / "versions")), ["2.1.2", "2.1.3", "2.1.4"])

    def test_a_failed_download_keeps_the_previous_build(self):
        self.resolve()
        self.age_check(60 * 25)
        p, out = self.resolve(version="2.1.301", FAKE_CURL_FAIL="1")
        self.assertIn("refresh failed; keeping the previous build", p.stdout)
        self.assertEqual(out["version"], "2.1.300")

    def test_a_build_that_fails_its_smoke_test_is_not_swapped_in(self):
        self.resolve()
        self.age_check(60 * 25)
        p, out = self.resolve(version="2.1.301", FAKE_BROKEN="1")
        self.assertIn("claude 2.1.301 failed its --version smoke test; keeping the previous build", p.stdout)
        self.assertEqual(out["version"], "2.1.300")

    def test_no_build_at_all_lets_the_action_install_its_own(self):
        p, out = self.resolve(FAKE_CURL_FAIL="1")
        self.assertEqual(out, {"path": "", "version": ""})


class Unwritable(unittest.TestCase):
    def test_an_unwritable_cache_falls_back_without_failing(self):
        r = Runner(self)
        (r.tmp / "a-file").write_text("")
        # A path under a regular file: unwritable even for root, as in the container.
        p = r.run(ACTION, STEP, CLAUDE_CLI_CACHE=str(r.tmp / "a-file/claude"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(r.outputs(), {"path": "", "version": ""})
        self.assertIn("is not writable; claude-code-action will install its own copy", p.stdout)


if __name__ == "__main__":
    unittest.main()
