# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""One coverage figure for everything consumers execute, Python and bash together.

    python3 tests/coverage_total.py --floor 95

Python is coverage.py's statements + branches (build/coverage/python.xml);
bash is kcov's lines (build/coverage/kcov/). The total is covered / valid over
both, the way coverage.py totals statements and branches. It fails when:

- a shipped script or extracted block is missing from either report (a file no
  one measured must not drop out of the figure), or
- the total is under the floor.

One correction to kcov, and it only removes false misses: kcov counts a line
that STARTS INSIDE a multi-line quoted string (the body of a jq filter, say)
as an executable bash line, and never marks it hit, because bash reports the
command at the line where it starts. Those lines are dropped from the bash
figure. Nothing else is adjusted.

Two more kcov blind spots are known and deliberately NOT corrected, so they
only ever understate the figure: the first line of a command continued with a
backslash (kcov credits a later line), and the closing line of a compound command
with a redirect (`} > file`, `done < file`). Each was checked against the
neighbouring hits when the gate was introduced: the command had run.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COV = ROOT / "build/coverage"
SRC = COV / "src"


def shipped() -> tuple[set[Path], set[Path]]:
    """Every file the figure must include: (bash, python)."""
    sh = set(SRC.glob("*.sh")) | set((ROOT / ".github").rglob("*.sh"))
    py = set(SRC.glob("*.py")) | set((ROOT / ".github").rglob("*.py"))
    return {p.resolve() for p in sh}, {p.resolve() for p in py}


def lines_inside_quotes(text: str) -> set[int]:
    """1-based numbers of the lines that begin inside an open '...' or "..." string."""
    inside, stack, heredoc, pending = set(), [], None, None
    lines = text.split("\n")
    for number, line in enumerate(lines, 1):
        if heredoc is not None:
            if (line.strip() if heredoc[1] else line) == heredoc[0]:
                heredoc = None
            continue
        if stack and stack[-1][0] in ("sq", "dq"):
            inside.add(number)
        i = 0
        while i < len(line):
            c, top = line[i], stack[-1][0] if stack else "top"
            if top == "sq":
                if c == "'":
                    stack.pop()
            elif c == "\\":
                i += 1
            elif top == "dq" and c == '"':
                stack.pop()
            elif line.startswith("$(", i):
                stack.append(["cs", 0])
                i += 1
            elif top == "dq":
                pass
            elif c == "'":
                stack.append(["sq", 0])
            elif c == '"':
                stack.append(["dq", 0])
            elif c == "#" and (i == 0 or line[i - 1] in " \t;|&("):
                break
            elif line.startswith("<<", i) and not line.startswith("<<<", i):
                m = re.match(r"<<(-?)\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line[i:])
                if m:
                    pending = (m.group(2), m.group(1) == "-")
                    i += m.end() - 1
            elif top == "cs" and c == "(":
                stack[-1][1] += 1
            elif top == "cs" and c == ")":
                if stack[-1][1]:
                    stack[-1][1] -= 1
                else:
                    stack.pop()
            i += 1
        if pending:
            heredoc, pending = pending, None
    return inside


def python_report(path: Path) -> dict[Path, tuple[int, int]]:
    """{file: (covered, valid)} counting statements and branch outcomes."""
    out = {}
    root = ET.parse(path).getroot()
    # One <source> per .coveragerc root; each filename is relative to its own.
    bases = [Path(s.text) for s in root.iter("source")] or [ROOT]
    for cls in root.iter("class"):
        covered = valid = 0
        for line in cls.iter("line"):
            valid += 1
            covered += int(line.get("hits")) > 0
            m = re.search(r"\((\d+)/(\d+)\)", line.get("condition-coverage") or "")
            if m:
                covered, valid = covered + int(m.group(1)), valid + int(m.group(2))
        found = [b / cls.get("filename") for b in bases if (b / cls.get("filename")).exists()]
        out[(found[0] if found else bases[0] / cls.get("filename")).resolve()] = (covered, valid)
    return out


def bash_report(kcov: Path) -> dict[Path, tuple[int, int]]:
    """{file: (covered, valid)} over every kcov run, a line hit in any run counting as hit."""
    hits: dict[Path, dict[int, int]] = {}
    for report in kcov.rglob("cobertura.xml"):
        root = ET.parse(report).getroot()
        base = Path(root.findtext("sources/source") or "/")
        for cls in root.iter("class"):
            lines = hits.setdefault((base / cls.get("filename")).resolve(), {})
            for line in cls.iter("line"):
                n = int(line.get("number"))
                lines[n] = max(lines.get(n, 0), int(line.get("hits")))
    out = {}
    for file, lines in hits.items():
        drop = lines_inside_quotes(file.read_text()) if file.exists() else set()
        kept = {n: h for n, h in lines.items() if n not in drop}
        out[file] = (sum(1 for h in kept.values() if h), len(kept))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=float, required=True)
    args = ap.parse_args()

    py = python_report(COV / "python.xml")
    sh = bash_report(COV / "kcov")
    want_sh, want_py = shipped()
    missing = sorted((want_sh - sh.keys()) | (want_py - py.keys()))
    rows = [("bash", f, *sh[f]) for f in sorted(want_sh & sh.keys())] + \
           [("python", f, *py[f]) for f in sorted(want_py & py.keys())]

    width = max((len(str(f.relative_to(ROOT))) for _, f, _, _ in rows), default=0)
    for kind, f, covered, valid in rows:
        pct = 100.0 * covered / valid if valid else 100.0
        print(f"{kind:6}  {str(f.relative_to(ROOT)):{width}}  {covered:4}/{valid:<4}  {pct:6.1f}%")
    covered = sum(r[2] for r in rows)
    valid = sum(r[3] for r in rows)
    total = 100.0 * covered / valid if valid else 0.0
    bash = [r for r in rows if r[0] == "bash"]
    print(f"\nbash {sum(r[2] for r in bash)}/{sum(r[3] for r in bash)} lines, "
          f"python {covered - sum(r[2] for r in bash)}/{valid - sum(r[3] for r in bash)} statements+branches")
    print(f"TOTAL {covered}/{valid} = {total:.1f}% (floor {args.floor:g}%)")
    if missing:
        print("\nNOT MEASURED — shipped code no report covers:", *missing, sep="\n  ")
        return 1
    if total < args.floor:
        print(f"\nFAIL: {total:.1f}% is under the {args.floor:g}% floor")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
