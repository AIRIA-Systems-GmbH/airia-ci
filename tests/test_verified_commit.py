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

from steps import ROOT, Runner

SCRIPT = Path(__file__).resolve().parents[1] / ".github/actions/verified-commit/verified_commit.py"
spec = importlib.util.spec_from_file_location("verified_commit", SCRIPT)
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeGitHub:
    """Just enough of GitHub: refs, createCommitOnBranch, commit verification."""

    def __init__(self, branches, verified=True, remote=None):
        self.branches = dict(branches)
        self.verified = verified
        # With a bare `remote`, the mutation really builds the commit there,
        # so a fast-forward can fetch it. `race`: someone pushes right after;
        # `drop`: the commit silently loses its last file.
        self.remote, self.race, self.drop = remote, False, False
        self.mutations, self.created_refs = [], []
        self.reject_graphql = self.reject_ref = False
        # Endpoints ("ref", "graphql", "commits") that answer with GitHub's
        # HTML 502 page instead of JSON, as its front end does in an outage.
        self.outage = set()
        self.outage_page = b"<html><body><h1>502 Bad Gateway</h1></body></html>"
        self.outage_status = 502
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

            def down(self, endpoint):
                if endpoint not in fake.outage:
                    return False
                page = fake.outage_page
                self.send_response(fake.outage_status)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return True

            def do_GET(self):
                if "/git/ref/heads/" in self.path and self.down("ref"):
                    return None
                if "/commits/" in self.path and self.down("commits"):
                    return None
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
                    if fake.reject_ref:
                        return self.reply(422, {"message": "Reference update failed"})
                    fake.created_refs.append(body)
                    fake.branches[body["ref"].removeprefix("refs/heads/")] = body["sha"]
                    return self.reply(201, {})
                if self.down("graphql"):
                    return None
                if fake.reject_graphql:
                    return self.reply(401, {"message": "Bad credentials"})
                inp = body["variables"]["input"]
                fake.mutations.append(inp)
                name = inp["branch"]["branchName"]
                if fake.branches.get(name) != inp["expectedHeadOid"]:
                    return self.reply(200, {"data": {"createCommitOnBranch": None}, "errors": [
                        {"type": "STALE_DATA", "message": "Expected branch to point to ..."}]})
                fake.branches[name] = fake.build(inp) if fake.remote else "c0ffee" * 6 + "abcd"
                return self.reply(200, {"data": {"createCommitOnBranch": {"commit": {
                    "oid": fake.branches[name], "url": "https://github.example/commit"}}}})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def build(self, inp):
        def git(*args, data=None, env=None):
            return subprocess.run(["git", f"--git-dir={self.remote}", *args], input=data, check=True,
                                  capture_output=True, env={**os.environ, **(env or {})}).stdout.decode().strip()
        ref, parent = f"refs/heads/{inp['branch']['branchName']}", inp["expectedHeadOid"]
        additions = inp["fileChanges"]["additions"][:-1 if self.drop else None]
        with tempfile.NamedTemporaryFile() as idx, tempfile.TemporaryDirectory() as wt:
            env = {"GIT_INDEX_FILE": idx.name, "GIT_WORK_TREE": wt}
            git("read-tree", parent, env=env)
            for d in inp["fileChanges"]["deletions"]:
                git("update-index", "--force-remove", d["path"], env=env)
            for a in additions:
                blob = git("hash-object", "-w", "--stdin", data=base64.b64decode(a["contents"]))
                git("update-index", "--add", "--cacheinfo", f"100644,{blob},{a['path']}", env=env)
            tree = git("write-tree", env=env)
        # GitHub sets the identity itself; a runner has none to guess from.
        ident = {f"GIT_{who}_{what}": value for who in ("AUTHOR", "COMMITTER")
                 for what, value in (("NAME", "fake-app[bot]"), ("EMAIL", "bot@example.invalid"))}
        oid = git("commit-tree", tree, "-p", parent, "-m", inp["message"]["headline"], env=ident)
        git("update-ref", ref, oid, parent)
        if self.race:
            racer = git("commit-tree", tree, "-p", oid, "-m", "a concurrent push", env=ident)
            git("update-ref", ref, racer, oid)
        return oid

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Checkout(unittest.TestCase):
    """A real git checkout with a base commit; the helpers every test here uses."""

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

    def point_at(self, fake):
        # Both: a GitHub runner exports GITHUB_GRAPHQL_URL, which would send
        # the mutation to the real API.
        os.environ["GITHUB_API_URL"] = fake.url
        os.environ["GITHUB_GRAPHQL_URL"] = fake.url + "/graphql"

    def run_main(self, *args, fake=None, token="t0ken"):
        os.environ.pop("GITHUB_OUTPUT", None)
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ["GH_TOKEN"] = token
        if fake:
            self.point_at(fake)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = vc.main(["--repo", "o/r", "--cwd", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def edit_tree(self):
        (self.repo / "pyproject.toml").write_text('version = "1.2.4"\n')
        (self.repo / "new.md").write_text("new\n")
        (self.repo / "gone.md").unlink()
        sh(self.repo, "git", "mv", "old.md", "renamed.md")

    def with_origin(self, branch="main"):
        remote = self.repo.parent / f"{self.repo.name}.git"
        sh(self.repo.parent, "git", "init", "-q", "--bare", str(remote))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(remote)], check=True))
        sh(self.repo, "git", "remote", "add", "origin", str(remote))
        sh(self.repo, "git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        fake = FakeGitHub({branch: self.head}, remote=str(remote))
        self.addCleanup(fake.close)
        return fake



class VerifiedCommitTest(Checkout):
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

    def test_a_rejected_token_is_reported_not_swallowed(self):
        self.edit_tree()
        fake = FakeGitHub({"b": self.head})
        self.addCleanup(fake.close)
        fake.reject_graphql = True
        code, _, err = self.run_main("--branch", "b", "--message", "m", fake=fake)
        self.assertEqual(code, 2)
        self.assertIn("Bad credentials", err)

    def test_an_unverified_commit_fails_loud(self):
        self.edit_tree()
        fake = FakeGitHub({"b": self.head}, verified=False)
        self.addCleanup(fake.close)
        code, _, err = self.run_main("--branch", "b", "--message", "m", fake=fake)
        self.assertEqual(code, 3)
        self.assertIn("Commit not Verified", err)

    def test_outputs_are_written_for_the_action(self):
        self.edit_tree()
        fake = FakeGitHub({"b": self.head})
        self.addCleanup(fake.close)
        out_file = self.repo.parent / f"{self.repo.name}.out"
        self.addCleanup(lambda: out_file.unlink(missing_ok=True))
        self.point_at(fake)
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


    # -- --fast-forward ---------------------------------------------------

    def test_fast_forward_moves_the_checkout_to_the_new_commit(self):
        fake = self.with_origin()
        self.edit_tree()
        code, out, err = self.run_main("--branch", "main", "--message", "m", "--fast-forward", fake=fake)
        self.assertEqual(code, 0, out + err)
        new = sh(self.repo, "git", "rev-parse", "HEAD")
        self.assertEqual(new, fake.branches["main"])
        self.assertEqual(sh(self.repo, "git", "rev-parse", "HEAD^"), self.head, "only forward: parent is the old head")
        self.assertEqual(sh(self.repo, "git", "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "", "the tree is exactly the commit")

    def test_fast_forward_works_on_a_detached_checkout(self):
        fake = self.with_origin("release/bump")
        sh(self.repo, "git", "checkout", "-q", "--detach")
        self.edit_tree()
        code, out, err = self.run_main("--branch", "release/bump", "--message", "m", "--fast-forward", fake=fake)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sh(self.repo, "git", "rev-parse", "HEAD"), fake.branches["release/bump"])

    def test_without_fast_forward_the_checkout_stays(self):
        fake = self.with_origin()
        self.edit_tree()
        code, _, _ = self.run_main("--branch", "main", "--message", "m", fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(sh(self.repo, "git", "rev-parse", "HEAD"), self.head)

    def test_fast_forward_refuses_a_checkout_on_another_branch_before_writing(self):
        fake = self.with_origin()
        sh(self.repo, "git", "checkout", "-q", "-b", "other")
        self.edit_tree()
        code, _, err = self.run_main("--branch", "main", "--message", "m", "--fast-forward", fake=fake)
        self.assertEqual(code, 1)
        self.assertIn("on other, not main", err)
        self.assertEqual(fake.mutations, [])

    def test_fast_forward_refuses_a_checkout_not_at_expected_head_before_writing(self):
        fake = self.with_origin()
        self.edit_tree()
        code, _, err = self.run_main("--branch", "main", "--message", "m", "--fast-forward",
                                     "--expected-head", "f" * 40, fake=fake)
        self.assertEqual(code, 1)
        self.assertIn("not at --expected-head", err)
        self.assertEqual(fake.mutations, [])

    def test_a_push_right_after_the_commit_leaves_the_checkout_alone(self):
        fake = self.with_origin()
        fake.race = True
        self.edit_tree()
        code, _, err = self.run_main("--branch", "main", "--message", "m", "--fast-forward", fake=fake)
        self.assertEqual(code, 4)
        self.assertIn("moved again", err)
        self.assertEqual(sh(self.repo, "git", "rev-parse", "HEAD"), self.head)
        self.assertEqual((self.repo / "new.md").read_text(), "new\n", "the working tree is untouched")

    def test_a_commit_that_differs_from_the_working_tree_fails_loud(self):
        fake = self.with_origin()
        fake.drop = True
        self.edit_tree()
        code, _, err = self.run_main("--branch", "main", "--message", "m", "--fast-forward", fake=fake)
        self.assertEqual(code, 4)
        self.assertIn("differs from the working tree", err)


    # -- the edges --------------------------------------------------------

    def test_a_copy_entry_writes_the_copy_and_keeps_its_source(self):
        # With status.renames=copies, -z puts a copy's source in the next entry too.
        real = vc.git
        self.addCleanup(setattr, vc, "git", real)
        vc.git = lambda *_: "C  copy.md\0orig.md\0R  new.md\0old.md\0 M kept.md\0"
        self.assertEqual(vc.changed_paths(self.repo), (["copy.md", "new.md", "kept.md"], ["old.md"]))

    def test_explicit_paths_and_deletions_are_taken_as_given(self):
        self.edit_tree()
        code, out, _ = self.run_main("--branch", "b", "--message", "m", "--dry-run",
                                     "--paths", "pyproject.toml", "--delete", "gone.md")
        self.assertEqual(code, 0)
        self.assertIn("1 written, 1 deleted", out)
        self.assertNotIn("new.md", out, "only the named paths")

    def test_a_file_that_is_not_regular_is_refused(self):
        os.mkfifo(self.repo / "pipe")
        code, _, err = self.run_main("--branch", "b", "--message", "m", "--dry-run", "--paths", "pipe")
        self.assertEqual((code, "not a regular file" in err), (1, True))

    def test_no_token_is_refused_before_any_request(self):
        self.edit_tree()
        code, _, err = self.run_main("--branch", "b", "--message", "m", token="")
        self.assertEqual(code, 1)
        self.assertIn("no GH_TOKEN or GITHUB_TOKEN", err)

    def test_a_refused_branch_is_an_api_refusal_not_a_traceback(self):
        self.edit_tree()
        fake = FakeGitHub({"main": self.head})
        self.addCleanup(fake.close)
        fake.reject_ref = True
        code, _, err = self.run_main("--branch", "release/new", "--message", "m", fake=fake)
        self.assertEqual(code, 2)
        self.assertIn("::error title=Branch refused::could not create release/new", err)
        self.assertEqual(fake.mutations, [], "no commit without its branch")

    # An outage must end in the documented exit code and a readable ::error,
    # never a traceback: a traceback exits 1, which means "refused before any
    # write", and that is not what happened.
    def test_an_outage_reading_the_branch_is_not_taken_for_a_missing_branch(self):
        self.edit_tree()
        fake = FakeGitHub({"b": self.head})
        self.addCleanup(fake.close)
        fake.outage = {"ref"}
        code, _, err = self.run_main("--branch", "b", "--message", "m", fake=fake)
        self.assertEqual(code, 2)
        self.assertIn("could not read b: 502", err)
        self.assertEqual(fake.created_refs, [], "a 502 is not a 404: the branch is never re-created")
        self.assertEqual(fake.mutations, [])

    def test_an_html_error_page_on_the_commit_is_reported_not_raised(self):
        self.edit_tree()
        fake = FakeGitHub({"b": self.head})
        self.addCleanup(fake.close)
        fake.outage = {"graphql"}
        code, _, err = self.run_main("--branch", "b", "--message", "m", fake=fake)
        self.assertEqual(code, 2)
        self.assertIn('"status": 502', err)
        self.assertIn("502 Bad Gateway", err)

    def test_a_json_body_that_is_not_an_object_is_reported_not_raised(self):
        # A proxy can answer `null` or `[]`: valid JSON the callers cannot
        # `.get` from. It must still end in exit 2 and an ::error.
        self.edit_tree()
        fake = FakeGitHub({"b": self.head})
        self.addCleanup(fake.close)
        fake.outage = {"graphql"}
        fake.outage_page = b"null"
        code, _, err = self.run_main("--branch", "b", "--message", "m", fake=fake)
        self.assertEqual(code, 2)
        self.assertIn('"status": 502', err)

    def test_a_success_status_with_a_non_object_body_is_not_verified(self):
        # 200 with `[]`: the commit landed, but nothing confirms it Verified.
        self.edit_tree()
        fake = FakeGitHub({"b": self.head})
        self.addCleanup(fake.close)
        fake.outage, fake.outage_status, fake.outage_page = {"commits"}, 200, b"[]"
        code, out, _ = self.run_main("--branch", "b", "--message", "m", fake=fake)
        self.assertEqual(code, 3)
        self.assertIn("verified: False", out)

    def test_an_outage_reading_the_verification_says_so(self):
        self.edit_tree()
        fake = FakeGitHub({"b": self.head})
        self.addCleanup(fake.close)
        fake.outage = {"commits"}
        code, out, _ = self.run_main("--branch", "b", "--message", "m", fake=fake)
        self.assertEqual(code, 3, "committed, but not confirmed Verified")
        self.assertIn("could not read the commit: 502", out)

    def test_an_unreachable_api_is_reported_not_raised(self):
        self.edit_tree()
        fake = FakeGitHub({"b": self.head})
        fake.close()  # nothing listens on its port any more
        code, _, err = self.run_main("--branch", "b", "--message", "m", fake=fake)
        self.assertEqual(code, 2)
        self.assertIn("GitHub unreachable", err)

    def test_a_commit_that_does_not_descend_from_head_is_not_fast_forwarded(self):
        self.with_origin()
        tree = sh(self.repo, "git", "rev-parse", "HEAD^{tree}")
        orphan = sh(self.repo, "git", "commit-tree", tree, "-m", "unrelated history")
        sh(self.repo, "git", "push", "-q", "-f", "origin", f"{orphan}:refs/heads/main")
        problem = vc.fast_forward(self.repo, "main", self.head, orphan, [])
        self.assertIn("does not descend from", problem)
        self.assertEqual(sh(self.repo, "git", "rev-parse", "HEAD"), self.head)

    def test_the_script_runs_as_a_cli(self):
        p = subprocess.run(["python3", str(SCRIPT), "--repo", "o/r", "--cwd", str(self.repo), "--branch", "b",
                            "--message", "m", "--dry-run"], capture_output=True, text=True)
        self.assertEqual((p.returncode, "nothing to commit" in p.stderr), (1, True))


ACTION = ROOT / ".github/actions/verified-commit/action.yml"


class ActionSteps(Checkout):
    """The action's own shell: how its inputs become verified_commit.py's arguments."""

    def test_the_repository_input_is_split_into_owner_and_name(self):
        r = Runner(self)
        self.assertEqual(r.run(ACTION, "Split the repository name", REPOSITORY="AIRIA-Systems-GmbH/airia-ci").returncode, 0)
        self.assertEqual(r.outputs(), {"owner": "AIRIA-Systems-GmbH", "name": "airia-ci"})

    def commit_step(self, fake, **env):
        r = Runner(self)
        base = {"REPOSITORY": "o/r", "BRANCH": "b", "MESSAGE": "chore: bump\n\nwhy", "PATHS": "",
                "EXPECTED_HEAD": "", "FAST_FORWARD": "false", "GH_TOKEN": "t0ken",
                "GITHUB_API_URL": fake.url,
                "GITHUB_GRAPHQL_URL": fake.url + "/graphql"}
        p = r.run(ACTION, "Commit through the API", cwd=self.repo, **{**base, **env})
        return p, r.outputs()

    def test_paths_one_per_line_blank_lines_dropped(self):
        self.edit_tree()
        fake = FakeGitHub({"b": self.head})
        self.addCleanup(fake.close)
        p, out = self.commit_step(fake, PATHS="\npyproject.toml\n\n  \nnew.md\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        sent = fake.mutations[0]
        self.assertEqual([a["path"] for a in sent["fileChanges"]["additions"]], ["pyproject.toml", "new.md"])
        self.assertEqual(sent["message"]["headline"], "chore: bump")
        self.assertEqual(out["verified"], "true")

    def test_spaces_around_a_path_are_not_part_of_its_name(self):
        # `paths: |` in YAML keeps a trailing space or a tab: "new.md " is not a file.
        self.edit_tree()
        fake = FakeGitHub({"b": self.head})
        self.addCleanup(fake.close)
        p, _ = self.commit_step(fake, PATHS="pyproject.toml \n\t new.md\t\n")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([a["path"] for a in fake.mutations[0]["fileChanges"]["additions"]], ["pyproject.toml", "new.md"])

    def test_no_paths_commits_everything_git_status_reports(self):
        self.edit_tree()
        fake = FakeGitHub({"b": self.head})
        self.addCleanup(fake.close)
        p, _ = self.commit_step(fake)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(sorted(d["path"] for d in fake.mutations[0]["fileChanges"]["deletions"]), ["gone.md", "old.md"])

    def test_expected_head_is_passed_through(self):
        self.edit_tree()
        fake = FakeGitHub({"b": "f" * 40})
        self.addCleanup(fake.close)
        p, _ = self.commit_step(fake, EXPECTED_HEAD="f" * 40)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(fake.mutations[0]["expectedHeadOid"], "f" * 40)

    def test_fast_forward_true_moves_the_checkout(self):
        fake = self.with_origin("b")
        sh(self.repo, "git", "checkout", "-q", "-b", "b")
        self.edit_tree()
        p, _ = self.commit_step(fake, FAST_FORWARD="true")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(sh(self.repo, "git", "rev-parse", "HEAD"), fake.branches["b"])


SMOKE = ROOT / ".github/actions/verified-commit/smoke.sh"

# Answers the two App endpoints smoke.sh calls with curl, and logs every call.
FAKE_CURL = """\
#!/bin/sh
echo "$@" >> "$FAKE_CURL_LOG"
case "$*" in
  */repos/o/r/installation*) echo '{"id": 99}' ;;
  *-X\ POST*/app/installations/99/access_tokens*) echo '{"token": "ghs_fake"}' ;;
  *-X\ DELETE*/installation/token*) ;;
  *) echo "fake curl: unexpected $*" >&2; exit 22 ;;
esac
"""


class Smoke(unittest.TestCase):
    """smoke.sh: the post-setup proof, run against fakes of curl, gh and GitHub."""

    def setUp(self):
        self.r = Runner(self)
        self.key = self.r.tmp / "app.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(self.key), "2048"], check=True, capture_output=True)

    def test_the_jwt_is_an_rs256_app_token_the_key_verifies(self):
        p = self.r.script(SMOKE, "Iv1.client", str(self.key), SMOKE_JWT_ONLY="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        header, payload, signature = p.stdout.strip().split(".")

        def unb64(part):
            return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

        self.assertEqual(json.loads(unb64(header)), {"alg": "RS256", "typ": "JWT"})
        claims = json.loads(unb64(payload))
        self.assertEqual(claims["iss"], "Iv1.client")
        self.assertEqual(claims["exp"] - claims["iat"], 600, "GitHub refuses an App JWT living over ten minutes")
        pub, sig = self.r.tmp / "pub.pem", self.r.tmp / "sig"
        subprocess.run(["openssl", "rsa", "-in", str(self.key), "-pubout", "-out", str(pub)], check=True, capture_output=True)
        sig.write_bytes(unb64(signature))
        verify = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(sig)],
                                input=f"{header}.{payload}".encode(), capture_output=True)
        self.assertEqual(verify.returncode, 0, verify.stdout)

    def test_a_full_run_commits_verified_and_revokes_the_token(self):
        origin = self.r.tmp / "origin"
        origin.mkdir()
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                     ["commit", "-q", "--allow-empty", "-m", "base"]):
            sh(origin, "git", *args)
        head = sh(origin, "git", "rev-parse", "HEAD")
        self.r.rules([[r"^repo clone o/r ", {"text": ""}]])
        # The fake gh's clone: copy the origin to the requested directory.
        self.r.tool("gh", f"#!/bin/sh\n[ \"$1 $2\" = 'repo clone' ] || exit 97\ngit clone -q {origin} \"$4\"\n")
        self.r.tool("curl", FAKE_CURL)
        fake = FakeGitHub({"main": head})
        self.addCleanup(fake.close)
        log = self.r.tmp / "curl.log"
        p = self.r.script(SMOKE, "Iv1.client", str(self.key), "o/r", FAKE_CURL_LOG=str(log),
                          GITHUB_API_URL=fake.url, GITHUB_GRAPHQL_URL=fake.url + "/graphql", GITHUB_OUTPUT="")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("verified: True", p.stdout)
        self.assertIn("PASS. Clean up with: gh api -X DELETE repos/o/r/git/refs/heads/verified-commit-smoke/", p.stdout)
        (sent,) = fake.mutations
        self.assertEqual([a["path"] for a in sent["fileChanges"]["additions"]], [".verified-commit-smoke"])
        self.assertTrue(sent["branch"]["branchName"].startswith("verified-commit-smoke/"))
        calls = log.read_text()
        self.assertIn('"permissions":{"contents":"write"}', calls, "the token is minted contents-only")
        self.assertIn("-X DELETE", calls.splitlines()[-1], "the token is revoked on exit")


if __name__ == "__main__":
    unittest.main()
