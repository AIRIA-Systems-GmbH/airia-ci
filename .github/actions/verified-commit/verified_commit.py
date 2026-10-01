# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Commit a working tree's changes to a GitHub branch as a Verified commit.

The commit is made by GitHub's `createCommitOnBranch` GraphQL mutation, so no
local signing key is involved. Authenticated as a GitHub App installation,
GitHub signs it ("Verified"): the mutation takes no author, committer or
signature, which is the condition GitHub sets for signing a bot's API commit.

    GH_TOKEN=... python3 verified_commit.py --repo OWNER/NAME --branch B \
        --message-file msg.txt [--paths auto|FILE...] [--dry-run]

Exit codes: 0 committed and Verified (or dry run) · 1 refused before any
write (nothing to commit, unsupported file, bad input) · 2 the API refused the
commit (e.g. the branch moved since HEAD) · 3 committed but NOT Verified.
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
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def branch_head(self, repo: str, branch: str) -> str | None:
        status, data = self.request("GET", f"{self.api}/repos/{repo}/git/ref/heads/{branch}")
        return data["object"]["sha"] if status == 200 else None

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
        _, data = self.request("GET", f"{self.api}/repos/{repo}/commits/{sha}")
        return (data.get("commit") or {}).get("verification") or {}


def set_output(**values: str) -> None:
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as fh:
            for key, value in values.items():
                fh.write(f"{key}={value}\n")


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
    if gh.branch_head(args.repo, args.branch) is None:
        gh.create_branch(args.repo, args.branch, head)
        print(f"created {args.branch} at {head[:12]}")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
