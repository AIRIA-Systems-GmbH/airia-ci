# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The debt baseline of the shared Python gate: `.airia/debt-baseline.json`.

    debt.py [--file PATH] [--today YYYY-MM-DD] check
    debt.py [--today YYYY-MM-DD] compare [--event NAME] BASE HEAD
    debt.py [--file PATH] [--today YYYY-MM-DD] pyright REPORT
    debt.py [--file PATH] mypy-guard
    debt.py [--file PATH] [--today YYYY-MM-DD] tests STATUS OUTPUT

Design: design/gate-debt-ratchet.md (sections 1 to 4). The file is a dated,
tighten-only exception to the standard; this module loads it and decides
whether it may be relied on at all.

`check` fails loud (one `::error` line per cause, exit 1) when the file is
missing, is not valid JSON, has an unknown key or a wrong type, has a
`version` other than 1, a malformed date, an `expires` not after `set`, a
horizon over 90 days, a negative count, a coverage floor outside [0, 100] or a
duplicate test id. Past `expires` (UTC run date) it exits 1 and names the date:
the baselined sections no longer apply and the standard does. Within 14 days of
it, a `::warning` gives the days left. A `set` after the run date fails too, so
the horizon cannot be pushed out by a future `set`. `--today` injects the run date for tests.

`compare` holds the head's file to the base branch's copy (amendment A): it
exits 1, one `::error` per cause, when the head raises a `types.pyright` rule
count (a rule absent counts as 0), adds a test id to `tests.failing`, lowers
`coverage.floor` (or adds one where the base has none: the base is then at the
standard), or moves `expires` or `set` later. Dropping an id, a rule or a
section, lowering a count, raising the floor and an earlier date all pass.
Both files are validated with `load` first. BASE may be absent (no such file):
on a pull request that is a first file, and its `set` must be within 1 day of
the run date so a far-future `set` cannot buy extra days. On a push an absent
base passes with a notice: the PR that added the file was held to that rule on
its own run date, and `check` holds the horizon on every run.

`pyright REPORT` is the lint job's type-check pass rule with the ratchet on: pyright's `--outputjson` report, errors
counted per rule, each at or below `types.pyright` (a rule absent counts 0). Below passes with a `::notice`; a report
that is not pyright's JSON fails. `mypy-guard` fails when the file has a `types` section and the checker is mypy.

`tests STATUS OUTPUT` is the test job's pass rule with the ratchet on: STATUS is pytest's exit code and OUTPUT its
text, run with `-rfE`. The failing and erroring ids come from the `short test summary info` block (`FAILED id - msg`,
`ERROR id - msg`, normalised to the id) and every one must be in `tests.failing`; an id in the baseline that did not
fail passes with a `::notice`. Exit codes 2-5 (and anything but 0 or 1) fail outright, and exit 1 with no parsed id
fails: a crash, or a coverage miss with nothing failing, is not "no failures". A file with no `tests` section, or one
outside `set`..`expires`, holds the standard: nothing may fail.

`coverage_floor` (called by toolchain.py's `standard`) turns the `coverage` section into the floor the test
job's pytest step holds. The `coverage` section, `types` (by `pyright` above) and `tests` (by `tests` above) are
enforced; all are validated and compared.

`dumps` is the writer's canonical serialisation (the replacement JSON of
section 5); the writer itself is a later slice.

Standard library only: it runs before any project environment exists.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

DEFAULT_FILE = ".airia/debt-baseline.json"
MAX_HORIZON_DAYS = 90
WARN_DAYS = 14
KEYS = {"version", "set", "expires", "tests", "types", "coverage"}
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class Invalid(Exception):
    """The file cannot be relied on; `args[0]` is the list of reasons."""


def is_count(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_date(name: str, v: object, errors: list[str]) -> datetime.date | None:
    if not isinstance(v, str):
        errors.append(f"`{name}` must be a string YYYY-MM-DD, not {type(v).__name__}")
        return None
    try:
        if not DATE.fullmatch(v):
            raise ValueError(v)
        return datetime.date.fromisoformat(v)
    except ValueError:
        errors.append(f"`{name}` is not a date YYYY-MM-DD: {v!r}")
        return None


def section(data: dict, name: str, allowed: set[str], errors: list[str]) -> dict | None:
    """`data[name]` as an object with only `allowed` keys, or None (absent or reported)."""
    if name not in data:
        return None
    v = data[name]
    if not isinstance(v, dict):
        errors.append(f"`{name}` must be an object, not {type(v).__name__}")
        return None
    for k in sorted(set(v) - allowed):
        errors.append(f"unknown key `{name}.{k}`")
    return v


def validate(data: object) -> tuple[datetime.date | None, datetime.date | None, list[str]]:
    """(set, expires, every reason the content is invalid)."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return None, None, [f"the top level must be an object, not {type(data).__name__}"]
    for k in sorted(set(data) - KEYS):
        errors.append(f"unknown key `{k}`")
    if "version" not in data:
        errors.append("`version` is missing")
    elif data["version"] != 1 or not is_count(data["version"]):
        errors.append(f"`version` must be 1, not {data['version']!r}")
    dates = {}
    for name in ("set", "expires"):
        if name not in data:
            errors.append(f"`{name}` is missing")
            dates[name] = None
        else:
            dates[name] = parse_date(name, data[name], errors)
    first, last = dates["set"], dates["expires"]
    if first and last:
        if last <= first:
            errors.append(f"`expires` ({last}) must be after `set` ({first})")
        elif (last - first).days > MAX_HORIZON_DAYS:
            errors.append(f"the horizon is {(last - first).days} days from `set` ({first}) to `expires` ({last}); "
                          f"the maximum is {MAX_HORIZON_DAYS}")

    tests = section(data, "tests", {"failing"}, errors)
    if tests is not None and "failing" in tests:
        ids = tests["failing"]
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            errors.append("`tests.failing` must be a list of test ids (strings)")
        else:
            for i in sorted({i for i in ids if ids.count(i) > 1}):
                errors.append(f"duplicate test id in `tests.failing`: {i}")

    types = section(data, "types", {"pyright"}, errors)
    if types is not None and "pyright" in types:
        rules = types["pyright"]
        if not isinstance(rules, dict):
            errors.append("`types.pyright` must be an object of rule: count")
        else:
            for rule, n in rules.items():
                if not is_count(n):
                    errors.append(f"`types.pyright.{rule}` must be an integer count, not {n!r}")
                elif n < 0:
                    errors.append(f"`types.pyright.{rule}` is negative: {n}")

    cov = section(data, "coverage", {"floor"}, errors)
    if cov is not None and "floor" in cov:
        floor = cov["floor"]
        if not is_number(floor):
            errors.append(f"`coverage.floor` must be a number, not {floor!r}")
        elif not 0 <= floor <= 100:
            errors.append(f"`coverage.floor` must be within [0, 100], not {floor}")
    return first, last, errors


def load(path: Path) -> dict:
    """The validated content of `path`; raises Invalid with every reason otherwise."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Invalid([f"{path} is missing, and `debt-ratchet` is on"]) from None
    except OSError as e:
        raise Invalid([f"{path} cannot be read: {e}"]) from None
    try:
        data = json.loads(text)
    except ValueError as e:
        raise Invalid([f"{path} is not valid JSON: {e}"]) from None
    _, _, errors = validate(data)
    if errors:
        raise Invalid(errors)
    return data


def dumps(baseline: dict) -> str:
    """The canonical text of a baseline: stable key order, sorted ids and rules, a final newline."""
    out = {k: baseline[k] for k in ("version", "set", "expires") if k in baseline}
    if "tests" in baseline:
        out["tests"] = {"failing": sorted(baseline["tests"].get("failing", []))}
    if "types" in baseline:
        out["types"] = {"pyright": dict(sorted(baseline["types"].get("pyright", {}).items()))}
    if "coverage" in baseline:
        out["coverage"] = dict(baseline["coverage"])
    return json.dumps(out, indent=2) + "\n"


def coverage_floor(path: Path, requested: float, standard: float, today: datetime.date) -> float:
    """The coverage floor pytest must hold with the ratchet on (design section 2, "Coverage").

    `requested` is the caller's `coverage-floor` input, already refused if under `standard`. The baseline's
    `coverage.floor` replaces the standard, so it is the only way under it: while the file is valid and in date
    it is the floor. A caller that raised its input above the standard keeps the higher of the two. A file
    with no `coverage` section, or one outside `set`..`expires`, leaves `requested` (past `expires` the
    standard applies; `check` is what turns that red). Raises Invalid when the file cannot be relied on.
    """
    data = load(path)
    floor = data.get("coverage", {}).get("floor")
    if floor is None:
        return requested
    if not datetime.date.fromisoformat(data["set"]) <= today <= datetime.date.fromisoformat(data["expires"]):
        return requested
    return max(requested, floor) if requested > standard else floor


def pyright_errors(report: Path) -> dict[str, list[str]]:
    """Pyright's `--outputjson` report as {rule: [one line per error]}; only `severity == "error"` counts.

    An error with no rule (a syntax error) is keyed `(no rule)`, which no baseline can hold. Raises Invalid when
    the report is not pyright's JSON: the step must not read an empty or truncated report as "no errors".
    """
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Invalid([f"{report} is not pyright's JSON output: {e}"]) from None
    diagnostics = data.get("generalDiagnostics") if isinstance(data, dict) else None
    if not isinstance(diagnostics, list) or not all(isinstance(d, dict) for d in diagnostics):
        raise Invalid([f"{report} has no `generalDiagnostics` list: it is not pyright's JSON output"])
    errors: dict[str, list[str]] = {}
    for d in diagnostics:
        if d.get("severity") != "error":
            continue
        start = d.get("range", {}).get("start", {}) if isinstance(d.get("range"), dict) else {}
        where = f"{d.get('file', '?')}:{start.get('line', 0) + 1}"
        errors.setdefault(str(d.get("rule", "(no rule)")), []).append(f"{where} {d.get('message', '')}")
    return errors


def pyright(baseline: Path, report: Path, today: datetime.date) -> int:
    """Hold pyright's errors per rule to `types.pyright` (design section 2, "Types").

    A rule above its baseline (absent counts 0) is an `::error` and exit 1; below is a `::notice` and passes.
    A file with no `types` section, or one outside `set`..`expires`, holds the standard: no errors at all
    (past `expires` `check` is what turns red).
    """
    try:
        data = load(baseline)
        errors = pyright_errors(report)
    except Invalid as e:
        for reason in e.args[0]:
            print(f"::error title=Debt baseline::{reason}")
        return 1
    in_date = datetime.date.fromisoformat(data["set"]) <= today <= datetime.date.fromisoformat(data["expires"])
    allowed = data.get("types", {}).get("pyright", {}) if in_date else {}
    failed = False
    for rule in sorted(errors):
        if len(errors[rule]) > allowed.get(rule, 0):
            failed = True
            print(f"::error title=Pyright above the debt baseline::{rule}: {len(errors[rule])} errors, "
                  f"the baseline allows {allowed.get(rule, 0)}")
            for line in errors[rule]:
                print(f"  {line} ({rule})")
    better = [f"{rule} {allowed[rule]} -> {len(errors.get(rule, []))}" for rule in sorted(allowed)
              if len(errors.get(rule, [])) < allowed[rule]]
    if better and not failed:
        print(f"::notice title=Debt baseline can tighten::pyright errors fell below the baseline: {', '.join(better)}")
    return 1 if failed else 0


ANSI = re.compile(r"\x1b\[[0-9;]*m")
EXIT_NAMES = {2: "was interrupted", 3: "hit an internal error", 4: "was used wrongly", 5: "collected no tests"}


def node_id(rest: str) -> str:
    """The node id of a summary line after its `FAILED `/`ERROR `: the text up to the first ` - ` outside brackets.

    A parametrised id may hold spaces and ` - ` inside `[...]`. An id with unbalanced brackets is never cut, so it
    reads as an id nobody baselined: the safe direction.
    """
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif depth == 0 and rest.startswith(" - ", i):
            return rest[:i]
    return rest


def failing_ids(text: str) -> set[str]:
    """The ids on `FAILED`/`ERROR` lines of pytest's `short test summary info` block (`-rfE`); only that block counts,
    so a line a test printed cannot add or hide an id."""
    ids: set[str] = set()
    inside = False
    for line in ANSI.sub("", text).splitlines():
        if line.startswith("="):
            inside = "short test summary info" in line
        elif inside and (m := re.match(r"(?:FAILED|ERROR) (\S.*)$", line)):
            ids.add(node_id(m.group(1).rstrip()))
    return ids


def tests(baseline: Path, status: int, output: Path, today: datetime.date) -> int:
    """Hold pytest's failing ids to `tests.failing` (design section 2, "Tests"), as sets.

    Every failing id must be in the baseline; a baseline id that did not fail is a `::notice`. See the module doc
    for the exit codes.
    """
    try:
        data = load(baseline)
        text = output.read_text(encoding="utf-8", errors="replace")
    except Invalid as e:
        for reason in e.args[0]:
            print(f"::error title=Debt baseline::{reason}")
        return 1
    except OSError as e:
        print(f"::error title=Pytest output::cannot read pytest's output: {e}")
        return 1
    if status not in (0, 1):
        print(f"::error title=Pytest::pytest exited {status} and {EXIT_NAMES.get(status, 'failed to run')}; "
              "the debt baseline holds failing tests, not a run that did not finish")
        return 1
    failing = failing_ids(text) if status == 1 else set()
    if status == 1 and not failing:
        print("::error title=Pytest::pytest exited 1 but no FAILED or ERROR line was found in its summary; a crash, or "
              "a coverage miss with nothing failing, is not 'no failures'. The run must pass -rfE.")
        return 1
    in_date = datetime.date.fromisoformat(data["set"]) <= today <= datetime.date.fromisoformat(data["expires"])
    allowed = set(data.get("tests", {}).get("failing", [])) if in_date else set()
    new = sorted(failing - allowed)
    for test_id in new:
        print(f"::error title=Test outside the debt baseline::{test_id} fails and is not in tests.failing")
    fixed = sorted(allowed - failing)
    if fixed and not new:
        print(f"::notice title=Debt baseline can tighten::{len(fixed)} baselined test(s) did not fail: {', '.join(fixed)}")
    return 1 if new else 0


def mypy_guard(baseline: Path) -> int:
    """mypy --strict has no per-rule count here: a `types` section with the ratchet on fails loud (design §6.4)."""
    try:
        data = load(baseline)
    except Invalid as e:
        for reason in e.args[0]:
            print(f"::error title=Debt baseline::{reason}")
        return 1
    if "types" in data:
        print("::error title=Debt baseline::`types` baselines pyright errors, and `type-checker` is mypy-strict; "
              "remove the section or use pyright")
        return 1
    return 0


def check(path: Path, today: datetime.date) -> int:
    try:
        data = load(path)
    except Invalid as e:
        for reason in e.args[0]:
            print(f"::error title=Debt baseline::{reason}")
        return 1
    first = datetime.date.fromisoformat(data["set"])
    if first > today:
        print(f"::error title=Debt baseline::`set` ({first}) is after the run date ({today}); "
              f"a baseline cannot be set in the future")
        return 1
    expires = datetime.date.fromisoformat(data["expires"])
    left = (expires - today).days
    if left < 0:
        print(f"::error title=Debt baseline expired::the debt baseline expired on {expires}; "
              f"its sections no longer apply and the standard does")
        return 1
    if left <= WARN_DAYS:
        print(f"::warning title=Debt baseline expires::the debt baseline expires on {expires}: "
              f"{left} day(s) left, then the standard applies")
    return 0


def loosened(base: dict | None, head: dict, today: datetime.date, event: str) -> list[str]:
    """Every way `head` is looser than `base`; `base` None is a first file."""
    if base is None:
        first = datetime.date.fromisoformat(head["set"])
        if event == "push" or abs((first - today).days) <= 1:
            return []
        return [f"the base has no debt baseline, so this is a first file; its `set` ({first}) must be "
                f"within 1 day of the run date ({today})"]
    reasons = []
    base_rules = base.get("types", {}).get("pyright", {})
    for rule, n in sorted(head.get("types", {}).get("pyright", {}).items()):
        if n > base_rules.get(rule, 0):
            reasons.append(f"`types.pyright.{rule}` rose from {base_rules.get(rule, 0)} to {n}")
    base_ids = set(base.get("tests", {}).get("failing", []))
    for i in sorted(set(head.get("tests", {}).get("failing", [])) - base_ids):
        reasons.append(f"`tests.failing` gained a test id the base does not have: {i}")
    head_floor = head.get("coverage", {}).get("floor")
    base_floor = base.get("coverage", {}).get("floor")
    if head_floor is not None:
        if base_floor is None:
            reasons.append(f"`coverage.floor` ({head_floor}) is new; the base has none, so the standard applies there")
        elif head_floor < base_floor:
            reasons.append(f"`coverage.floor` fell from {base_floor} to {head_floor}")
    for name in ("expires", "set"):
        if head[name] > base[name]:
            reasons.append(f"`{name}` moved later, from {base[name]} to {head[name]}")
    return reasons


def compare(base: Path, head: Path, today: datetime.date, event: str) -> int:
    errors = []
    loaded = {}
    for role, path in (("head", head), ("base", base)):
        if role == "base" and not path.is_file():
            loaded[role] = None
            continue
        try:
            loaded[role] = load(path)
        except Invalid as e:
            errors += [f"{role}: {reason}" for reason in e.args[0]]
    if not errors:
        if loaded["base"] is None and event == "push":
            print(f"::notice title=Debt baseline::no base copy at {base} on a push: nothing to compare; "
                  f"`check` holds the horizon")
        errors = loosened(loaded["base"], loaded["head"], today, event)
    for reason in errors:
        print(f"::error title=Debt baseline only tightens::{reason}")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", type=Path, default=Path(DEFAULT_FILE))
    ap.add_argument("--today", type=datetime.date.fromisoformat, help="the run date (UTC); default today")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    cmp = sub.add_parser("compare", help="hold HEAD's baseline to BASE's copy")
    cmp.add_argument("--event", default="pull_request",
                     help="the GitHub event; only `push` lets an absent BASE pass without the first-file rule")
    cmp.add_argument("base", type=Path, help="the base branch's copy; may be absent")
    cmp.add_argument("head", type=Path)
    pyr = sub.add_parser("pyright", help="hold a pyright --outputjson report to the baseline's types.pyright")
    pyr.add_argument("report", type=Path)
    sub.add_parser("mypy-guard", help="fail when the baseline has a types section and the checker is mypy-strict")
    tst = sub.add_parser("tests", help="hold pytest's failing ids (-rfE) to the baseline's tests.failing")
    tst.add_argument("status", type=int, help="pytest's exit code")
    tst.add_argument("output", type=Path, help="pytest's output")
    args = ap.parse_args(argv)
    today = args.today or datetime.datetime.now(datetime.UTC).date()
    if args.command == "tests":
        return tests(args.file, args.status, args.output, today)
    if args.command == "pyright":
        return pyright(args.file, args.report, today)
    if args.command == "mypy-guard":
        return mypy_guard(args.file)
    if args.command == "compare":
        return compare(args.base, args.head, today, args.event)
    return check(args.file, today)


if __name__ == "__main__":
    sys.exit(main())
