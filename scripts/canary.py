# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Prove `@claude` answers on a consumer after a release.

    python3 scripts/canary.py --repo AIRIA-Systems-GmbH/AIRIA-DevTools --pr 23

Posts an `@claude` comment on an open pull request, as whoever `gh` is logged
in as, and waits for a reply carrying a token unique to this run. It has to be
a person: a comment made with a workflow's GITHUB_TOKEN starts no workflow, and
the responder refuses anyone without write access before it runs. No pull
request runs the responder, because it runs from the default branch, so this
is the only test of it before a consumer's first real `@claude`.

Exit 0: the reply arrived. 1: none before the timeout (the latest `claude.yml`
run is reported). 2: the comment could not be posted or read.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


def post(repo: str, pr: str, token: str) -> int:
    body = (f"@claude release canary: reply on this pull request with exactly `CANARY-OK {token}` "
            "and nothing else. Do not change, commit or push anything.")
    p = gh("pr", "comment", pr, "--repo", repo, "--body", body)
    found = re.search(r"#issuecomment-(\d+)", p.stdout)
    if p.returncode or not found:
        raise RuntimeError(f"could not comment on {repo}#{pr}: {p.stderr.strip() or p.stdout.strip()}")
    return int(found.group(1))


def replied(repo: str, pr: str, after: int, token: str) -> bool:
    p = gh("api", f"repos/{repo}/issues/{pr}/comments", "--paginate",
           "--jq", f".[] | select(.id > {after}) | .body")
    if p.returncode:
        raise RuntimeError(f"could not read {repo}#{pr}'s comments: {p.stderr.strip()}")
    return f"CANARY-OK {token}" in p.stdout


def latest_run(repo: str) -> str:
    p = gh("run", "list", "--repo", repo, "--workflow", "claude.yml", "--limit", "1",
           "--json", "status,conclusion,url")
    runs = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else []
    if not runs:
        return "no claude.yml run found"
    run = runs[0]
    return f"latest claude.yml run: {run['status']} {run.get('conclusion') or ''} {run['url']}".replace("  ", " ")


def main(argv: list[str] | None = None, sleep=time.sleep) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="OWNER/NAME of a consumer")
    ap.add_argument("--pr", required=True, help="an open pull request to comment on")
    ap.add_argument("--timeout", type=int, default=900, help="seconds to wait for the reply")
    ap.add_argument("--interval", type=int, default=20, help="seconds between looks")
    args = ap.parse_args(argv)
    token = uuid.uuid4().hex[:12]
    try:
        comment = post(args.repo, args.pr, token)
        print(f"asked @claude on {args.repo}#{args.pr} (token {token}); waiting up to {args.timeout}s")
        waited = 0
        while not replied(args.repo, args.pr, comment, token):
            if waited >= args.timeout:
                print(f"::error title=Canary unanswered::no reply on {args.repo}#{args.pr} "
                      f"after {args.timeout}s; {latest_run(args.repo)}", file=sys.stderr)
                return 1
            sleep(args.interval)
            waited += args.interval
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"@claude answered on {args.repo}#{args.pr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
