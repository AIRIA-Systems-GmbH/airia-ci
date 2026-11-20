# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Does every repository's live ruleset equal the shared baseline plus its listed exceptions?

    python3 scripts/rulesets.py

Run it with your own `gh` login: it reads the rulesets of private repositories,
which no token in this repository can. It only reads: every call is a GET
(`gh api repos/<r>/rulesets` and `.../rulesets/<id>`), it never writes a ruleset.

The baseline is rulesets/baseline.json; each repository's rulesets, required
checks and every departure from the baseline, each with a reason, are in
rulesets/exceptions.toml (ruling 16, 2026-11-19: exceptions only for very good
reasons). A live state that differs from baseline + listed exceptions is drift:
it is printed and the exit code is 1. An exception without a reason, or of an
unknown kind, is refused (exit 2), as is a lookup that fails.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KINDS = {"required_checks", "omit_rule", "ref_include", "addition"}
# Absent from the payload of a user-owned repository, empty everywhere else: not a departure.
IGNORED_PR_PARAMETERS = {"dismissal_restriction"}


class Refused(Exception):
    """The exceptions file or a lookup is unusable; nothing was compared."""


def gh_get(path: str):
    p = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if p.returncode:
        raise Refused(f"cannot read {path}: {p.stderr.strip()}")
    return json.loads(p.stdout)


def load_exceptions(path: Path) -> dict:
    data = tomllib.loads(path.read_text())
    for repo, entry in data.get("repos", {}).items():
        for field in ("rulesets", "required_checks"):
            if field not in entry:
                raise Refused(f"{repo}: no `{field}` listed")
        for exc in entry.get("exceptions", []):
            kind = exc.get("kind")
            if kind not in KINDS:
                raise Refused(f"{repo}: exception of unknown kind {kind!r} (known: {', '.join(sorted(KINDS))})")
            if not str(exc.get("reason", "")).strip():
                raise Refused(f"{repo}: the {kind} exception has no reason")
    return data


def normalise(rules: list[dict]) -> dict[str, dict]:
    """{rule type: parameters}, order-free, without the fields that are not a departure."""
    out = {}
    for rule in rules:
        params = copy.deepcopy(rule.get("parameters", {}))
        if rule["type"] == "pull_request":
            for k in IGNORED_PR_PARAMETERS:
                params.pop(k, None)
        if rule["type"] == "required_status_checks":
            params["required_status_checks"] = sorted(params["required_status_checks"], key=lambda c: c["context"])
        out[rule["type"]] = params
    return out


def expected(baseline: dict, aggregate: str, entry: dict) -> tuple[dict, list[str]]:
    """What the repository's main ruleset must be: baseline + its exceptions; and what it still owes."""
    body = copy.deepcopy(baseline)
    owed = []
    excs = entry.get("exceptions", [])
    kinds = {e["kind"] for e in excs}
    checks = entry["required_checks"]
    if "required_checks" not in kinds and (not aggregate or checks != [aggregate]):
        owed.append(f"required checks {checks} are not the aggregate check {aggregate!r} "
                    "and no `required_checks` exception says why")
    rules = normalise(body["rules"])
    rules["required_status_checks"]["required_status_checks"] = [{"context": c} for c in sorted(checks)]
    if not checks:
        del rules["required_status_checks"]
    for e in excs:
        if e["kind"] == "omit_rule":
            rules.pop(e["rule"], None)
        elif e["kind"] == "ref_include":
            body["conditions"]["ref_name"]["include"] = e["include"]
    body["rules"] = rules
    return body, owed


def compare(want: dict, live: dict) -> list[str]:
    found = []
    for key in ("target", "enforcement", "bypass_actors"):
        if live.get(key) != want[key]:
            found.append(f"{key} is {live.get(key)!r}, the baseline has {want[key]!r}")
    if live.get("conditions") != want["conditions"]:
        found.append(f"conditions are {live.get('conditions')!r}, expected {want['conditions']!r}")
    have = normalise(live.get("rules", []))
    for t in sorted(set(want["rules"]) - set(have)):
        found.append(f"rule {t} is missing")
    for t in sorted(set(have) - set(want["rules"])):
        found.append(f"rule {t} is live and not in the baseline")
    for t in sorted(set(have) & set(want["rules"])):
        if have[t] != want["rules"][t]:
            found.append(f"rule {t} parameters are {json.dumps(have[t], sort_keys=True)}, "
                         f"expected {json.dumps(want['rules'][t], sort_keys=True)}")
    return found


def drift(repo: str, entry: dict, baseline: dict, aggregate: str) -> list[str]:
    want, found = expected(baseline, aggregate, entry)
    additions = [e["ruleset"] for e in entry.get("exceptions", []) if e["kind"] == "addition"]
    listed = list(entry["rulesets"]) + additions
    live_ids = sorted(r["id"] for r in gh_get(f"repos/{repo}/rulesets"))
    for i in sorted(set(live_ids) - set(listed)):
        found.append(f"ruleset {i} exists and is not listed")
    for i in sorted(set(listed) - set(live_ids)):
        found.append(f"ruleset {i} is listed and does not exist")
    for i in entry["rulesets"]:
        if i in live_ids:
            found += [f"ruleset {i}: {p}" for p in compare(want, gh_get(f"repos/{repo}/rulesets/{i}"))]
    for i in additions:
        if i in live_ids and gh_get(f"repos/{repo}/rulesets/{i}").get("enforcement") != "active":
            found.append(f"ruleset {i} (addition) is not active")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", type=Path, default=ROOT / "rulesets/baseline.json")
    ap.add_argument("--exceptions", type=Path, default=ROOT / "rulesets/exceptions.toml")
    args = ap.parse_args(argv)
    try:
        baseline = json.loads(args.baseline.read_text())
        data = load_exceptions(args.exceptions)
        aggregate = data.get("aggregate_check", "")
        drifting = 0
        for repo, entry in sorted(data["repos"].items()):
            problems = drift(repo, entry, baseline, aggregate)
            drifting += bool(problems)
            print(f"{repo}\t{'DRIFT' if problems else 'ok'}\t{len(entry.get('exceptions', []))} exceptions")
            for p in problems:
                print(f"  - {p}")
        print(f"{drifting} of {len(data['repos'])} repositories drift from the baseline")
        return 1 if drifting else 0
    # Exit 1 is drift: an input that cannot be read must never look like it.
    except (Refused, tomllib.TOMLDecodeError, json.JSONDecodeError, OSError, KeyError) as exc:
        print(f"error: {exc!r}" if isinstance(exc, KeyError) else f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
