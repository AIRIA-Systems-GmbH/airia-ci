# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The bump plan of every recipient, read through `gh` (#94, dry run). It writes nothing to any repository.

    python3 scripts/bump_dry_run.py --recipients bump-recipients.toml --index https://.../simple/

For each [[recipient]] it reads the default branch's root pyproject.toml and
uv.lock with `gh api` (GH_TOKEN, read access only), plans with bump_plan.plan
against the index, prints the plan and appends it to $GITHUB_STEP_SUMMARY. It
sets the outputs `recipients`, `moves`, `failed` and `plan` (one line of JSON)
in $GITHUB_OUTPUT.

A recipient whose repository the token cannot read, or whose plan cannot be
trusted (index unreadable, a lock that misses a declared package), is reported
as such and the run exits 1 once every recipient has been tried: a plan that
silently skipped a recipient would read as "nothing to bump". A repository the
token can read with no root pyproject.toml is not an error (it has nothing to
bump); a 404 on the repository itself is, because GitHub answers 404 for a
private repository the token cannot see.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import bump_plan

RAW = "Accept: application/vnd.github.raw"


class Unreadable(Exception):
    """The repository, or a file in it, cannot be read with this token."""


def gh(*args: str) -> str:
    done = subprocess.run(["gh", *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise Unreadable(done.stderr.strip() or f"gh {args[0]} exited {done.returncode}")
    return done.stdout


def manifest(repo: str, path: str) -> str | None:
    """The file on the default branch, or None when the repository has no such file."""
    try:
        return gh("api", f"repos/{repo}/contents/{path}", "-H", RAW)
    except Unreadable as exc:
        if "HTTP 404" in str(exc):
            return None
        raise


def one(recipient: dict, index: str) -> dict:
    repo = recipient["repo"]
    try:
        gh("api", f"repos/{repo}", "--jq", ".default_branch")  # 404 here: the token cannot see it
        pyproject, lock = manifest(repo, "pyproject.toml"), manifest(repo, "uv.lock")
    except Unreadable as exc:
        return {"repo": repo, "state": "not-reachable", "why": str(exc)}
    try:
        rows, notes = bump_plan.plan(recipient, pyproject, lock, index=index)
    except bump_plan.PlanError as exc:
        return {"repo": repo, "state": "failed", "why": str(exc)}
    return {"repo": repo, "state": "planned", "notes": notes, "rows": [r._asdict() for r in rows],
            "text": bump_plan.render(repo, rows, notes)}


def report(results: list[dict]) -> str:
    out = ["## Bump plan (dry run: nothing was written anywhere)", ""]
    for r in results:
        if r["state"] == "planned":
            out += ["```", r["text"], "```"]
        else:
            out.append(f"- **{r['repo']}**: {r['state']}: {r['why']}")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recipients", type=Path, default=bump_plan.RECIPIENTS)
    ap.add_argument("--index", default=bump_plan.INDEX)
    args = ap.parse_args(argv)
    results = [one(r, args.index) for r in bump_plan.recipients(args.recipients)["recipient"]]
    text = report(results)
    print(text)
    failed = [r for r in results if r["state"] != "planned"]
    moves = sum(row["state"] == "bump" for r in results for row in r.get("rows", []))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(text)
    if output := os.environ.get("GITHUB_OUTPUT"):
        plan = [{k: v for k, v in r.items() if k != "text"} for r in results]
        with open(output, "a") as f:
            f.write(f"recipients={len(results)}\nmoves={moves}\nfailed={len(failed)}\nplan={json.dumps(plan)}\n")
    for r in failed:
        print(f"::error title=Bump plan::{r['repo']} is {r['state']}: {r['why']}".replace("\n", " "), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
