# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""What the framework bump job (#94) would move in one recipient. It writes nothing.

    python3 scripts/bump_plan.py --dry-run --recipient AIRIA-Systems-GmbH/AIRIA-process \\
        --pyproject pyproject.toml --lock uv.lock

Reads the latest release of each airia-* package the recipient already declares
from the internal simple index, and the recipient's pyproject.toml and uv.lock,
and prints per package: locked version, target, and whether it is held. Rules:

- Only a name declared in the root pyproject.toml (dependencies, optional
  dependencies, dependency groups) moves. A name the index also serves but the
  recipient does not declare is never added; a name in `not_moved` of
  bump-recipients.toml is left alone.
- The target is the highest release that is not yanked and not a pre-release,
  inside every upper bound (the recipient's own specifier and its `holds`).
  A bound is never edited: a package the bound keeps below the newest release
  is reported as held.
- Nothing lowers a version: a target under the locked version is kept as is.
- An index answer that is not a package list for the package fails the run
  (exit 2); it never reads as "nothing to bump".

No `uv lock`, no git, no GitHub: `--dry-run` is the only mode.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import urllib.request
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple

INDEX = "https://pypi.de.airia.systems/simple/"
RECIPIENTS = Path(__file__).resolve().parent.parent / "bump-recipients.toml"
FILE = re.compile(r"^(?P<name>.+?)-(?P<version>\d[^-]*?)(?:-.+\.whl|\.tar\.gz|\.zip)$")
FINAL = re.compile(r"^\d+(?:\.\d+)*$")
REQUIREMENT = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?P<spec>[^;]*?)\s*(?:;.*)?$")
CLAUSE = re.compile(r"^(==|>=|<=|<|>)\s*(\d+(?:\.\d+)*)$")


class PlanError(Exception):
    """The plan cannot be trusted: say so, never print a partial one."""


class Row(NamedTuple):
    package: str
    current: str
    target: str
    state: str  # bump | current | kept | not-moved
    why: str


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def key(version: str) -> tuple[int, ...]:
    """Sort key of a final release; trailing zeros do not count (0.8 == 0.8.0)."""
    parts = [int(p) for p in version.split(".")]
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


class Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.files: list[tuple[str, bool]] = []  # (file name, yanked)
        self._yanked: bool | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._yanked = "data-yanked" in dict(attrs)

    def handle_data(self, data):
        if self._yanked is not None and data.strip():
            self.files.append((data.strip(), self._yanked))
            self._yanked = None


def fetch(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - the internal index
            return r.read().decode()
    except (OSError, ValueError) as exc:
        raise PlanError(f"cannot read {url}: {exc}") from exc


def releases(package: str, get: Callable[[str], str] = fetch, index: str = INDEX) -> list[str]:
    """Every release of `package` that is not yanked and not a pre-release, oldest first."""
    url = f"{index.rstrip('/')}/{norm(package)}/"
    page = Anchors()
    page.feed(get(url))
    found: dict[str, list[bool]] = {}
    for name, yanked in page.files:
        m = FILE.match(name)
        if m and norm(m["name"]) == norm(package) and FINAL.match(m["version"]):
            found.setdefault(m["version"], []).append(yanked)
        elif m is None:
            raise PlanError(f"{url} lists {name!r}, which is not a wheel or sdist file name")
    if not page.files:
        raise PlanError(f"{url} lists no files for {package}: not a package list")
    live = sorted((v for v, flags in found.items() if not all(flags)), key=key)
    if not live:
        raise PlanError(f"{url} has no released, unyanked version of {package}")
    return live


def declared(pyproject: dict) -> dict[str, list[str]]:
    """{airia-* name: [specifier, ...]} over dependencies, optional dependencies and groups."""
    project = pyproject.get("project", {})
    lines = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        lines += extra
    for group in pyproject.get("dependency-groups", {}).values():
        lines += [g for g in group if isinstance(g, str)]  # {include-group = ...} is not a requirement
    found: dict[str, list[str]] = {}
    for line in lines:
        m = REQUIREMENT.match(line)
        if m is None:
            raise PlanError(f"cannot read the requirement {line!r}")
        if norm(m["name"]).startswith("airia-"):
            found.setdefault(norm(m["name"]), []).append(m["spec"])
    return found


def ceilings(specs: list[str], hold: dict | None) -> list[tuple[str, str]]:
    """[(operator, version)] upper bounds from the specifiers and the recipient's hold."""
    clauses = [c.strip() for spec in specs + ([hold["ceiling"]] if hold else []) for c in spec.split(",") if c.strip()]
    out = []
    for c in clauses:
        m = CLAUSE.match(c)
        if m is None:
            raise PlanError(f"unsupported specifier clause {c!r}: the planner reads ==, >=, >, <, <=")
        if m[1] in ("<", "<="):
            out.append((m[1], m[2]))
    return out


def allowed(version: str, bounds: list[tuple[str, str]]) -> bool:
    return all(key(version) < key(v) if op == "<" else key(version) <= key(v) for op, v in bounds)


def plan(recipient: dict, pyproject_text: str | None, lock_text: str | None,
         get: Callable[[str], str] = fetch, index: str = INDEX) -> tuple[list[Row], list[str]]:
    """(rows, notes) for one recipient; no row for a package it does not declare."""
    if pyproject_text is None:
        return [], ["no root pyproject.toml: nothing to bump"]
    wanted = declared(tomllib.loads(pyproject_text))
    if not wanted:
        return [], ["no airia-* declaration: nothing to bump"]
    if lock_text is None:
        raise PlanError("uv.lock is missing: the locked versions are the baseline of the plan")
    lock = {norm(p["name"]): p.get("version", "") for p in tomllib.loads(lock_text).get("package", [])}
    not_moved = recipient.get("not_moved", {})
    holds = recipient.get("holds", {})
    expected = set(recipient.get("packages", [])) | set(not_moved)
    notes = [f"{n} is declared but not in bump-recipients.toml: list it or put it in not_moved"
             for n in sorted(set(wanted) - expected)]
    notes += [f"{n} is listed in bump-recipients.toml but not declared in pyproject.toml"
              for n in sorted(expected - set(wanted))]
    rows = []
    for name in sorted(wanted):
        if name not in lock:
            raise PlanError(f"uv.lock has no {name}, which pyproject.toml declares")
        current = lock[name]
        if name in not_moved:
            rows.append(Row(name, current, current, "not-moved", not_moved[name]))
            continue
        if any(s.startswith("@") for s in wanted[name]):
            rows.append(Row(name, current, current, "not-moved", "bound to a direct reference"))
            continue
        every = releases(name, get, index)
        bounds = ceilings(wanted[name], holds.get(name))
        inside = [v for v in every if allowed(v, bounds)]
        target = inside[-1] if inside else current
        why = ""
        if inside and inside[-1] != every[-1]:
            held = ",".join(f"{op}{v}" for op, v in bounds)
            why = f"held at {held}: newest is {every[-1]}"
        if key(target) < key(current):
            rows.append(Row(name, current, current, "kept",
                            f"the index offers {target} under the locked {current}; never lowered"))
        elif key(target) == key(current):
            rows.append(Row(name, current, target, "current", why))
        else:
            rows.append(Row(name, current, target, "bump", why))
    return rows, notes


def render(repo: str, rows: list[Row], notes: list[str]) -> str:
    out = [repo, *[f"  note: {n}" for n in notes]]
    out += [f"  {r.package}\t{r.current}\t{r.target}\t{r.state}\t{r.why}".rstrip("\t") for r in rows]
    moves = sum(r.state == "bump" for r in rows)
    out.append(f"  {moves} to move, {sum(bool(r.why) for r in rows)} held")
    return "\n".join(out)


def recipients(path: Path = RECIPIENTS) -> dict:
    return tomllib.loads(path.read_text())


def unlisted(repos: list[str], listed: dict | None = None) -> list[str]:
    """Repositories (say, from `consumers.py`) that are neither a recipient nor excluded with a reason."""
    listed = listed or recipients()
    known = {r["repo"] for r in listed["recipient"]} | {e["repo"] for e in listed["excluded"]}
    return sorted(set(repos) - known)


def main(argv: list[str] | None = None, get: Callable[[str], str] = fetch) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", required=True, help="the only mode: print the plan, write nothing")
    ap.add_argument("--recipient", required=True, help="owner/name as listed in bump-recipients.toml")
    ap.add_argument("--pyproject", type=Path, required=True, help="the recipient's root pyproject.toml")
    ap.add_argument("--lock", type=Path, required=True, help="the recipient's uv.lock")
    ap.add_argument("--index", default=INDEX)
    args = ap.parse_args(argv)
    try:
        found = [r for r in recipients()["recipient"] if r["repo"] == args.recipient]
        if not found:
            raise PlanError(f"{args.recipient} is not in bump-recipients.toml")
        rows, notes = plan(found[0],
                           args.pyproject.read_text() if args.pyproject.is_file() else None,
                           args.lock.read_text() if args.lock.is_file() else None,
                           get, args.index)
    except PlanError as exc:
        print(f"error: {exc}; nothing was planned", file=sys.stderr)
        return 2
    print(render(args.recipient, rows, notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
