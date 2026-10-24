# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""scripts/findings.py against a fake `gh`.

What these tests hold: "every bot finding has a reply" (the rule a PR at the
review cap merges by, airia-ci#71) is answered by code, not by reading a thread:
a finding is open until a comment replies to it or its thread is resolved, only
a Bot's marker makes a finding, every page is read, and a failed lookup is an
error, never "none left".

Run: python3 -m unittest discover -s tests
"""

import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

from steps import ROOT, Runner

spec = importlib.util.spec_from_file_location("findings", ROOT / "scripts/findings.py")
findings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(findings)


def finding(cid, fid="F1", path="a.py", user="Bot", reply_to=None):
    return {"id": cid, "user": {"type": user}, "in_reply_to_id": reply_to, "html_url": f"https://x/pull/7#discussion_r{cid}",
            "body": f"text\n<!-- airia-finding run=11 id={fid} file={path} -->"}


def threads(*resolved_ids, open_ids=()):
    nodes = [{"isResolved": True, "comments": {"nodes": [{"databaseId": i}]}} for i in resolved_ids]
    nodes += [{"isResolved": False, "comments": {"nodes": [{"databaseId": i}]}} for i in open_ids]
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}


class Findings(unittest.TestCase):
    def check(self, pages, thread_pages=None, **rules):
        r = Runner(self, [
            ["graphql", rules.get("graphql", {"json": thread_pages or [threads()]})],
            ["pulls/7/comments", rules.get("comments", {"json": pages})],
        ])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, r.env()), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = findings.main(["--repo", "o/r", "--pr", "7"])
        return code, out.getvalue(), err.getvalue()

    def test_a_pr_without_findings_passes(self):
        code, out, _ = self.check([[]])
        self.assertEqual(code, 0)
        self.assertIn("0 open finding(s) on o/r#7", out)

    def test_an_unanswered_finding_is_listed_and_fails(self):
        code, out, _ = self.check([[finding(101, "F2", "src/b.py")]])
        self.assertEqual(code, 1)
        self.assertIn("F2\tsrc/b.py\thttps://x/pull/7#discussion_r101", out)
        self.assertIn("1 open finding(s)", out)

    def test_a_reply_answers_the_finding(self):
        code, out, _ = self.check([[finding(101), finding(102, "F2"),
                                    finding(103, "x", user="User", reply_to=101)]])
        self.assertEqual(code, 1)
        self.assertNotIn("F1\t", out)
        self.assertIn("F2\ta.py", out)
        self.assertIn("1 open finding(s)", out)

    def test_a_resolved_thread_answers_the_finding(self):
        code, out, _ = self.check([[finding(101), finding(102, "F2")]], [threads(101, open_ids=[102])])
        self.assertEqual(code, 1)
        self.assertIn("F2\ta.py", out)
        self.assertNotIn("F1\ta.py", out)
        code, _, _ = self.check([[finding(101)]], [threads(101)])
        self.assertEqual(code, 0)

    def test_a_marker_quoted_by_a_person_is_not_a_finding(self):
        code, out, _ = self.check([[finding(101, user="User")]])
        self.assertEqual(code, 0)
        self.assertIn("0 open finding(s)", out)

    def test_a_comment_without_the_marker_is_not_a_finding(self):
        code, _, _ = self.check([[{**finding(101), "body": "just a review comment"}]])
        self.assertEqual(code, 0)

    def test_every_page_is_read_for_comments_and_threads(self):
        # The reply on page 2 answers the finding on page 1; the resolved thread on page 2 answers its second.
        code, out, _ = self.check([[finding(101), finding(102, "F2"), finding(103, "F3")],
                                   [finding(201, "R", user="User", reply_to=101)]],
                                  [threads(), threads(102)])
        self.assertEqual(code, 1)
        self.assertEqual(out.count("\n"), 2)
        self.assertIn("F3\ta.py", out)
        self.assertIn("1 open finding(s)", out)

    def test_a_failed_lookup_is_an_error_not_none_left(self):
        code, out, err = self.check([[]], comments={"exit": 1, "stderr": "HTTP 403"})
        self.assertEqual(code, 2)
        self.assertIn("HTTP 403", err)
        self.assertEqual(out, "")
        code, _, err = self.check([[finding(101)]], graphql={"exit": 1, "stderr": "HTTP 502"})
        self.assertEqual(code, 2)
        self.assertIn("graphql failed: HTTP 502", err)

    def test_the_queries_ask_for_the_pr_and_every_page(self):
        r = Runner(self, [["graphql", {"json": [threads()]}], ["pulls/7/comments", {"json": [[]]}]])
        with mock.patch.dict(os.environ, r.env()), contextlib.redirect_stdout(io.StringIO()):
            findings.main(["--repo", "o/r", "--pr", "7"])
        calls = [c["args"] for c in r.calls()]
        self.assertTrue(all("--paginate" in a and "--slurp" in a for a in calls))
        graphql = next(a for a in calls if "graphql" in a)
        self.assertIn("owner=o", graphql)
        self.assertIn("name=r", graphql)
        self.assertIn("pr=7", graphql)
        self.assertIn("isResolved", " ".join(graphql))


if __name__ == "__main__":
    unittest.main()
