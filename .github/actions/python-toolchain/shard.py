# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""shard.py: which test files one shard of the Python gate runs (airia-ci#171).

    python3 shard.py LISTING SHARDS SHARD

LISTING is the output of `pytest --collect-only -q`. Prints, one per line, the files this shard (1..SHARDS) runs, in
listing order; prints nothing for a shard that gets no file. Whole files, never ids: passing node ids changes which
`conftest` pytest imports first, and a module-scoped fixture would be rebuilt in every shard holding one of its tests.

Files go in descending test count (ties by file name) to the least-loaded shard (ties to the lowest index), so the split is
a function of the listing alone. Every id read must equal pytest's own count, and must land in exactly one shard's files;
a listing that cannot be read exits 1 with an `::error`, never a guess. One huge file makes uneven shards, never a lost test.
"""

from __future__ import annotations

import re
import sys

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


def main(argv: list[str]) -> int:
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
