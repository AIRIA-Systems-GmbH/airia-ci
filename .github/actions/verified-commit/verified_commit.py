# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Commit a working tree's changes to a GitHub branch as a Verified commit.

The commit is made by GitHub's `createCommitOnBranch` GraphQL mutation, so no
local signing key is involved. Authenticated as a GitHub App installation,
GitHub signs it ("Verified"): the mutation takes no author, committer or
signature, which is the condition GitHub sets for signing a bot's API commit.

    GH_TOKEN=... python3 verified_commit.py --repo OWNER/NAME --branch B \
        --message-file msg.txt [--paths auto|FILE...] [--fast-forward] [--dry-run]

With --fast-forward the checkout's own ref then moves to the new commit
(fetched from `origin`, which must be --repo), never anywhere else: only when
origin's branch is exactly that commit and it descends from the old HEAD. The
working tree must then match it for every committed path.

Exit codes: 0 committed and Verified (or dry run) · 1 refused before any
write (nothing to commit, unsupported file, bad input) · 2 the API refused or
failed the branch or the commit (e.g. the branch moved since HEAD, an outage) ·
3 committed but NOT confirmed Verified ·
4 committed and Verified, but the checkout could not be fast-forwarded to it.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

MUTATION = """
mutation ($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid url } }
}
"""


class Refused(Exception):
    """Refused before anything was written."""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def changed_paths(root: Path) -> tuple[list[str], list[str]]:
    """The working tree's changes against HEAD: (paths to write, paths to delete)."""
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = out.split("\0")
    writes, deletes = [], []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            # -z puts a rename's source in the next entry
            source = entries[i]
            i += 1
            if "R" in code:
                deletes.append(source)
        if "D" in code:
            deletes.append(path)
        else:
            writes.append(path)
    return writes, deletes


def file_changes(root: Path, writes: list[str], deletes: list[str]) -> dict:
    additions = []
    for rel in writes:
        path = root / rel
        mode = path.lstat().st_mode
        # The mutation has no file-mode field: everything it writes is 100644.
        if stat.S_ISLNK(mode):
            raise Refused(f"{rel} is a symlink; createCommitOnBranch cannot write one")
        if not stat.S_ISREG(mode):
            raise Refused(f"{rel} is not a regular file")
        if mode & 0o111:
            raise Refused(f"{rel} is executable; createCommitOnBranch would drop the bit")
        additions.append(
            {"path": rel, "contents": base64.b64encode(path.read_bytes()).decode()}
        )
    return {"additions": additions, "deletions": [{"path": p} for p in deletes]}


def split_message(text: str) -> dict:
    headline, _, body = text.strip().partition("\n")
    if not headline.strip():
        raise Refused("the commit message is empty")
    return {"headline": headline.strip(), "body": body.strip()}


def _json(raw: bytes) -> dict:
    """A response body as JSON, or its text as the message when it is not JSON."""
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {"message": raw.decode(errors="replace").strip()[:300]}


class GitHub:
    def __init__(self, token: str) -> None:
        self.token = token
        self.api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.graphql_url = os.environ.get("GITHUB_GRAPHQL_URL", self.api + "/graphql")

    def request(self, method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
        )
        # Never raises: an outage answers with an HTML page or nothing at all,
        # and the caller reports the status like any other refusal.
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, _json(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _json(exc.read())
        except urllib.error.URLError as exc:
            return 0, {"message": f"GitHub unreachable: {exc.reason}"}

    def branch_head(self, repo: str, branch: str) -> str | None:
        status, data = self.request("GET", f"{self.api}/repos/{repo}/git/ref/heads/{branch}")
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"could not read {branch}: {status} {data}")
        return data["object"]["sha"]

    def create_branch(self, repo: str, branch: str, sha: str) -> None:
        status, data = self.request(
            "POST", f"{self.api}/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}
        )
        if status != 201:
            raise RuntimeError(f"could not create {branch} at {sha}: {status} {data}")

    def create_commit(self, variables: dict) -> tuple[dict | None, list]:
        status, data = self.request("POST", self.graphql_url, {"query": MUTATION, "variables": variables})
        commit = ((data.get("data") or {}).get("createCommitOnBranch") or {}).get("commit")
        # A 401/403 has no `errors`, only a `message`: report that, not `[]`.
        return commit, data.get("errors") or [{"status": status, "message": data.get("message")}]

    def verification(self, repo: str, sha: str) -> dict:
        status, data = self.request("GET", f"{self.api}/repos/{repo}/commits/{sha}")
        if status != 200:
            return {"verified": False, "reason": f"could not read the commit: {status} {data.get('message')}"}
        return (data.get("commit") or {}).get("verification") or {}


def check_fast_forward(root: Path, branch: str, head: str) -> None:
    """Refuse, before any write, a checkout whose ref cannot follow the commit."""
    current = subprocess.run(["git", "symbolic-ref", "-q", "--short", "HEAD"],
                             cwd=root, capture_output=True, text=True, check=False).stdout.strip()
    if current and current != branch:
        raise Refused(f"--fast-forward: the checkout is on {current}, not {branch}")
    if git(root, "rev-parse", "HEAD").strip() != head:
        raise Refused("--fast-forward: the checkout is not at --expected-head")


def fast_forward(root: Path, branch: str, head: str, oid: str, paths: list[str]) -> str | None:
    """Move the checkout to `oid`; return why not, or None."""
    git(root, "fetch", "-q", "origin", f"refs/heads/{branch}")
    fetched = git(root, "rev-parse", "FETCH_HEAD").strip()
    if fetched != oid:
        return f"origin/{branch} is at {fetched}, not {oid}: it moved again; the checkout stays at {head}"
    if subprocess.run(["git", "merge-base", "--is-ancestor", head, oid], cwd=root, check=False).returncode:
        return f"{oid} does not descend from {head}; the checkout stays there"
    git(root, "reset", "-q", "--mixed", oid)
    drift = git(root, "status", "--porcelain", "--untracked-files=all", "--", *paths)
    if drift.strip():
        return f"{oid} differs from the working tree:\n{drift}"
    return None


def set_output(**values: str) -> None:
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as fh:
            fh.writelines(f"{key}={value}\n" for key, value in values.items())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="OWNER/NAME")
    ap.add_argument("--branch", required=True, help="created at --expected-head if it does not exist")
    msg = ap.add_mutually_exclusive_group(required=True)
    msg.add_argument("--message", help="first line is the headline, the rest the body")
    msg.add_argument("--message-file")
    ap.add_argument("--paths", nargs="*", default=["auto"],
                    help="files to commit, relative to the repository root; `auto` (default) takes "
                         "every change `git status` reports, deletions and renames included")
    ap.add_argument("--delete", nargs="*", default=[], help="files to delete (with explicit --paths)")
    ap.add_argument("--expected-head", help="the commit the change was made on (default: local HEAD); "
                    "the commit is refused if the branch has moved past it")
    ap.add_argument("--cwd", default=".", help="any directory inside the checkout")
    ap.add_argument("--fast-forward", action="store_true",
                    help="then move the checkout's ref to the new commit (see above)")
    ap.add_argument("--dry-run", action="store_true", help="print the request; send nothing")
    args = ap.parse_args(argv)

    try:
        root = Path(git(Path(args.cwd), "rev-parse", "--show-toplevel").strip())
        head = args.expected_head or git(root, "rev-parse", "HEAD").strip()
        if args.paths == ["auto"]:
            writes, deletes = changed_paths(root)
        else:
            writes, deletes = list(args.paths), list(args.delete)
        if not writes and not deletes:
            raise Refused("nothing to commit")
        if args.fast_forward:
            check_fast_forward(root, args.branch, head)
        text = args.message if args.message is not None else Path(args.message_file).read_text()
        variables = {"input": {
            "branch": {"repositoryNameWithOwner": args.repo, "branchName": args.branch},
            "message": split_message(text),
            "expectedHeadOid": head,
            "fileChanges": file_changes(root, writes, deletes),
        }}
    except (Refused, OSError, subprocess.CalledProcessError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    fc = variables["input"]["fileChanges"]
    print(f"{args.repo}@{args.branch} on {head[:12]}: "
          f"{len(fc['additions'])} written, {len(fc['deletions'])} deleted")
    for a in fc["additions"]:
        print(f"  + {a['path']}")
    for d in fc["deletions"]:
        print(f"  - {d['path']}")
    if args.dry_run:
        shown = json.loads(json.dumps(variables))
        for a in shown["input"]["fileChanges"]["additions"]:
            a["contents"] = f"<{len(a['contents'])} base64 chars>"
        print(json.dumps(shown, indent=2))
        return 0

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("refused: no GH_TOKEN or GITHUB_TOKEN in the environment", file=sys.stderr)
        return 1
    gh = GitHub(token)
    try:
        if gh.branch_head(args.repo, args.branch) is None:
            gh.create_branch(args.repo, args.branch, head)
            print(f"created {args.branch} at {head[:12]}")
    except RuntimeError as exc:
        print(f"::error title=Branch refused::{exc}"[:900], file=sys.stderr)
        return 2
    commit, errors = gh.create_commit(variables)
    if not commit:
        print(f"::error title=Commit refused::{json.dumps(errors)[:900]}", file=sys.stderr)
        return 2
    v = gh.verification(args.repo, commit["oid"])
    verified = bool(v.get("verified"))
    set_output(sha=commit["oid"], url=commit["url"], verified=str(verified).lower())
    print(f"commit {commit['oid']} {commit['url']}")
    print(f"verified: {verified} ({v.get('reason')})")
    if not verified:
        print(f"::error title=Commit not Verified::{commit['oid']} landed on {args.branch} but "
              f"GitHub reports it unverified ({v.get('reason')}). Authenticate with a GitHub App "
              "installation token.", file=sys.stderr)
        return 3
    if args.fast_forward:
        problem = fast_forward(root, args.branch, head, commit["oid"], writes + deletes)
        if problem:
            print(f"::error title=Checkout not fast-forwarded::{problem}", file=sys.stderr)
            return 4
        print(f"fast-forwarded the checkout to {commit['oid'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
