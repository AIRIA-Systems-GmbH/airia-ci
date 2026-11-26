# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""shard.py: what one shard of the Python gate runs (airia-ci#171; named groups, airia-ci#181).

    python3 shard.py LISTING SHARDS SHARD
    python3 shard.py group SHARD

LISTING is the output of `pytest --collect-only -q`. Prints, one per line, the files this shard (1..SHARDS) runs, in
listing order; prints nothing for a shard that gets no file. Whole files, never ids: passing node ids changes which
`conftest` pytest imports first, and a module-scoped fixture would be rebuilt in every shard holding one of its tests.

Files go in descending test count (ties by file name) to the least-loaded shard (ties to the lowest index), so the split is
a function of the listing alone. Every id read must equal pytest's own count, and must land in exactly one shard's files;
a listing that cannot be read exits 1 with an `::error`, never a guess. One huge file makes uneven shards, never a lost test.

`group SHARD` serves the `shard-groups` input instead of the by-file split. It reads SHARD_GROUPS (`name: paths`, one per
line), SHARD_WORKERS (`name: N`, 1 to 4) and WORKERS (the `pytest-workers` input) from the environment. Shards 1..G are the
groups in order; shard G+1 is `rest`: the whole suite with an `--ignore` for every group's path, so a directory no group
names is run, never skipped. A group is PATHS ONLY: with an option such as `-m` or `-k`, or a `::` id, `rest` would ignore
the whole path while the group ran only part of it, and the remainder would run nowhere. It prints the shard's name, its
worker count, then one pytest argument per line.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

REST = "rest"
MAX_WORKERS = 4
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SUMMARY = re.compile(r"^(\d+)(?:/\d+)? tests? collected")


class Refused(Exception):
    pass


def read_ids(text: str) -> tuple[list[str], int | None]:
    """The ids of a listing (a line at column 0 holding `::`, read whole) and pytest's own count if it printed one."""
    ids, expected = [], None
    for line in text.splitlines():
        if line[:1].strip() and "::" in line:
            ids.append(line)
        elif m := SUMMARY.match(line):
            expected = int(m.group(1))
    return ids, expected


def check(ids: list[str], files: list[list[str]]) -> None:
    """The guard: each id's file is in exactly one shard, so no id is dropped or run twice."""
    for i in ids:
        f = i.split("::", 1)[0]
        n = sum(f in fs for fs in files)
        if n != 1:
            raise Refused(f"the test {i} would run in {n} shards, not exactly one")


def assign(ids: list[str], shards: int) -> list[list[str]]:
    """The files of each shard, as lists in listing order."""
    counts: dict[str, int] = {}
    for i in ids:
        f = i.split("::", 1)[0]
        counts[f] = counts.get(f, 0) + 1
    load = [0] * shards
    owner: dict[str, int] = {}
    for f in sorted(counts, key=lambda f: (-counts[f], f)):
        k = min(range(shards), key=lambda s: (load[s], s))
        owner[f] = k
        load[k] += counts[f]
    files: list[list[str]] = [[] for _ in range(shards)]
    for f in counts:  # insertion order is the listing order
        files[owner[f]].append(f)
    check(ids, files)
    return files


def shard_files(text: str, shards: int, shard: int) -> list[str]:
    ids, expected = read_ids(text)
    if expected is not None and len(ids) != expected:
        raise Refused(f"Collection count::pytest collected {expected} tests but {len(ids)} ids were read from its listing, "
                      "so a test would run in no shard")
    return assign(ids, shards)[shard - 1]


def named_lines(text: str, source: str) -> dict[str, str]:
    """The `name: value` lines of an input, in order; blank lines are ignored."""
    out: dict[str, str] = {}
    for line in filter(None, (raw.strip() for raw in text.splitlines())):
        name, colon, value = (part.strip() for part in line.partition(":"))
        if not colon or not value or not NAME.fullmatch(name):
            raise Refused(f"{source}: each line is `name: value`, the name of letters, digits, `.`, `_` and `-`; "
                          f"got {line!r}")
        if name in out:
            raise Refused(f"{source}: {name!r} is named twice")
        out[name] = value
    return out


def parse_groups(text: str) -> dict[str, list[str]]:
    """{group: its paths}, each path in the checkout and in no other group."""
    groups: dict[str, list[str]] = {}
    taken: list[tuple[Path, str]] = []
    for name, value in named_lines(text, "shard-groups").items():
        if name == REST:
            raise Refused(f"shard-groups: `{REST}` is the automatic shard of everything no group names; use another name")
        try:
            paths = shlex.split(value)
        except ValueError as e:
            raise Refused(f"shard-groups: {name}: {e}") from None
        for path in paths:
            if path.startswith("-") or "::" in path:
                raise Refused(f"shard-groups: {name}: {path!r} is not a path. A group takes only files and directories: "
                              f"`{REST}` ignores each whole path, so what an option or a `::` id left out would run in "
                              "no shard")
            if not Path(path).exists():
                raise Refused(f"shard-groups: {name}: no file or directory {path!r} in the checkout")
            here = Path(path).resolve()
            for other, owner in taken:
                if owner != name and (here.is_relative_to(other) or other.is_relative_to(here)):
                    raise Refused(f"shard-groups: {path!r} of {name} overlaps a path of {owner}, "
                                  "so its tests would run twice")
            taken.append((here, name))
        groups[name] = paths
    return groups


def parse_workers(text: str, names: list[str]) -> dict[str, int]:
    """{shard: its xdist workers}; only shards in `names`, never more than MAX_WORKERS."""
    workers: dict[str, int] = {}
    for name, value in named_lines(text, "shard-workers").items():
        if name not in names:
            raise Refused(f"shard-workers: no shard is called {name!r}; the shards are {', '.join(names)}")
        if not re.fullmatch(f"[1-{MAX_WORKERS}]", value):
            raise Refused(f"shard-workers: {name}: a whole number from 1 to {MAX_WORKERS} "
                          f"({MAX_WORKERS} is the most a job runs), not {value!r}")
        workers[name] = int(value)
    return workers


def group_plan(groups_text: str, workers_text: str, default: str, shard: int) -> tuple[str, int, list[str]]:
    """(name, xdist workers, pytest arguments) of shard 1..groups+1; `default` is `pytest-workers`, maybe empty."""
    groups = parse_groups(groups_text)
    names = [*groups, REST]
    workers = parse_workers(workers_text, names)
    if not 1 <= shard <= len(names):
        raise Refused(f"shard {shard} is outside 1..{len(names)}")
    name = names[shard - 1]
    args = groups[name] if name != REST else [f"--ignore={p}" for paths in groups.values() for p in paths]
    return name, workers.get(name, int(default or MAX_WORKERS)), args


def group_main(argv: list[str]) -> int:
    try:
        (shard,) = argv
        name, workers, args = group_plan(os.environ.get("SHARD_GROUPS", ""), os.environ.get("SHARD_WORKERS", ""),
                                         os.environ.get("WORKERS", ""), int(shard))
    except (Refused, ValueError) as e:
        print(f"::error title=Shard groups::{e}")
        return 1
    print("\n".join([name, str(workers), *args]))
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["group"]:
        return group_main(argv[1:])
    try:
        listing, shards, shard = argv
        if not 1 <= int(shard) <= int(shards):
            raise Refused(f"shard {shard} is outside 1..{shards}")
        with open(listing, encoding="utf-8", errors="replace") as fh:
            files = shard_files(fh.read(), int(shards), int(shard))
    except Refused as e:
        msg = str(e)
        print(f"::error title={msg}" if msg.startswith("Collection count::") else f"::error title=Shard assignment::{msg}")
        return 1
    except (ValueError, OSError) as e:
        print(f"::error title=Shard assignment::shard.py cannot read its input: {e}")
        return 1
    if files:
        print("\n".join(files))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
