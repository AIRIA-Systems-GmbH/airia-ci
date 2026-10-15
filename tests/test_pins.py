# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""What the shipped YAML runs on, and at which version.

- every third-party action is pinned to a commit, with its release as a comment
  (the comment is what Dependabot reads and rewrites), except the few that
  follow a major tag on purpose;
- Dependabot never touches the pins of this repository's own actions, nor the
  ones that follow a major, and its pull requests get no Claude review;
- this repository's own jobs name a runner image, not `ubuntu-latest`;
- pins.toml, the standard's tool versions and floors, is well-formed: every
  gate tool at one exact version, the floors where Andrea set them.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import re
import tomllib
import unittest

import yaml
from steps import ROOT

YAML = sorted(ROOT.glob(".github/workflows/*.yml")) + sorted(ROOT.glob(".github/actions/*/action.yml"))
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", re.MULTILINE)
# Actions that run at their moving major tag by choice. claude-code-action ships
# fixes almost daily; at @v1 every caller has them the same day, where a pin
# would hold each one behind a Dependabot PR (which has no Claude token to test
# it with) and a ci-vN move.
FOLLOWS_MAJOR = {"anthropics/claude-code-action": "v1"}


def third_party(*, following_major: bool = False) -> list[tuple[str, str, str]]:
    found = []
    for path in YAML:
        for ref, rest in USES.findall(path.read_text()):
            if ref.startswith("./") or ref.startswith("AIRIA-Systems-GmbH/airia-ci/"):
                continue
            if (ref.split("@")[0] in FOLLOWS_MAJOR) != following_major:
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

    def test_an_action_that_follows_its_major_is_on_exactly_that_tag(self):
        found = third_party(following_major=True)
        self.assertEqual({ref.split("@")[0] for _, ref, _ in found}, set(FOLLOWS_MAJOR))
        for where, ref, comment in found:
            with self.subTest(where=where, ref=ref):
                name, tag = ref.split("@")
                self.assertEqual(tag, FOLLOWS_MAJOR[name])
                self.assertEqual(comment, "")

    def test_dependabot_leaves_the_moving_ci_tag_alone(self):
        # Dependabot would rewrite @ci-vN to the newest frozen ci-vN.M, and the
        # callers would stop receiving every later fix.
        config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
        (update,) = config["updates"]
        self.assertEqual(update["package-ecosystem"], "github-actions")
        self.assertIn("/.github/actions/*", update["directories"])
        self.assertIn("AIRIA-Systems-GmbH/airia-ci*", [i["dependency-name"] for i in update["ignore"]])

    def test_dependabot_leaves_an_action_that_follows_its_major_alone(self):
        # Dependabot would otherwise pin it, or propose the next major, as a PR.
        (update,) = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())["updates"]
        ignored = [i["dependency-name"] for i in update["ignore"]]
        for name in FOLLOWS_MAJOR:
            self.assertIn(name, ignored)

    def test_dependabot_pull_requests_are_not_reviewed(self):
        # Its runs get no Actions secrets, so the review could only fail with
        # "No Claude credential", a red mark on every weekly bump.
        job = yaml.safe_load((ROOT / ".github/workflows/self-test.yml").read_text())["jobs"]["claude-review"]
        self.assertIn("github.event.pull_request.user.login != 'dependabot[bot]'", job["if"])

    def test_this_repository_runs_on_a_named_image(self):
        # ubuntu-latest moves to a new release under us (Ubuntu 26 from
        # 2026-10-19); a move is a change we make and test, not one that happens.
        for path in sorted(ROOT.glob(".github/workflows/*.yml")):
            jobs = yaml.safe_load(path.read_text())["jobs"]
            for name, job in jobs.items():
                with self.subTest(workflow=path.name, job=name):
                    labels = [job.get("runs-on", ""), str(job.get("with", {}).get("runner", ""))]
                    self.assertFalse([label for label in labels if "ubuntu-latest" in str(label)])


class Standard(unittest.TestCase):
    """pins.toml: what the python-toolchain action installs and the floors it holds."""

    def setUp(self):
        self.pins = tomllib.loads((ROOT / "pins.toml").read_text())

    def test_every_gate_tool_has_one_exact_version(self):
        # A range or a bare name is what broke SiteBuilder (airia-ci#37).
        tools = self.pins["tools"]
        self.assertEqual(set(tools), {"uv", "ruff", "pyright", "mypy", "pytest", "pytest-cov", "coverage",
                                      "pytest-xdist", "import-linter", "reuse", "pip-audit", "zizmor", "gitleaks"})
        for name, version in [*tools.items(), *self.pins["harness"].items()]:
            with self.subTest(tool=name):
                self.assertRegex(version, r"^\d+(\.\d+)+$")

    def test_gitleaks_is_checked_against_a_digest_per_platform(self):
        digests = self.pins["gitleaks"]["sha256"]
        self.assertIn("linux_x64", digests)  # the Thor pool and GitHub's runners
        for key, digest in digests.items():
            with self.subTest(platform=key):
                self.assertRegex(key, r"^(linux|darwin)_(x64|arm64)$")
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_the_floors_are_the_ones_decided(self):
        # 2026-10-14: 90 % coverage; pyright, or mypy --strict where it already
        # was the gate; ty not before its 1.0.
        standard = self.pins["standard"]
        self.assertEqual(standard["coverage-floor"], 90)
        self.assertEqual(standard["type-checkers"], ["pyright", "mypy-strict"])
        self.assertEqual(standard["mypy-strict-repositories"], ["AIRIA-Systems-GmbH/AIRIA-process"])

    def test_the_allowed_pythons_are_minor_versions_in_order(self):
        allowed = self.pins["python"]["allowed"]
        self.assertTrue(allowed)
        for v in allowed:
            self.assertRegex(v, r"^3\.\d+$")
        self.assertEqual(allowed, sorted(allowed, key=lambda v: int(v.split(".")[1])))


class Template(unittest.TestCase):
    """templates/python: what a consumer copies, in step with the standard it is checked against."""

    def test_the_template_dev_group_is_at_the_pins(self):
        # A consumer that copies the template must pass the gate's below-the-pin check on its first run.
        pins = tomllib.loads((ROOT / "pins.toml").read_text())["tools"]
        dev = tomllib.loads((ROOT / "templates/python/pyproject.toml").read_text())["dependency-groups"]["dev"]
        found = {}
        for spec in dev:
            name, version = re.fullmatch(r"([a-z-]+)(?:\[\w+\])?==([\d.]+)", spec).groups()
            found[name] = version
        self.assertEqual(set(found), {"ruff", "pyright", "pytest", "pytest-cov", "pytest-xdist", "coverage"})
        for name, version in found.items():
            with self.subTest(tool=name):
                self.assertEqual(version, pins[name])

    def test_the_template_holds_the_floor_and_measures_a_package(self):
        tool = tomllib.loads((ROOT / "templates/python/pyproject.toml").read_text())["tool"]
        self.assertEqual(tool["coverage"]["report"]["fail_under"], 90)
        self.assertIn("--cov=<package>", tool["pytest"]["ini_options"]["addopts"].split())
        self.assertIn("auto", tool["pytest"]["ini_options"]["addopts"].split(), "-n auto: ci-local caps it")


if __name__ == "__main__":
    unittest.main()
