# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""verified_commit.py against a real git checkout and a fake GitHub API.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github/actions/verified-commit/verified_commit.py"
spec = importlib.util.spec_from_file_location("verified_commit", SCRIPT)
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeGitHub:
    """Just enough of GitHub: refs, createCommitOnBranch, commit verification."""

    def __init__(self, branches, verified=True):
        self.branches = dict(branches)
        self.verified = verified
        self.mutations, self.created_refs = [], []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if "/git/ref/heads/" in self.path:
                    name = self.path.split("/git/ref/heads/", 1)[1]
                    if name in fake.branches:
                        return self.reply(200, {"object": {"sha": fake.branches[name]}})
                    return self.reply(404, {"message": "Not Found"})
                if "/commits/" in self.path:
                    reason = "valid" if fake.verified else "unsigned"
                    return self.reply(200, {"commit": {"verification": {"verified": fake.verified, "reason": reason}}})
                self.reply(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/git/refs"):
                    fake.created_refs.append(body)
                    fake.branches[body["ref"].removeprefix("refs/heads/")] = body["sha"]
                    return self.reply(201, {})
                inp = body["variables"]["input"]
                fake.mutations.append(inp)
                name = inp["branch"]["branchName"]
                if fake.branches.get(name) != inp["expectedHeadOid"]:
                    return self.reply(200, {"data": {"createCommitOnBranch": None}, "errors": [
                        {"type": "STALE_DATA", "message": "Expected branch to point to ..."}]})
                fake.branches[name] = "c0ffee" * 6 + "abcd"
                return self.reply(200, {"data": {"createCommitOnBranch": {"commit": {
                    "oid": fake.branches[name], "url": "https://github.example/commit"}}}})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


class VerifiedCommitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.email", "t@example.com")
        sh(self.repo, "git", "config", "user.name", "t")
        sh(self.repo, "git", "config", "commit.gpgsign", "false")
        for name in ("pyproject.toml", "old.md", "gone.md"):
            (self.repo / name).write_text(f"{name}\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "base")
        self.head = sh(self.repo, "git", "rev-parse", "HEAD")
        self.env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()

    def run_main(self, *args, fake=None, token="t0ken"):
        os.environ.pop("GITHUB_OUTPUT", None)
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ["GH_TOKEN"] = token
        if fake:
            os.environ["GITHUB_API_URL"] = fake.url
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = vc.main(["--repo", "o/r", "--cwd", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def edit_tree(self):
        (self.repo / "pyproject.toml").write_text('version = "1.2.4"\n')
        (self.repo / "new.md").write_text("new\n")
        (self.repo / "gone.md").unlink()
        sh(self.repo, "git", "mv", "old.md", "renamed.md")

    # -- the change set ---------------------------------------------------

    def test_auto_takes_modified_new_deleted_and_renamed_files(self):
        self.edit_tree()
        writes, deletes = vc.changed_paths(self.repo)
        self.assertEqual(sorted(writes), ["new.md", "pyproject.toml", "renamed.md"])
        self.assertEqual(sorted(deletes), ["gone.md", "old.md"])

    def test_contents_are_the_files_bytes(self):
        (self.repo / "pyproject.toml").write_bytes(b"\x00binary\xff")
        fc = vc.file_changes(self.repo, ["pyproject.toml"], [])
        self.assertEqual(base64.b64decode(fc["additions"][0]["contents"]), b"\x00binary\xff")

    def test_nothing_to_commit_is_refused(self):
        code, _, err = self.run_main("--branch", "b", "--message", "m", "--dry-run")
        self.assertEqual((code, "nothing to commit" in err), (1, True))

    def test_an_executable_file_is_refused_not_committed_without_its_bit(self):
        script = self.repo / "run.sh"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        fake = FakeGitHub({"b": self.head})
        self.addCleanup(fake.close)
        code, _, err = self.run_main("--branch", "b", "--message", "m", fake=fake)
        self.assertEqual(code, 1)
        self.assertIn("executable", err)
        self.assertEqual(fake.mutations, [], "nothing may be sent once a file is refused")

    def test_a_symlink_is_refused(self):
        (self.repo / "link").symlink_to("pyproject.toml")
        code, _, err = self.run_main("--branch", "b", "--message", "m", "--dry-run")
        self.assertEqual((code, "symlink" in err), (1, True))

    def test_message_splits_into_headline_and_body(self):
        self.assertEqual(vc.split_message("chore: bump\n\nwhy\nmore\n"),
                         {"headline": "chore: bump", "body": "why\nmore"})
        with self.assertRaises(vc.Refused):
            vc.split_message("\n\n")

    # -- the API flow -----------------------------------------------------

    def test_commits_on_top_of_local_head_and_reports_verified(self):
        self.edit_tree()
        fake = FakeGitHub({"release/bump": self.head})
        self.addCleanup(fake.close)
        code, out, _ = self.run_main("--branch", "release/bump", "--message", "chore: bump\n\nbody", fake=fake)
        self.assertEqual(code, 0, out)
        sent = fake.mutations[0]
        self.assertEqual(sent["expectedHeadOid"], self.head)
        self.assertEqual(sent["message"], {"headline": "chore: bump", "body": "body"})
        self.assertNotIn("author", json.dumps(sent), "a custom author would cost the signature")
        self.assertIn("verified: True", out)

    def test_a_missing_branch_is_created_at_head_first(self):
        self.edit_tree()
        fake = FakeGitHub({"main": self.head})
        self.addCleanup(fake.close)
        code, out, _ = self.run_main("--branch", "release/bump-1.2.4", "--message", "m", fake=fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.created_refs, [{"ref": "refs/heads/release/bump-1.2.4", "sha": self.head}])

    def test_a_branch_that_moved_is_refused_by_the_api(self):
        self.edit_tree()
        fake = FakeGitHub({"b": "f" * 40})
        self.addCleanup(fake.close)
        code, _, err = self.run_main("--branch", "b", "--message", "m", fake=fake)
        self.assertEqual(code, 2)
        self.assertIn("STALE_DATA", err)

    def test_an_unverified_commit_fails_loud(self):
        self.edit_tree()
        fake = FakeGitHub({"b": self.head}, verified=False)
        self.addCleanup(fake.close)
        code, out, err = self.run_main("--branch", "b", "--message", "m", fake=fake)
        self.assertEqual(code, 3)
        self.assertIn("Commit not Verified", err)

    def test_outputs_are_written_for_the_action(self):
        self.edit_tree()
        fake = FakeGitHub({"b": self.head})
        self.addCleanup(fake.close)
        out_file = self.repo.parent / f"{self.repo.name}.out"
        self.addCleanup(lambda: out_file.unlink(missing_ok=True))
        os.environ["GITHUB_API_URL"] = fake.url
        os.environ["GH_TOKEN"] = "t"
        os.environ["GITHUB_OUTPUT"] = str(out_file)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(vc.main(["--repo", "o/r", "--cwd", str(self.repo), "--branch", "b", "--message", "m"]), 0)
        self.assertIn("verified=true", out_file.read_text())

    def test_dry_run_sends_nothing_and_needs_no_token(self):
        self.edit_tree()
        code, out, _ = self.run_main("--branch", "b", "--message", "m", "--dry-run", token="")
        self.assertEqual(code, 0)
        self.assertIn('"expectedHeadOid"', out)
        self.assertIn("base64 chars", out)


if __name__ == "__main__":
    unittest.main()
