# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""peak_rss.py: run a command and report its process tree's peak resident memory (airia-ci#181).

    python3 peak_rss.py COMMAND [ARG ...]

The gate's shard jobs run pytest through it, so a lane can watch its memory limit. Every second it sums `VmRSS` over
COMMAND and its descendants (pytest and its xdist workers) from /proc, and when COMMAND ends it prints the highest sum and
adds it to GITHUB_STEP_SUMMARY. It is a sample: a spike between two samples is missed, and a page two processes share is
counted in each. COMMAND's output and exit status pass through untouched (128 + N when signal N killed it). Nothing about
the measurement can fail the job: with no /proc it says the figure was not sampled.

AIRIA_PROC names another proc directory (the tests). Standard library only.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path

INTERVAL = 1.0


def processes(proc: Path) -> dict[int, tuple[int, int]]:
    """{pid: (parent pid, resident KiB)}; a process that ends while it is read is left out."""
    found = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "status").read_text(errors="replace")
            fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
            # A zombie and a kernel thread have no VmRSS line: they hold no memory.
            found[int(entry.name)] = (int(fields["PPid"]), int(fields.get("VmRSS", "0 kB").split()[0]))
        except (OSError, ValueError, KeyError, IndexError):
            continue
    return found


def tree_kib(root: int, proc: Path) -> int:
    """The resident KiB of `root` and all its descendants."""
    table = processes(proc)
    children: dict[int, list[int]] = {}
    for pid, (parent, _) in table.items():
        children.setdefault(parent, []).append(pid)
    total, todo, seen = 0, [root], set()
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += table.get(pid, (0, 0))[1]
        todo += children.get(pid, [])
    return total


def report(peak: int) -> None:
    shard = f"Shard {os.environ.get('SHARD', '?')}/{os.environ.get('SHARDS', '?')}"
    figure = f"{peak / 1024:.0f} MiB" if peak else "not sampled"
    line = f"{shard} peak resident memory of the pytest process tree (sampled every {INTERVAL:g} s): {figure}"
    print(line, flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def main(argv: list[str]) -> int:
    proc = Path(os.environ.get("AIRIA_PROC", "/proc"))
    try:
        child = subprocess.Popen(argv)
    except OSError as e:
        print(f"cannot run {argv[0]}: {e}", file=sys.stderr)
        return 127
    peak, status = 0, None
    while status is None:
        with contextlib.suppress(Exception):  # measuring never fails the job
            peak = max(peak, tree_kib(child.pid, proc))
        with contextlib.suppress(subprocess.TimeoutExpired):
            status = child.wait(timeout=INTERVAL)
    try:
        report(peak)
    except Exception as e:  # nor does reporting
        print(f"::warning title=Peak memory::the figure could not be reported: {e}")
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
