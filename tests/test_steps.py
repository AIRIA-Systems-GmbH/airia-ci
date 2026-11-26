# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The harness's own shared state: the sliced files under build/coverage/src.

Parallel workers slice the same shipped blocks into the same files; a worker that
truncates a file another is executing makes bash read half a script. `_publish`
must leave identical bytes alone and replace changed ones atomically.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import steps


class Publish(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.src = Path(scratch.name) / "src"
        patch = mock.patch.object(steps, "SRC", self.src)
        patch.start()
        self.addCleanup(patch.stop)
        self.out = self.src / "a.sh"

    def test_it_writes_the_file_with_its_mode_and_leaves_no_temp_file(self):
        steps._publish(self.out, "echo hi\n", 0o755)
        self.assertEqual(self.out.read_text(), "echo hi\n")
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o755)
        self.assertEqual([p.name for p in self.src.iterdir()], ["a.sh"])

    def test_identical_bytes_are_left_alone_so_a_running_reader_keeps_its_inode(self):
        steps._publish(self.out, "echo hi\n", 0o755)
        inode = self.out.stat().st_ino
        steps._publish(self.out, "echo hi\n", 0o755)
        self.assertEqual(self.out.stat().st_ino, inode)

    def test_changed_bytes_replace_the_file_atomically(self):
        steps._publish(self.out, "echo hi\n", 0o755)
        reader = self.out.open()
        self.addCleanup(reader.close)
        steps._publish(self.out, "echo bye\n", 0o755)
        self.assertEqual(reader.read(), "echo hi\n", "the old file is whole for whoever already opened it")
        self.assertEqual(self.out.read_text(), "echo bye\n")
        self.assertEqual([p.name for p in self.src.iterdir()], ["a.sh"])


if __name__ == "__main__":
    unittest.main()
