# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Every repository that calls airia-ci, and whether its pin is the newest major.

    python3 scripts/consumers.py AIRIA-Systems-GmbH tumma72

Run it with your own `gh` login at release time: it reads private repositories
of every owner given, which no token in this repository can. A pin older than
the newest `ci-vN` is frozen, so moving the tag never reaches it; such a
repository is listed as BEHIND and the exit code is 1. AIRIA-DevTools and
AIRIA-process sat on the frozen `ci-v2` unnoticed until 2026-10-05.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

HARNESS = "AIRIA-Systems-GmbH/airia-ci"
USES = re.compile(re.escape(HARNESS) + r"/(\S+?)@(\S+)")
MAJOR = re.compile(r"^ci-v(\d+)$")


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=False)


def newest_major() -> str:
    p = gh("api", f"repos/{HARNESS}/tags", "--paginate", "--jq", ".[].name")
    if p.returncode:
        raise RuntimeError(f"cannot list {HARNESS} tags: {p.stderr.strip()}")
    majors = [int(m.group(1)) for m in map(MAJOR.match, p.stdout.split()) if m]
    if not majors:
        raise RuntimeError(f"{HARNESS} has no ci-vN tag")
    return f"ci-v{max(majors)}"


def repositories(owner: str) -> list[str]:
    p = gh("repo", "list", owner, "--no-archived", "--limit", "1000", "--json", "nameWithOwner")
    if p.returncode:
        raise RuntimeError(f"cannot list {owner}'s repositories: {p.stderr.strip()}")
    return sorted(r["nameWithOwner"] for r in json.loads(p.stdout))


def pins(repo: str) -> list[tuple[str, str, str]]:
    """(workflow file, called path, ref) for every airia-ci reference in the repo's workflows."""
    p = gh("api", f"repos/{repo}/contents/.github/workflows", "--jq", ".[].path")
    if p.returncode:  # no .github/workflows (or an empty repository)
        return []
    found = []
    for path in p.stdout.split():
        f = gh("api", f"repos/{repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw")
        if f.returncode:
            raise RuntimeError(f"cannot read {repo}/{path}: {f.stderr.strip()}")
        found += [(path, called, ref) for called, ref in USES.findall(f.stdout)]
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("owners", nargs="+", help="organizations or users whose repositories to scan")
    args = ap.parse_args(argv)
    try:
        newest = newest_major()
        rows = [(repo, *pin) for owner in args.owners for repo in repositories(owner)
                if repo != HARNESS for pin in pins(repo)]
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    behind = 0
    for repo, path, called, ref in rows:
        state = "current" if ref == newest else f"BEHIND (newest is {newest})"
        behind += ref != newest
        print(f"{repo}\t{path}\t{called.rsplit('/', 1)[-1]}@{ref}\t{state}")
    print(f"{len({r[0] for r in rows})} consumers, {len(rows)} pins, {behind} behind {newest}")
    return 1 if behind else 0


if __name__ == "__main__":
    sys.exit(main())
