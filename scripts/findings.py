# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The automatic review's findings that nobody has answered yet.

    python3 scripts/findings.py --repo OWNER/NAME --pr N

Prints one line per open finding (id, file, link) and exits 1 if any is left,
0 if none is. A finding is a PR review comment by a Bot user that carries the
`<!-- airia-finding run=<run> id=<id> file=<path> -->` marker review-post
writes. It is open while no comment replies to it (`in_reply_to_id` is its id)
and its thread is not resolved. Another user's comment that quotes the marker
is not a finding. No model runs here: this is the check behind "every bot
finding has a reply", the rule a PR at the review cap merges by (airia-ci#71,
#73). Run it with a `gh` login that can read the PR; exit 2 means a lookup
failed, which is never the same as "no findings left".

reusable-claude-review.yml's cap comment lists the same findings with the same
rule in jq, because that step runs in the caller's repository, where this file
is not checked out; tests/test_review_steps.py holds the two to one answer.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

MARKER = re.compile(r"<!-- airia-finding run=\d+ id=(\S+) file=(\S+) -->")
THREADS = """
query($owner: String!, $name: String!, $pr: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $endCursor) {
        nodes { isResolved comments(first: 100) { nodes { databaseId } } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


class LookupFailed(Exception):
    """A gh call failed; the answer would be a guess."""


def gh_json(*args: str) -> list:
    p = subprocess.run(["gh", "api", "--paginate", "--slurp", *args], capture_output=True, text=True, check=False)
    if p.returncode:
        raise LookupFailed(f"gh api {args[-1] if args[0] != 'graphql' else 'graphql'} failed: {p.stderr.strip()}")
    return json.loads(p.stdout)


def resolved_comment_ids(repo: str, pr: int) -> set[int]:
    owner, name = repo.split("/", 1)
    pages = gh_json("graphql", "-f", f"query={THREADS}", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"pr={pr}")
    return {
        c["databaseId"]
        for page in pages
        for t in page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"] if t["isResolved"]
        for c in t["comments"]["nodes"]
    }


def open_findings(repo: str, pr: int) -> list[dict]:
    pages = gh_json(f"repos/{repo}/pulls/{pr}/comments")
    comments = [c for page in pages for c in page]
    replied = {c["in_reply_to_id"] for c in comments if c.get("in_reply_to_id")}
    resolved = resolved_comment_ids(repo, pr)
    found = []
    for c in comments:
        m = MARKER.search(c.get("body") or "")
        if not m or (c.get("user") or {}).get("type") != "Bot":
            continue
        if c["id"] in replied or c["id"] in resolved:
            continue
        found.append({"id": m.group(1), "file": m.group(2), "url": c["html_url"]})
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="OWNER/NAME")
    ap.add_argument("--pr", required=True, type=int)
    args = ap.parse_args(argv)
    try:
        found = open_findings(args.repo, args.pr)
    except LookupFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for f in found:
        print(f"{f['id']}\t{f['file']}\t{f['url']}")
    print(f"{len(found)} open finding(s) on {args.repo}#{args.pr}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
