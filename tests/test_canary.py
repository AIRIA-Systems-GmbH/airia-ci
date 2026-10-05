# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""scripts/canary.py against a fake `gh`.

What these tests hold: only a reply that carries this run's token, posted
after the canary comment, counts; silence until the timeout fails and names
the responder's run, so a broken release is visible; a comment that could not
be posted is an error, not a wait.

Run: python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

from steps import ROOT, Runner

SCRIPT = ROOT / "scripts/canary.py"
spec = importlib.util.spec_from_file_location("canary", SCRIPT)
canary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canary)

POSTED = ["pr comment 7", {"text": "https://github.com/o/r/pull/7#issuecomment-500\n"}]
RUN = ["run list", {"json": [{"status": "completed", "conclusion": "failure", "url": "https://run/9"}]}]


class Canary(unittest.TestCase):
    def run_main(self, rules, timeout=40):
        r = Runner(self, rules)
        out, err, naps = io.StringIO(), io.StringIO(), []
        with mock.patch.dict(os.environ, r.env()), mock.patch.object(canary.uuid, "uuid4") as u, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            u.return_value.hex = "abc123def456ffff"
            code = canary.main(["--repo", "o/r", "--pr", "7", "--timeout", str(timeout), "--interval", "20"],
                               sleep=naps.append)
        return code, out.getvalue(), err.getvalue(), r, naps

    def test_the_reply_with_this_runs_token_passes(self):
        code, out, _, r, _ = self.run_main([
            POSTED, ["issues/7/comments", {"json": [{"id": 501, "body": "CANARY-OK abc123def456"}]}]])
        self.assertEqual(code, 0)
        self.assertIn("@claude answered on o/r#7", out)
        posted = r.calls()[0]["args"]
        self.assertIn("@claude", posted[posted.index("--body") + 1])
        read = [c["args"] for c in r.calls() if "issues/7/comments" in " ".join(c["args"])][0]
        self.assertIn(".id > 500", read[read.index("--jq") + 1], "only comments after the canary count")

    def test_an_earlier_canarys_reply_does_not_count_and_silence_fails(self):
        code, _, err, _, naps = self.run_main([
            POSTED, ["issues/7/comments", {"json": [{"id": 501, "body": "CANARY-OK 000000000000"}]}], RUN])
        self.assertEqual(code, 1)
        self.assertEqual(naps, [20, 20], "it waits the whole timeout")
        self.assertIn("no reply on o/r#7 after 40s; latest claude.yml run: completed failure https://run/9", err)

    def test_silence_with_no_responder_run_says_so(self):
        code, _, err, _, _ = self.run_main([
            POSTED, ["issues/7/comments", {"json": []}], ["run list", {"json": []}]], timeout=0)
        self.assertEqual(code, 1)
        self.assertIn("no claude.yml run found", err)

    def test_a_comment_that_could_not_be_posted_is_an_error(self):
        code, _, err, r, naps = self.run_main([["pr comment", {"exit": 1, "stderr": "HTTP 403"}]])
        self.assertEqual(code, 2)
        self.assertIn("could not comment on o/r#7: HTTP 403", err)
        self.assertEqual(naps, [])

    def test_unreadable_comments_are_an_error(self):
        code, _, err, _, _ = self.run_main([POSTED, ["issues/7/comments", {"exit": 1, "stderr": "HTTP 502"}]])
        self.assertEqual(code, 2)
        self.assertIn("could not read o/r#7's comments: HTTP 502", err)


if __name__ == "__main__":
    unittest.main()
