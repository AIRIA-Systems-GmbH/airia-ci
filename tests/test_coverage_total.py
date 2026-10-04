# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The coverage gate's own arithmetic: the figure is only as honest as these.

lines_inside_quotes is the one correction applied to kcov. It must drop the
lines kcov can never mark hit (they start inside a multi-line string) and
nothing else: dropping a real line would hide an untested one.

Run: python3 -m unittest discover -s tests
"""

import tempfile
import unittest
from pathlib import Path

from coverage_total import lines_inside_quotes, python_report


class LinesInsideQuotes(unittest.TestCase):
    def test_the_body_of_a_multi_line_single_quoted_jq_filter_is_dropped(self):
        self.assertEqual(lines_inside_quotes("x=$(jq -r '\n  .a\n  | .b\n' f)\necho done"), {2, 3, 4})

    def test_a_double_quoted_string_continues_past_an_escaped_quote(self):
        self.assertEqual(lines_inside_quotes('echo "a \\" b\nc"\necho d'), {2})

    def test_a_hash_inside_a_string_is_not_a_comment(self):
        self.assertEqual(lines_inside_quotes("echo '# not a comment\nstill'\necho e"), {2})

    def test_a_quote_nested_in_a_command_substitution_in_a_string(self):
        self.assertEqual(lines_inside_quotes('echo "$(echo "a\nb")"\necho i'), {2})

    def test_real_commands_are_kept(self):
        # Each of these lines is a command bash can report, so kcov's hit or
        # miss on it is real and must stay in the figure.
        for name, text in {
            "a multi-line command substitution": "x=$(\n  echo a\n)\necho b",
            "an apostrophe in a comment": "# don't\necho f",
            "a backslash continuation": "echo a \\\n  b\necho c",
        }.items():
            with self.subTest(name):
                self.assertEqual(lines_inside_quotes(text), set())

    def test_a_heredoc_body_is_not_parsed_as_bash(self):
        # A stray quote in the body must not open a string that swallows the
        # rest of the script. The body itself is not dropped either: kcov
        # does not count heredoc bodies today, and if it starts to, this
        # test is where that decision gets made.
        for text in ("cat <<'E'\nit's 'open\nE\necho g", "cat <<-X\n\tit's\n\tX\necho h"):
            with self.subTest(text=text):
                self.assertEqual(lines_inside_quotes(text), set())


class PythonReport(unittest.TestCase):
    def test_each_file_resolves_against_the_source_root_it_lives_under(self):
        # .coveragerc has two roots; coverage.py writes one <source> per root
        # and each filename relative to its own. Resolving against the first
        # only would report the second root's files as unmeasured.
        with tempfile.TemporaryDirectory() as t:
            a, b = Path(t, "a"), Path(t, "b")
            (a / "pkg").mkdir(parents=True)
            b.mkdir()
            (a / "pkg/one.py").write_text("")
            (b / "two.py").write_text("")
            xml = Path(t, "c.xml")
            xml.write_text(
                f"<coverage><sources><source>{a}</source><source>{b}</source></sources><packages>"
                '<package><classes><class filename="pkg/one.py"><lines>'
                '<line number="1" hits="1" branch="true" condition-coverage="50% (1/2)"/>'
                '</lines></class><class filename="two.py"><lines><line number="1" hits="0"/>'
                "</lines></class></classes></package></packages></coverage>"
            )
            self.assertEqual(python_report(xml), {(a / "pkg/one.py").resolve(): (2, 3), (b / "two.py").resolve(): (0, 1)})


if __name__ == "__main__":
    unittest.main()
