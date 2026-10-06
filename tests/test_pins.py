# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""What the shipped YAML runs on, and at which version.

- every third-party action is pinned to a commit, with its release as a comment
  (the comment is what Dependabot reads and rewrites);
- Dependabot never touches the pins of this repository's own actions;
- this repository's own jobs name a runner image, not `ubuntu-latest`.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import re
import unittest

import yaml
from steps import ROOT

YAML = sorted(ROOT.glob(".github/workflows/*.yml")) + sorted(ROOT.glob(".github/actions/*/action.yml"))
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", re.MULTILINE)


def third_party() -> list[tuple[str, str, str]]:
    found = []
    for path in YAML:
        for ref, rest in USES.findall(path.read_text()):
            if ref.startswith("./") or ref.startswith("AIRIA-Systems-GmbH/airia-ci/"):
                continue
            found.append((path.relative_to(ROOT).as_posix(), ref, rest.strip()))
    return found


class Pins(unittest.TestCase):
    def test_every_third_party_action_is_pinned_to_a_commit_with_its_release(self):
        # A tag like @v1 can be moved by its owner; these steps hold the Claude
        # token and the App key of every calling repository.
        found = third_party()
        self.assertGreaterEqual(len(found), 3)
        for where, ref, comment in found:
            with self.subTest(where=where, ref=ref):
                self.assertRegex(ref, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
                self.assertRegex(comment, r"^# v\d+\.\d+\.\d+$")

    def test_one_action_is_pinned_to_one_commit_everywhere(self):
        commits: dict[str, set[str]] = {}
        for _, ref, _ in third_party():
            name, sha = ref.split("@")
            commits.setdefault(name, set()).add(sha)
        self.assertEqual({n: s for n, s in commits.items() if len(s) > 1}, {})

    def test_dependabot_leaves_the_moving_ci_tag_alone(self):
        # Dependabot would rewrite @ci-v3 to the newest frozen ci-v3.N, and the
        # callers would stop receiving every later fix.
        config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
        (update,) = config["updates"]
        self.assertEqual(update["package-ecosystem"], "github-actions")
        self.assertIn("/.github/actions/*", update["directories"])
        self.assertIn("AIRIA-Systems-GmbH/airia-ci*", [i["dependency-name"] for i in update["ignore"]])

    def test_this_repository_runs_on_a_named_image(self):
        # ubuntu-latest moves to a new release under us (Ubuntu 26 from
        # 2026-10-19); a move is a change we make and test, not one that happens.
        for path in sorted(ROOT.glob(".github/workflows/*.yml")):
            jobs = yaml.safe_load(path.read_text())["jobs"]
            for name, job in jobs.items():
                with self.subTest(workflow=path.name, job=name):
                    labels = [job.get("runs-on", ""), str(job.get("with", {}).get("runner", ""))]
                    self.assertFalse([label for label in labels if "ubuntu-latest" in str(label)])


if __name__ == "__main__":
    unittest.main()
