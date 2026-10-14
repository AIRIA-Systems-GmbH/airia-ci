# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Every repository that calls airia-ci, and whether its pin is the newest major.

    python3 scripts/consumers.py AIRIA-Systems-GmbH tumma72
    python3 scripts/consumers.py --conformance [--dry-run] AIRIA-Systems-GmbH tumma72

Run it with your own `gh` login at release time: it reads private repositories
of every owner given, which no token in this repository can. A pin older than
the newest `ci-vN` is frozen, so moving the tag never reaches it; such a
repository is listed as BEHIND and the exit code is 1. AIRIA-DevTools and
AIRIA-process sat on the frozen `ci-v2` unnoticed until 2026-10-05.

`--conformance` also reads each consumer's workflows, pyproject.toml, uv.lock
and default-branch ruleset against the AIRIA standard (pins.toml), and keeps
ONE issue per consumer, titled "airia-ci standard drift", saying what drifts:
it is opened, rewritten by every scan, and closed once nothing drifts. What
counts: a pin on an older major; no workflow calling reusable-python-gate.yml;
`<job> / gate` or the Claude review missing from the required checks; a
bypass actor; a coverage floor under the standard's, or `--no-cov`; a gate
tool run unpinned or locked below its pin; a type checker the standard does
not accept. `--dry-run` prints each issue instead of writing it. The scan
needs PyYAML (it parses the workflows) and a login that can read every
consumer's rulesets and write its issues. `conformance.yml` runs it weekly.
"""

from __future__ import annotations

import argparse
import datetime
import functools
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

HARNESS = "AIRIA-Systems-GmbH/airia-ci"
USES = re.compile(re.escape(HARNESS) + r"/(\S+?)@(\S+)")
MAJOR = re.compile(r"^ci-v(\d+)$")
HERE = Path(__file__).resolve().parent
TITLE = "airia-ci standard drift"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The standard is checked by the code that enforces it, never by a copy.
init = load("init", HERE / "init.py")
toolchain = load("toolchain", HERE.parent / ".github/actions/python-toolchain/toolchain.py")
PINS = toolchain.load_pins(toolchain.PINS)


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


@functools.cache
def workflows(repo: str) -> dict[str, str]:
    """{path: text} of every workflow file the repository has."""
    p = gh("api", f"repos/{repo}/contents/.github/workflows", "--jq", ".[].path")
    if p.returncode:  # no .github/workflows (or an empty repository)
        return {}
    found = {}
    for path in p.stdout.split():
        f = gh("api", f"repos/{repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw")
        if f.returncode:
            raise RuntimeError(f"cannot read {repo}/{path}: {f.stderr.strip()}")
        found[path] = f.stdout
    return found


def pins(repo: str) -> list[tuple[str, str, str]]:
    """(workflow file, called path, ref) for every airia-ci reference in the repo's workflows."""
    return [(path, called, ref) for path, text in workflows(repo).items() for called, ref in USES.findall(text)]


def raw(repo: str, path: str) -> str | None:
    """A file on the default branch, or None if there is none."""
    f = gh("api", f"repos/{repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw")
    if f.returncode == 0:
        return f.stdout
    if "Not Found" in f.stderr or "404" in f.stderr:
        return None
    raise RuntimeError(f"cannot read {repo}/{path}: {f.stderr.strip()}")


def ruleset(repo: str) -> dict | None:
    """The standard ruleset, with its bypass actors (which need admin read to see)."""
    p = gh("api", f"repos/{repo}/rulesets", "--jq", f'.[] | select(.name == "{init.RULESET}") | .id')
    if p.returncode:
        raise RuntimeError(f"cannot list {repo}'s rulesets: {p.stderr.strip()}")
    if not p.stdout.strip():
        return None
    d = gh("api", f"repos/{repo}/rulesets/{p.stdout.split()[0]}")
    if d.returncode:
        raise RuntimeError(f"cannot read {repo}'s ruleset: {d.stderr.strip()}")
    return json.loads(d.stdout)


def drift(repo: str, newest: str) -> list[str]:
    """What in this consumer falls short of the standard; empty when nothing does."""
    found = sorted({f"{path} calls airia-ci at `{ref}`, not the current `{newest}`"
                    for path, _, ref in pins(repo) if ref != newest})
    floor = PINS["standard"]["coverage-floor"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".github/workflows").mkdir(parents=True)
        for path, text in workflows(repo).items():
            (root / path).write_text(text)
        files = {name: raw(repo, name) for name in ("pyproject.toml", "uv.lock")}
        for name, text in files.items():
            if text is not None:
                (root / name).write_text(text)
        standard = init.standard_gate(root)
        if files["pyproject.toml"] is None:
            found.append("no pyproject.toml: only the pin and the ruleset are checked")
        else:
            if not standard:
                found.append("no pull-request workflow calls `reusable-python-gate.yml`: the gates are the "
                             "repository's own, not the standard")
            found += toolchain.workflow_problems(root)
            lock = toolchain.locked(root) if files["uv.lock"] is not None else {}
            if files["uv.lock"] is None:
                found.append("no uv.lock: the standard installs with `uv sync --locked`")
            found += [f"uv.lock resolves {t} {lock[t]}, below the pinned {pin}"
                      for t, pin in PINS["tools"].items() if t in lock and toolchain.version(lock[t]) < toolchain.version(pin)]
            pyproject = tomllib.loads(files["pyproject.toml"])
            tool = pyproject.get("tool", {})
            report_floor = tool.get("coverage", {}).get("report", {}).get("fail_under")
            if report_floor is not None and float(report_floor) < floor:
                found.append(f"[tool.coverage.report] fail_under is {report_floor}, under the standard's {floor}")
            addopts = tool.get("pytest", {}).get("ini_options", {}).get("addopts", "")
            addopts = " ".join(addopts) if isinstance(addopts, list) else addopts
            if "--no-cov" in addopts.split():
                found.append("pytest addopts has `--no-cov`: no coverage is measured")
            for m in re.finditer(r"--cov-fail-under[= ](\d+(?:\.\d+)?)", addopts):
                if float(m.group(1)) < floor:
                    found.append(f"pytest addopts has `{m.group(0)}`, under the standard's {floor}")
            text = "\n".join(workflows(repo).values())
            for m in re.finditer(r"coverage-floor:\s*['\"]?(\d+(?:\.\d+)?)", text):
                if float(m.group(1)) < floor:
                    found.append(f"the gate is called with `coverage-floor: {m.group(1)}`, under the standard's {floor}")
            checkers = set(re.findall(r"type-checker:\s*['\"]?([\w-]+)", text))
            for checker in sorted(checkers - set(PINS["standard"]["type-checkers"])):
                found.append(f"type-checker `{checker}` is not one the standard accepts")
            if "mypy-strict" in checkers and repo not in PINS["standard"]["mypy-strict-repositories"]:
                found.append("type-checker `mypy-strict` is accepted only where it was already the gate; use pyright")
            if "ty" in tool or "ty" in lock:
                found.append("ty is configured or locked: the standard does not accept it before its 1.0; use pyright")
    rules = ruleset(repo)
    if rules is None:
        found.append(f"no {init.RULESET!r} ruleset: run `init.py protect`")
    else:
        if rules.get("bypass_actors"):
            names = ", ".join(f"{a.get('actor_type')} {a.get('actor_id')}" for a in rules["bypass_actors"])
            found.append(f"the ruleset can be bypassed ({names}): the standard has no bypass")
        required = {c["context"] for r in rules.get("rules", []) if r["type"] == "required_status_checks"
                    for c in r["parameters"]["required_status_checks"]}
        expected = standard[1] if standard else ["<job> / gate", "<job> / claude-review / claude-review"]
        for check in expected:
            if check not in required:
                found.append(f"`{check}` is not a required check: run `init.py protect`")
    return found


def file_issue(repo: str, reasons: list[str], dry_run: bool) -> str:
    """Open, rewrite or close the consumer's one drift issue; return what was done."""
    today = datetime.date.today().isoformat()
    body = "\n".join([
        f"airia-ci's conformance scan of {today} found this repository short of the AIRIA standard "
        "(pins.toml and reusable-python-gate.yml in AIRIA-Systems-GmbH/airia-ci). A project may add to the "
        "standard, never weaken it.",
        "",
        *[f"- [ ] {r}" for r in reasons],
        "",
        "How to adopt it: https://github.com/AIRIA-Systems-GmbH/airia-ci/blob/main/HOWTO.md#adopt-the-standard. "
        "Every scan rewrites this issue, and closes it once nothing drifts.",
    ])
    if dry_run:
        return f"would file:\n{body}" if reasons else "would close any open drift issue"
    p = gh("issue", "list", "--repo", repo, "--state", "open", "--limit", "200", "--json", "number,title")
    if p.returncode:
        raise RuntimeError(f"cannot list {repo}'s issues: {p.stderr.strip()}")
    numbers = [str(i["number"]) for i in json.loads(p.stdout) if i["title"] == TITLE]
    if not reasons:
        for n in numbers:
            gh("issue", "close", n, "--repo", repo, "--comment", f"No drift from the AIRIA standard as of {today}.")
        return f"closed #{numbers[0]}" if numbers else "no drift"
    if numbers:
        c = gh("issue", "edit", numbers[0], "--repo", repo, "--body", body)
        done = f"rewrote #{numbers[0]}"
    else:
        c = gh("issue", "create", "--repo", repo, "--title", TITLE, "--body", body)
        done = f"opened {c.stdout.strip()}"
    if c.returncode:
        raise RuntimeError(f"cannot write {repo}'s drift issue: {c.stderr.strip()}")
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("owners", nargs="+", help="organizations or users whose repositories to scan")
    ap.add_argument("--conformance", action="store_true",
                    help="also check each consumer against the standard and keep its drift issue")
    ap.add_argument("--dry-run", action="store_true", help="with --conformance: print each issue, write none")
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
    if not args.conformance:
        return 1 if behind else 0
    drifting = 0
    try:
        for repo in sorted({r[0] for r in rows}):
            reasons = drift(repo, newest)
            drifting += bool(reasons)
            print(f"{repo}\t{len(reasons)} drift\t{file_issue(repo, reasons, args.dry_run)}")
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{drifting} of {len({r[0] for r in rows})} consumers drift from the standard")
    return 1 if behind or drifting else 0


if __name__ == "__main__":
    sys.exit(main())
