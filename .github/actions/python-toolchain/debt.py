# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The debt baseline of the shared Python gate: `.airia/debt-baseline.json`.

    debt.py [--file PATH] [--today YYYY-MM-DD] check
    debt.py [--today YYYY-MM-DD] compare [--event NAME] BASE HEAD

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
    args = ap.parse_args(argv)
    today = args.today or datetime.datetime.now(datetime.UTC).date()
    if args.command == "compare":
        return compare(args.base, args.head, today, args.event)
    return check(args.file, today)


if __name__ == "__main__":
    sys.exit(main())
