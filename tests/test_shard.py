# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""shard.py: the Python gate's shards are whole files, balanced by test count (airia-ci#171).

What these tests hold: the split is a function of the listing alone (a shard re-run on the same commit runs the same
files); loads differ by at most the largest file; every collected id lands in exactly one shard, whatever the file name;
a listing the script cannot account for fails loud instead of dropping a test.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

from steps import ROOT

SCRIPT = ROOT / ".github/actions/python-toolchain/shard.py"
spec = importlib.util.spec_from_file_location("shard", SCRIPT)
shard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shard)


def listing(files: dict[str, int], summary: bool = True) -> str:
    ids = [f"{f}::test_{n}" for f, c in files.items() for n in range(c)]
    return "\n".join(ids + ([f"\n{len(ids)} tests collected in 0.1s"] if summary else []))


def split(files, n):
    return [shard.shard_files(listing(files), n, k) for k in range(1, n + 1)]


class Assignment(unittest.TestCase):
    FILES = {"tests/e.py": 1, "tests/a.py": 5, "tests/b.py": 5, "tests/c.py": 3, "tests/d.py": 2}

    def test_the_assignment_is_deterministic_ties_by_name_then_lowest_shard(self):
        # a, b tie at 5: a (by name) -> shard 1, b -> shard 2; c (3) ties 5/5 -> shard 1 (8); d (2) -> shard 2 (7); e -> shard 2.
        for _ in range(2):
            self.assertEqual(split(self.FILES, 2), [["tests/a.py", "tests/c.py"], ["tests/e.py", "tests/b.py", "tests/d.py"]])

    def test_loads_differ_by_at_most_the_largest_file(self):
        files = {f"t/f{i}.py": (i * 7) % 11 + 1 for i in range(23)}
        for n in range(2, 9):
            loads = [sum(files[f] for f in fs) for fs in split(files, n)]
            self.assertLessEqual(max(loads) - min(loads), max(files.values()), (n, loads))

    def test_one_huge_file_is_uneven_but_nothing_is_dropped(self):
        files = {"t/s1.py": 1, "t/big.py": 50, "t/s2.py": 1}
        parts = split(files, 3)
        self.assertEqual(parts[0], ["t/big.py"])
        self.assertEqual(sorted(sum(parts, [])), sorted(files))

    def test_more_shards_than_files_leaves_empty_shards(self):
        self.assertEqual(split({"t/a.py": 2, "t/b.py": 1}, 4)[2:], [[], []])

    def test_shards_together_cover_every_id_exactly_once(self):
        files = {"t y/a[1].py": 3, "t/b.py": 4, "t/c.py": 4}
        text = listing(files)
        ids, _ = shard.read_ids(text)
        for n in (1, 2, 3, 5):
            ran = []
            for k in range(1, n + 1):
                mine = shard.shard_files(text, n, k)
                ran += [i for i in ids if i.split("::", 1)[0] in mine]
            self.assertEqual(sorted(ran), sorted(ids), n)


class Reading(unittest.TestCase):
    def test_ids_with_spaces_brackets_and_colons_are_read_whole_and_noise_is_not_an_id(self):
        text = "\n".join(["a b.py::t[x y::z]", "", "=== warnings summary ===", "  x.py:1: UserWarning: no::x",
                           "2/9 tests collected (7 deselected) in 0.1s"])
        ids, expected = shard.read_ids(text)
        self.assertEqual(ids, ["a b.py::t[x y::z]"])
        self.assertEqual(expected, 2)

    def test_a_count_that_differs_from_pytests_is_refused(self):
        with self.assertRaisesRegex(shard.Refused, "collected 3 tests but 2 ids"):
            shard.shard_files("a.py::t0\nb.py::t1\n3 tests collected in 0.1s", 2, 1)

    def test_a_listing_without_a_summary_is_accepted(self):
        self.assertEqual(shard.shard_files("a.py::t0", 2, 1), ["a.py"])

    def test_the_guard_refuses_a_file_in_no_shard_or_in_two(self):
        for files, n in (([[], []], 0), ([["a.py"], ["a.py"]], 2)):
            with self.assertRaisesRegex(shard.Refused, f"run in {n} shards"):
                shard.check(["a.py::t"], files)
        shard.check(["a.py::t"], [["a.py"], []])


class Command(unittest.TestCase):
    def run_main(self, text, *args):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "collected.txt"
            path.write_text(text)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = shard.main([str(path), *args])
            return code, out.getvalue()

    def test_prints_this_shards_files_one_per_line(self):
        self.assertEqual(self.run_main(listing({"t y/a.py": 3, "t/b.py": 1}), "2", "1"), (0, "t y/a.py\n"))

    def test_an_empty_shard_prints_nothing_and_passes(self):
        self.assertEqual(self.run_main("a.py::t\n1 test collected", "3", "3"), (0, ""))

    def test_count_mismatch_is_an_error_line(self):
        code, out = self.run_main("a.py::t\n4 tests collected", "2", "1")
        self.assertEqual(code, 1)
        self.assertIn("::error title=Collection count::pytest collected 4 tests but 1 ids", out)

    def test_unusable_arguments_fail_loud(self):
        for args in (("2", "0"), ("2", "3"), ("x", "1")):
            code, out = self.run_main("a.py::t", *args)
            self.assertEqual(code, 1, args)
            self.assertIn("::error title=Shard assignment::", out)

    def test_a_missing_listing_fails_loud(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = shard.main(["/nonexistent/collected.txt", "2", "1"])
        self.assertEqual(code, 1)
        self.assertIn("::error title=Shard assignment::", out.getvalue())


if __name__ == "__main__":
    unittest.main()
