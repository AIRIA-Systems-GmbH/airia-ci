# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""release.yml: the one step that moves a tag every consumer runs.

What these tests hold: a tag only ever points at a tested commit of main; a
move only goes forward, so no consumer loses a commit it already runs; the
newest tag is found by version, not by spelling (ci-v10 is after ci-v2); the
reusable workflows' HARNESS_MAJOR is the major being released.

Run: python3 -m unittest discover -s tests
"""

import subprocess
import unittest

from steps import ROOT, Runner

WORKFLOW = ROOT / ".github/workflows/release.yml"
STEP = "Point the tag at this commit"
TESTED = [["run list", {"json": [{"databaseId": 1}]}], ["release create", {}]]
UNTESTED = [["run list", {"json": []}]]
HARNESS = (".github/workflows/reusable-claude-review.yml", ".github/workflows/reusable-claude-respond.yml")
GIT_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class Release(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        self.origin, self.work = self.r.tmp / "origin.git", self.r.tmp / "work"
        self.git("init", "-q", "--bare", str(self.origin), cwd=self.r.tmp)
        self.git("init", "-q", "-b", "main", str(self.work), cwd=self.r.tmp)
        self.git("remote", "add", "origin", str(self.origin))
        self.a = self.commit("a")
        self.git("tag", "ci-v2")
        self.b = self.commit("b")
        self.git("tag", "ci-v10")
        self.head = self.commit("c")
        self.git("push", "-q", "origin", "main", "--tags")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.work, env={**self.r.env(), **GIT_ID},
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, msg):
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def tag_at_origin(self, tag):
        return self.git("rev-parse", "-q", "--verify", f"{tag}^{{commit}}", cwd=self.origin)

    def harness(self, review, respond=None):
        """Commit the two reusable workflows, each naming its major, as main's head."""
        for f, major in zip(HARNESS, (review, review if respond is None else respond), strict=True):
            path = self.work / f
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'jobs:\n  j:\n    steps:\n      - env:\n          HARNESS_MAJOR: "{major}"\n')
        self.git("add", "-A")
        self.head = self.commit(f"harness {review}")

    def release(self, bump, sha=None, ref="refs/heads/main", rules=TESTED, major=None):
        if sha is None and major is None:
            self.harness(10 if bump == "move" else 11)
        self.r.rules(rules)
        return self.r.run(WORKFLOW, STEP, cwd=self.work, BUMP=bump, SHA=sha or self.head, GITHUB_REF=ref)

    def test_move_points_the_newest_tag_by_version_at_main(self):
        p = self.release("move")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.tag_at_origin("ci-v10"), self.head)
        self.assertEqual(self.tag_at_origin("ci-v2"), self.a, "an older tag never moves")
        self.assertIn(f"ci-v10 now points at {self.head}", self.r.summary.read_text())

    def release_call(self):
        (call,) = [c for c in self.r.calls() if c["args"][:2] == ["release", "create"]]
        return call

    def test_a_move_publishes_a_release_listing_what_consumers_now_run(self):
        # Watchers of Releases are told; the notes are the commits since the tag's last position.
        p = self.release("move")
        self.assertEqual(p.returncode, 0, p.stderr)
        call = self.release_call()
        self.assertEqual(call["args"][2], "ci-v10.1")
        self.assertEqual(call["args"][call["args"].index("--target") + 1], self.head)
        notes = call["files"]["--notes-file"]
        self.assertIn(f"`ci-v10` now points at {self.head}. Since ci-v10 was at {self.b[:7]}:", notes)
        self.assertIn("- c", notes.splitlines())
        self.assertNotIn("- b", notes.splitlines(), "only what is new to ci-v10's consumers")

    def test_the_next_release_of_the_same_major_counts_up(self):
        self.git("tag", "ci-v10.1", self.b)
        self.git("tag", "ci-v10.x", self.b)
        p = self.release("move")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.release_call()["args"][2], "ci-v10.2")
        self.assertEqual(self.tag_at_origin("ci-v10"), self.head, "ci-v10.1 is never taken for the newest")

    def test_a_new_major_starts_its_own_releases(self):
        p = self.release("new")
        self.assertEqual(p.returncode, 0, p.stderr)
        call = self.release_call()
        self.assertEqual(call["args"][2], "ci-v11.1")
        self.assertIn("Since ci-v10 was at", call["files"]["--notes-file"])

    def test_new_creates_the_next_major_and_leaves_the_current_one(self):
        p = self.release("new")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.tag_at_origin("ci-v11"), self.head)
        self.assertEqual(self.tag_at_origin("ci-v10"), self.b)

    def test_a_move_that_is_not_forward_is_refused(self):
        # A commit beside ci-v10, not after it: consumers of ci-v10 would lose b.
        self.git("checkout", "-q", self.a)
        beside = self.commit("beside")
        p = self.release("move", sha=beside)
        self.assertEqual(p.returncode, 1)
        self.assertIn("Not forward", p.stdout)
        self.assertEqual(self.tag_at_origin("ci-v10"), self.b)

    def test_a_commit_self_test_did_not_pass_is_refused(self):
        p = self.release("move", rules=UNTESTED)
        self.assertEqual(p.returncode, 1)
        self.assertIn("Not tested", p.stdout)
        self.assertEqual(self.tag_at_origin("ci-v10"), self.b)
        (call,) = self.r.calls()
        self.assertEqual(call["args"][call["args"].index("--commit") + 1], self.head, "the exact commit is checked")
        self.assertNotIn("release", call["args"], "no release for an untested commit")
        self.assertIn("success", call["args"])

    def test_a_tag_that_is_not_ci_vN_is_never_taken_for_the_newest(self):
        # ci-vnext and ci-v11-rc sort after ci-v10 by version; neither is a release.
        self.git("tag", "ci-vnext")
        self.git("tag", "ci-v11-rc")
        p = self.release("new")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.tag_at_origin("ci-v11"), self.head)

    def test_no_ci_v_tag_is_refused_with_a_reason(self):
        self.git("tag", "-d", "ci-v2", "ci-v10")
        p = self.release("move")
        self.assertEqual(p.returncode, 1)
        self.assertIn("No release tag", p.stdout)

    def test_a_new_major_whose_workflows_still_name_the_old_one_is_refused(self):
        # ci-v11's own callers would be told on every review to upgrade to ci-v11.
        self.harness(10)
        p = self.release("new", major=10)
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Wrong HARNESS_MAJOR::.github/workflows/reusable-claude-review.yml says HARNESS_MAJOR '10' but this release is ci-v11",
                      p.stdout)
        self.assertEqual(self.git("ls-remote", "--tags", str(self.origin), "ci-v11"), "")
        self.assertNotIn("release", [a for c in self.r.calls() for a in c["args"]])

    def test_a_move_whose_workflows_name_the_next_major_is_refused(self):
        # ci-v10's callers would be told ci-v11 is theirs before it exists.
        self.harness(11)
        p = self.release("move", major=11)
        self.assertEqual(p.returncode, 1)
        self.assertIn("Wrong HARNESS_MAJOR", p.stdout)
        self.assertEqual(self.tag_at_origin("ci-v10"), self.b)

    def test_each_workflow_is_checked_not_just_the_first(self):
        self.harness(11, respond=10)
        p = self.release("new", major=11)
        self.assertEqual(p.returncode, 1)
        self.assertIn("reusable-claude-respond.yml says HARNESS_MAJOR '10'", p.stdout)

    def test_a_bump_that_is_neither_move_nor_new_moves_nothing(self):
        # The dispatch form offers only the two, but the API takes any string.
        p = self.release("major")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error::bump must be move or new, not major", p.stdout)
        self.assertEqual(self.tag_at_origin("ci-v10"), self.b)

    def test_only_main_is_released(self):
        p = self.release("new", ref="refs/heads/feature")
        self.assertEqual(p.returncode, 1)
        self.assertIn("Not main", p.stdout)
        self.assertEqual(self.r.calls(), [])
        self.assertEqual(self.git("ls-remote", "--tags", str(self.origin), "ci-v11"), "")


if __name__ == "__main__":
    unittest.main()
