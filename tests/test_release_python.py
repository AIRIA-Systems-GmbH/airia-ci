# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""reusable-release-python.yml: one pure-Python package, through airia-release (#151, slice 1).

What these tests hold: the workflow is workflow_call only; it publishes with
github.token and with no other token; --yes is passed only for a human
workflow_dispatch that turned dry-run off; index-upload true passes
--index-url and the password, and false passes --index-pull with the password
unset; a nested manifest installs its own directory; a GitHub-hosted runner is
refused. The compiled entry point is not this workflow.

Run: python3 -m unittest discover -s tests -p test_release_python.py
"""

from __future__ import annotations

import json
import re
import tomllib
import unittest

import yaml
from steps import ROOT, Runner

WORKFLOW = ROOT / ".github/workflows/reusable-release-python.yml"
DOC = yaml.safe_load(WORKFLOW.read_text())
CALL = DOC[True]["workflow_call"]  # PyYAML reads the key `on` as True
JOB = DOC["jobs"]["release"]
PINS = tomllib.loads((ROOT / "pins.toml").read_text())
INDEX = "https://pypi.de.airia.systems/simple/"
REFUSE = "Refuse a GitHub-hosted runner"
PUBLISH = "Install airia-release and publish the package"
THOR = '["self-hosted","linux","x64","thor"]'

# Records every invocation. `venv .release` plants the fake airia-release where
# the workflow executes it, so the test's fake is what publish runs.
FAKE_UV = """\
#!/bin/sh
printf '%s\\n' "uv $*" >> "$UV_LOG"
if [ "$1" = venv ]; then
  printf '%s\\n' "UV_PYTHON=${UV_PYTHON-}" >> "$UV_LOG"
fi
if [ -n "${AIRIA_PYPI_PASSWORD+x}" ]; then
  printf '%s\\n' "password=set" >> "$UV_LOG"
else
  printf '%s\\n' "password=unset" >> "$UV_LOG"
fi
if [ "$1" = venv ] && [ "$2" = .release ]; then
  src="$(command -v airia-release)"
  if [ -z "$src" ]; then
    echo "fake uv: airia-release is not on PATH" >&2
    exit 1
  fi
  mkdir -p .release/bin
  cp "$src" .release/bin/airia-release
  chmod +x .release/bin/airia-release
fi
exit 0
"""

# argv on the first line, then whether the index password was in the environment.
# The password itself is never written: a secret on a command line or in a log is the leak.
FAKE_RELEASE = """\
#!/bin/sh
printf '%s\\n' "$*" >> "$RELEASE_LOG"
if [ -n "${AIRIA_PYPI_PASSWORD+x}" ]; then
  printf '%s\\n' "password=set" >> "$RELEASE_LOG"
else
  printf '%s\\n' "password=unset" >> "$RELEASE_LOG"
fi
printf '%s\\n' "token=${GH_TOKEN-unset}" >> "$RELEASE_LOG"
exit 0
"""


def step(name: str) -> dict:
    (found,) = [item for item in JOB["steps"] if item.get("name") == name]
    return found


class Shape(unittest.TestCase):
    def test_it_is_called_only_and_has_no_outputs_yet(self):
        """Slice 1 adds a call contract of inputs and one secret. Outputs wait until a caller needs one."""
        self.assertEqual(set(DOC[True]), {"workflow_call"})
        self.assertNotIn("outputs", CALL)

    def test_the_inputs_and_their_defaults(self):
        """A single-package repository passes only its name: dry-run rehearses, and the index upload stays on."""
        self.assertEqual(set(CALL["inputs"]), {
            "package", "manifest", "airia-release-version", "dry-run", "index-upload", "runner"})
        self.assertIs(CALL["inputs"]["package"]["required"], True)
        self.assertNotIn("default", CALL["inputs"]["package"])
        self.assertEqual(CALL["inputs"]["manifest"]["default"], "pyproject.toml")
        self.assertEqual(CALL["inputs"]["airia-release-version"]["default"], "0.8.1")
        self.assertEqual(CALL["inputs"]["airia-release-version"]["default"], PINS["release"]["airia-release"])
        self.assertIs(CALL["inputs"]["dry-run"]["default"], True)
        self.assertIs(CALL["inputs"]["index-upload"]["default"], True)
        self.assertEqual(json.loads(CALL["inputs"]["runner"]["default"]), ["self-hosted", "linux", "x64", "thor"])
        self.assertEqual(CALL["inputs"]["runner"]["default"], THOR)
        for name, spec in CALL["inputs"].items():
            if name != "package":
                self.assertIs(spec["required"], False)

    def test_no_input_can_carry_a_token(self):
        """ADR-108: a PAT publishes as a person, so the release is provenance-unattested. There is no input to pass one."""
        for name in CALL["inputs"]:
            self.assertNotRegex(name.lower(), r"token|pat|password|secret|credential")

    def test_the_only_secret_is_the_index_password_and_it_is_optional(self):
        """The password exists for the interim upload. The cutover sets index-upload false and passes no secret."""
        self.assertEqual(set(CALL["secrets"]), {"AIRIA_PYPI_PASSWORD"})
        self.assertIs(CALL["secrets"]["AIRIA_PYPI_PASSWORD"]["required"], False)
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", WORKFLOW.read_text())), {"AIRIA_PYPI_PASSWORD"})

    def test_the_job_may_write_contents_and_nothing_is_attested(self):
        """ARC-18 asks contents: write only. id-token and attestations would mint a provenance this slice does not record."""
        self.assertEqual(DOC["permissions"], {})
        self.assertEqual(JOB["permissions"], {"contents": "write"})
        self.assertNotIn("env", JOB)
        text = WORKFLOW.read_text()
        self.assertIsNone(re.search(r"^\s*id-token\s*:", text, re.M))
        self.assertIsNone(re.search(r"^\s*attestations\s*:", text, re.M))
        self.assertNotRegex(text, r"uses:\s*\S*attest")
        for item in JOB["steps"]:
            self.assertNotIn("attest", item.get("uses", ""))

    def test_the_token_is_hard_coded_to_github_token(self):
        """The publish step's GH_TOKEN is github.token. The shell must not assign another, and no input is interpolated into it."""
        env = step(PUBLISH)["env"]
        self.assertEqual(env["GH_TOKEN"], "${{ github.token }}")
        self.assertNotIn("inputs.", env["GH_TOKEN"])
        self.assertNotRegex(step(PUBLISH)["run"], r"GH_TOKEN\s*=")
        self.assertNotIn("${{", step(PUBLISH)["run"])
        self.assertNotIn("${{", step(REFUSE)["run"])

    def test_the_password_is_mapped_only_by_an_expression_of_index_upload(self):
        """When index-upload is false the expression yields empty, so the secret value is not injected."""
        env = step(PUBLISH)["env"]
        self.assertEqual(
            env["AIRIA_PYPI_PASSWORD"],
            "${{ inputs.index-upload == true && secrets.AIRIA_PYPI_PASSWORD || '' }}",
        )
        self.assertNotIn("AIRIA_PYPI_PASSWORD", step(REFUSE).get("env", {}))

    def test_the_ref_and_the_default_branch_come_from_the_event(self):
        """--ref is the triggering commit, not a caller-supplied branch that could move, and --default-branch is the repository's own."""
        env = step(PUBLISH)["env"]
        self.assertEqual(env["REF"], "${{ github.sha }}")
        self.assertEqual(env["DEFAULT_BRANCH"], "${{ github.event.repository.default_branch }}")
        self.assertEqual(env["EVENT_NAME"], "${{ github.event_name }}")
        self.assertNotIn("event", CALL["inputs"])

    def test_the_checkout_is_the_caller_at_full_history(self):
        """Tags are <package>/v<version>. A shallow clone has no tags, and a repository: input could release someone else's repo."""
        checkout = JOB["steps"][1]
        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"], {"path": "subject", "fetch-depth": 0, "persist-credentials": False})

    def test_uv_is_pinned_like_the_toolchain_and_the_actions_cache_stays_off(self):
        """The runner has no uv until this step. The version is the gate's pin; the Actions cache is for hosted runners, which this job refuses."""
        setup = JOB["steps"][2]
        self.assertRegex(setup["uses"], r"^astral-sh/setup-uv@[0-9a-f]{40}$")
        self.assertEqual(setup["with"]["version"], PINS["tools"]["uv"])
        self.assertIs(setup["with"]["enable-cache"], False)
        self.assertNotIn("github-token", setup["with"])

    def test_it_runs_on_the_callers_labels_and_bounds_the_job(self):
        """The Thor pool is the default, as a JSON array. A hung publish must not hold a runner past the cap."""
        self.assertEqual(JOB["runs-on"], "${{ fromJSON(inputs.runner) }}")
        self.assertEqual(JOB["timeout-minutes"], 30)
        self.assertEqual(JOB["steps"][0]["name"], REFUSE)
        self.assertEqual(JOB["steps"][0]["env"], {"RUNNER": "${{ inputs.runner }}"})

    def test_the_index_url_is_not_an_input(self):
        """The interim upload has one index. An input would let a caller publish the package somewhere else."""
        self.assertNotIn("index-url", CALL["inputs"])
        self.assertIn(f'index_url="{INDEX}"', step(PUBLISH)["run"])

    def test_index_pull_is_documented_as_failing_closed_until_des_147(self):
        """airia-release 0.8.1 rejects --index-pull. The header, the input and the caller docs say that failure is intended."""
        description = CALL["inputs"]["index-upload"]["description"]
        self.assertIn("--index-pull", description)
        self.assertIn("DES-147", description)
        self.assertIn("0.8.1", description)
        self.assertIn("fail-closed", description)
        header = WORKFLOW.read_text().split("\non:\n", 1)[0]
        self.assertIn("--index-pull", header)
        self.assertIn("DES-147", header)
        self.assertIn("argument parser", header)
        self.assertIn("fail-closed", header)
        for path in (ROOT / "HOWTO.md", ROOT / "README.md"):
            text = path.read_text()
            self.assertIn("--index-pull", text, path.name)
            self.assertIn("DES-147", text, path.name)
            self.assertIn("0.8.1", text, path.name)
            self.assertIn("fail-closed", text, path.name)


class Behaviour(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        self.r.tool("uv", FAKE_UV)
        self.r.tool("airia-release", FAKE_RELEASE)
        self.uv_log = self.r.tmp / "uv.log"
        self.release_log = self.r.tmp / "release.log"

    def subject(self, python: str | None = "3.12"):
        root = self.r.tmp / "subject"
        root.mkdir(exist_ok=True)
        (root / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '1.2.3'\n")
        if python is None:
            version = root / ".python-version"
            if version.exists():
                version.unlink()
        else:
            (root / ".python-version").write_text(python + "\n")

    def env(self, **overrides) -> dict:
        base = {
            "PACKAGE": "demo",
            "MANIFEST": "pyproject.toml",
            "AIRIA_RELEASE_VERSION": "0.8.1",
            "DRY_RUN": "true",
            "EVENT_NAME": "pull_request",
            "INDEX_UPLOAD": "false",
            "REF": "abc123",
            "DEFAULT_BRANCH": "main",
            "GH_TOKEN": "sentinel-token",
            "AIRIA_PYPI_PASSWORD": "s3cret",
            "UV_LOG": str(self.uv_log),
            "RELEASE_LOG": str(self.release_log),
        }
        base.update(overrides)
        return base

    def publish(self, *, make_subject: bool = True, **overrides):
        if make_subject:
            self.subject()
        return self.r.run(WORKFLOW, PUBLISH, **self.env(**overrides))

    def refuse(self, **overrides):
        env = {"RUNNER": THOR, "RUNNER_ENVIRONMENT": "self-hosted"}
        env.update(overrides)
        return self.r.run(WORKFLOW, REFUSE, **env)

    def argv(self) -> str:
        self.assertTrue(self.release_log.is_file(), "airia-release was not run")
        return self.release_log.read_text().splitlines()[0]

    def release_report(self) -> list[str]:
        self.assertTrue(self.release_log.is_file(), "airia-release was not run")
        return self.release_log.read_text().splitlines()

    def test_a_github_hosted_runner_is_refused_before_any_publish(self):
        """The index does not resolve off the estate. A hosted runner must not reach checkout or airia-release."""
        done = self.refuse(RUNNER_ENVIRONMENT="github-hosted")
        self.assertEqual(done.returncode, 1)
        self.assertIn("refuses GitHub-hosted runners", done.stdout)
        self.assertIn("RUNNER_ENVIRONMENT=github-hosted", done.stdout)
        self.assertFalse(self.release_log.exists())

    def test_an_unset_runner_environment_is_refused(self):
        """Fail closed: a runner that does not say it is self-hosted is not asked to publish."""
        done = self.refuse(RUNNER_ENVIRONMENT="")
        self.assertEqual(done.returncode, 1)
        self.assertIn("RUNNER_ENVIRONMENT=unset", done.stdout)

    def test_hosted_image_labels_are_refused_even_when_the_environment_says_self_hosted(self):
        """The gate's public-repo value, ["ubuntu-24.04"], must not release. The index is internal-only."""
        done = self.refuse(RUNNER='["ubuntu-24.04"]')
        self.assertEqual(done.returncode, 1)
        self.assertIn("GitHub-hosted image", done.stdout)
        done = self.refuse(RUNNER='["self-hosted","ubuntu-24.04"]')
        self.assertEqual(done.returncode, 1)
        done = self.refuse(RUNNER='["self-hosted","windows-latest"]')
        self.assertEqual(done.returncode, 1)
        done = self.refuse(RUNNER='["self-hosted","macos-latest"]')
        self.assertEqual(done.returncode, 1)

    def test_a_label_that_only_contains_the_words_self_hosted_is_refused(self):
        """The label has to be self-hosted itself. A lookalike must not sneak a hosted job through."""
        done = self.refuse(RUNNER='["not-self-hosted","linux"]')
        self.assertEqual(done.returncode, 1)
        self.assertIn("self-hosted", done.stdout)

    def test_labels_that_are_not_a_json_array_are_refused(self):
        """runs-on is fromJSON of this input. A value that is not that array must not be treated as the Thor pool."""
        done = self.refuse(RUNNER="self-hosted")
        self.assertEqual(done.returncode, 1)
        self.assertIn("JSON array", done.stdout)

    def test_the_thor_pool_is_accepted(self):
        """The default labels are the ones this check allows, so a caller that passes nothing is not refused."""
        done = self.refuse()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("::error", done.stdout)

    def test_yes_only_when_a_human_dispatch_turns_dry_run_off(self):
        """--yes publishes. Only a person's workflow_dispatch, with dry-run false, is that act."""
        done = self.publish(EVENT_NAME="workflow_dispatch", DRY_RUN="false")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(
            self.argv(),
            "publish --repo o/r --package demo --ref abc123 --source subject --out release-out "
            "--default-branch main --manifest subject/pyproject.toml --governed demo "
            f"--index-pull {INDEX} --yes",
        )
        self.assertIn("confirm=--yes", done.stdout)

    def test_a_dispatch_rehearses_when_dry_run_stays_on(self):
        """dry-run defaults to true, so a dispatch that does not opt out must not publish."""
        done = self.publish(EVENT_NAME="workflow_dispatch", DRY_RUN="true")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("--dry-run", self.argv())
        self.assertNotIn("--yes", self.argv())

    def test_a_push_stays_a_dry_run_even_when_dry_run_is_false(self):
        """A push or a pull request is not a human release act. dry-run false on those events still rehearses."""
        for event in ("push", "pull_request", "schedule"):
            with self.subTest(event=event):
                self.release_log.unlink(missing_ok=True)
                done = self.publish(EVENT_NAME=event, DRY_RUN="false")
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertTrue(self.argv().endswith("--dry-run"))
                self.assertNotIn("--yes", self.argv())

    def test_index_url_and_the_password_reach_the_command_only_when_upload_is_on(self):
        """Until the index pulls, the upload is how the release arrives there. The password stays in the environment, never in argv."""
        done = self.publish(INDEX_UPLOAD="true")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"--index-url {INDEX}", self.argv())
        self.assertNotIn("--index-pull", self.argv())
        self.assertNotIn("s3cret", self.argv())
        self.assertNotIn("sentinel-token", self.argv())
        report = self.release_report()
        self.assertEqual(report[1], "password=set")
        self.assertIn("password=set", self.uv_log.read_text())

    def test_index_upload_off_passes_index_pull_and_drops_the_password(self):
        """DES-147 asks the index to pull. The upload flag must not go with that request, and the password must not be in the step's environment."""
        done = self.publish(INDEX_UPLOAD="false", AIRIA_PYPI_PASSWORD="s3cret")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"--index-pull {INDEX}", self.argv())
        self.assertNotIn("--index-url", self.argv())
        self.assertNotIn("s3cret", self.release_log.read_text())
        self.assertNotIn("s3cret", self.uv_log.read_text())
        self.assertNotIn("s3cret", done.stdout)
        self.assertEqual(self.release_report()[1], "password=unset")
        self.assertNotIn("password=set", self.uv_log.read_text())
        self.assertIn("password=unset", self.uv_log.read_text())

    def test_a_missing_password_fails_before_publish_when_upload_is_on(self):
        """index-upload true with no password must not publish a GitHub release and then skip the index write."""
        done = self.publish(INDEX_UPLOAD="true", AIRIA_PYPI_PASSWORD="")
        self.assertEqual(done.returncode, 1)
        self.assertIn("AIRIA_PYPI_PASSWORD is empty", done.stdout)
        self.assertFalse(self.release_log.exists())
        self.assertFalse(self.uv_log.exists())

    def test_the_child_keeps_the_token_the_step_was_given(self):
        """The shell does not swap GH_TOKEN. Whatever the step received (github.token, on Actions) is what airia-release sees."""
        done = self.publish()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.release_report()[2], "token=sentinel-token")
        self.assertNotIn("sentinel-token", self.argv())

    def test_the_install_uses_the_anonymous_index_and_the_subject_without_deps(self):
        """versioning-policy reads importlib.metadata in this venv. --no-deps installs the tree; the index read sends no password."""
        done = self.publish(INDEX_UPLOAD="false")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = self.uv_log.read_text().splitlines()
        self.assertEqual(lines[0], "uv venv .release")
        self.assertEqual(lines[1], "UV_PYTHON=3.12")
        self.assertEqual(
            lines[3],
            f"uv pip install --python .release --index {INDEX} airia-release==0.8.1",
        )
        self.assertEqual(lines[5], "uv pip install --python .release --no-deps ./subject")
        self.assertNotIn("--index-url", "".join(lines))

    def test_a_nested_manifest_installs_that_projects_directory(self):
        """versioning-policy reads the installed project's metadata. The checkout root is a different project when the manifest is nested."""
        self.subject()
        nested = self.r.tmp / "subject" / "packages" / "foo"
        nested.mkdir(parents=True)
        (nested / "pyproject.toml").write_text("[project]\nname = 'foo'\nversion = '4.5.6'\n")
        done = self.publish(make_subject=False, MANIFEST="packages/foo/pyproject.toml")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = self.uv_log.read_text().splitlines()
        self.assertEqual(lines[5], "uv pip install --python .release --no-deps subject/packages/foo")
        self.assertNotIn("no-deps ./subject", self.uv_log.read_text())
        self.assertIn("--manifest subject/packages/foo/pyproject.toml", self.argv())
        self.assertNotIn("--manifest subject/pyproject.toml ", self.argv())

    def test_the_subject_interpreter_is_kept_and_an_absent_pin_file_is_not_invented(self):
        """The venv must import the subject with its own Python. A leaked UV_PYTHON from the runner must not replace a missing file."""
        self.subject(python=None)
        done = self.publish(make_subject=False, UV_PYTHON="leak")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("UV_PYTHON=\n", self.uv_log.read_text())
        self.assertNotIn("leak", self.uv_log.read_text())

    def test_governed_is_the_package_name(self):
        """--governed is exactly the package name, not the repository and not the manifest path."""
        self.subject()
        done = self.publish(make_subject=False, PACKAGE="airia-sample")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("--package airia-sample", self.argv())
        self.assertIn("--governed airia-sample", self.argv())
        self.assertEqual(self.argv().count("airia-sample"), 2)

    def test_a_manifest_that_climbs_out_of_the_checkout_is_refused(self):
        """The caller gives a path inside its repository. A path that climbs out would publish a file the checkout did not fetch."""
        done = self.publish(MANIFEST="../pyproject.toml")
        self.assertEqual(done.returncode, 1)
        self.assertIn("path inside the checkout", done.stdout)
        self.assertFalse(self.release_log.exists())
        done = self.publish(MANIFEST="/etc/pyproject.toml")
        self.assertEqual(done.returncode, 1)
        self.assertIn("path inside the checkout", done.stdout)
        self.assertFalse(self.uv_log.exists())

    def test_a_missing_manifest_file_is_refused_before_install(self):
        """There is nothing to publish. Failing here leaves the index and GitHub untouched."""
        done = self.publish(make_subject=False, MANIFEST="pyproject.toml")
        self.assertEqual(done.returncode, 1)
        self.assertIn("subject/pyproject.toml does not exist", done.stdout)
        self.assertFalse(self.uv_log.exists())
        self.assertFalse(self.release_log.exists())

    def test_a_pin_that_is_not_an_exact_version_is_refused(self):
        """The pin is an input default, not a range and not a URL. A range would install whatever the index served last."""
        done = self.publish(AIRIA_RELEASE_VERSION="0.8.*")
        self.assertEqual(done.returncode, 1)
        self.assertIn("exact version", done.stdout)
        self.assertFalse(self.uv_log.exists())

    def test_a_package_name_that_could_be_a_flag_is_refused(self):
        """--package is the distribution name. A leading dash would be another flag, and --governed would copy it."""
        done = self.publish(PACKAGE="-demo")
        self.assertEqual(done.returncode, 1)
        self.assertIn("distribution name", done.stdout)
        self.assertFalse(self.release_log.exists())

    def test_a_value_other_than_true_or_false_is_refused(self):
        """A typo must not publish to GitHub while skipping the index, or the reverse."""
        done = self.publish(DRY_RUN="yes")
        self.assertEqual(done.returncode, 1)
        self.assertIn("dry-run must be true or false", done.stdout)
        self.assertFalse(self.uv_log.exists())
        done = self.publish(DRY_RUN="false", INDEX_UPLOAD="yes")
        self.assertEqual(done.returncode, 1)
        self.assertIn("index-upload must be true or false", done.stdout)
        self.assertFalse(self.release_log.exists())

    def test_a_dispatch_that_publishes_can_still_upload(self):
        """The one real publish (dispatch, dry-run false) still takes the interim index upload when that input is on."""
        done = self.publish(EVENT_NAME="workflow_dispatch", DRY_RUN="false", INDEX_UPLOAD="true")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(self.argv().endswith(f"--index-url {INDEX} --yes"))
        self.assertEqual(self.release_report()[1], "password=set")
        self.assertNotIn("s3cret", self.argv())


if __name__ == "__main__":
    unittest.main()
