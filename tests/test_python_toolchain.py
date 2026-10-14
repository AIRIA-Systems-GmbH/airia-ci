# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The python-toolchain action: a project may add to the standard, never weaken it.

What these tests hold: an input below the standard (a lower coverage floor, a
type checker outside the accepted list, a Python the standard does not allow)
is refused, with every reason at once; a uv.lock that resolves a gate tool
below its pin, or lacks one that must run in the project, is refused; a
workflow step that runs a tool unpinned is refused, while a pinned one and a
permission rule that merely names one are not; each tool runs at the locked
version when the project locks it, else at the pin; gitleaks is refused unless
its digest is the pinned one.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import platform
import sys
import tarfile
import textwrap
import unittest
from unittest import mock

from steps import ROOT, Runner

ACTION = ROOT / ".github/actions/python-toolchain/action.yml"
SCRIPT = ACTION.parent / "toolchain.py"
spec = importlib.util.spec_from_file_location("toolchain", SCRIPT)
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)
PINS = tc.load_pins(ROOT / "pins.toml")
T = PINS["tools"]


def lock(**packages: str) -> str:
    return "version = 1\n" + "".join(f'\n[[package]]\nname = "{n}"\nversion = "{v}"\n' for n, v in packages.items())


GOOD_LOCK = lock(pytest=T["pytest"], pytest_cov=T["pytest-cov"], coverage=T["coverage"])


class Project:
    """A scratch checkout of a consumer project."""

    def __init__(self, runner: Runner, *, python: str | None = "3.12", lockfile: str | None = GOOD_LOCK,
                 pyproject: str = "[project]\nname = 'p'\n"):
        self.root = runner.tmp / "project"
        (self.root / ".github/workflows").mkdir(parents=True)
        if python is not None:
            (self.root / ".python-version").write_text(python + "\n")
        if lockfile is not None:
            (self.root / "uv.lock").write_text(lockfile)
        (self.root / "pyproject.toml").write_text(pyproject)

    def workflow(self, name: str, text: str) -> None:
        (self.root / ".github/workflows" / name).write_text(textwrap.dedent(text))


class Standard(unittest.TestCase):
    def setUp(self):
        self.p = Project(Runner(self))

    def standard(self, **env):
        base = {"TYPE_CHECKER": "pyright", "COVERAGE_FLOOR": "90", "GITHUB_REPOSITORY": "o/r", "PYTHON_VERSION": ""}
        return tc.standard(PINS, {**base, **env}, self.p.root)

    def refused(self, **env) -> list[str]:
        with self.assertRaises(tc.Refused) as cm:
            self.standard(**env)
        return list(cm.exception.args)

    def test_the_baseline_passes_and_names_what_to_provision(self):
        self.assertEqual(self.standard(), {"uv": T["uv"], "python": "3.12", "pyyaml": PINS["harness"]["pyyaml"]})

    def test_an_input_python_wins_over_the_file_and_may_name_a_patch(self):
        self.assertEqual(self.standard(PYTHON_VERSION="3.13.5")["python"], "3.13.5")

    def test_a_raised_floor_is_accepted(self):
        self.standard(COVERAGE_FLOOR="97.5")

    def test_a_lowered_floor_is_refused(self):
        (reason,) = self.refused(COVERAGE_FLOOR="85")
        self.assertIn("below the standard's 90", reason)

    def test_a_floor_that_is_not_a_number_is_refused(self):
        self.assertIn("not a number", self.refused(COVERAGE_FLOOR="ninety")[0])

    def test_ty_is_refused(self):
        self.assertIn("ty is not accepted", self.refused(TYPE_CHECKER="ty")[0])

    def test_mypy_strict_only_where_it_was_already_the_gate(self):
        self.assertIn("use pyright", self.refused(TYPE_CHECKER="mypy-strict")[0])
        self.standard(TYPE_CHECKER="mypy-strict", GITHUB_REPOSITORY="AIRIA-Systems-GmbH/AIRIA-process")

    def test_a_python_outside_the_allowed_list_is_refused(self):
        self.assertIn("not one the standard allows", self.refused(PYTHON_VERSION="3.11")[0])

    def test_a_missing_python_version_file_is_refused_not_guessed(self):
        (self.p.root / ".python-version").unlink()
        self.assertIn(".python-version is missing", self.refused()[0])

    def test_every_reason_is_reported_at_once(self):
        self.assertEqual(len(self.refused(TYPE_CHECKER="ty", COVERAGE_FLOOR="50", PYTHON_VERSION="2.7")), 3)


class Lock(unittest.TestCase):
    def problems(self, lockfile: str, checker: str = "pyright", pyproject: dict | None = None) -> list[str]:
        r = Runner(self)
        p = Project(r, lockfile=lockfile)
        return tc.lock_problems(PINS, tc.locked(p.root), checker, pyproject or {})

    def test_the_project_tools_at_their_pins_pass(self):
        self.assertEqual(self.problems(GOOD_LOCK), [])

    def test_a_newer_locked_tool_passes(self):
        self.assertEqual(self.problems(GOOD_LOCK + lock(ruff="99.0.0")[len("version = 1\n"):]), [])

    def test_a_tool_locked_below_its_pin_is_refused(self):
        # Underscores and case are the same package to uv: pytest_cov is pytest-cov.
        old = lock(pytest=T["pytest"], PyTest_Cov="1.0.0", coverage=T["coverage"], ruff="0.0.1")
        found = self.problems(old)
        self.assertEqual(len(found), 2)
        self.assertIn("pytest-cov 1.0.0, below", found[0] + found[1])
        self.assertIn("ruff 0.0.1, below", found[0] + found[1])

    def test_a_missing_project_tool_is_refused_with_the_command_to_add_it(self):
        (reason,) = self.problems(lock(pytest=T["pytest"], coverage=T["coverage"]))
        self.assertIn(f"uv add --dev pytest-cov=={T['pytest-cov']}", reason)

    def test_mypy_must_be_locked_under_mypy_strict(self):
        self.assertIn("mypy is not in uv.lock", self.problems(GOOD_LOCK, checker="mypy-strict")[0])

    def test_import_linter_must_be_locked_when_configured(self):
        found = self.problems(GOOD_LOCK, pyproject={"tool": {"importlinter": {"root_package": "p"}}})
        self.assertIn("import-linter is not in uv.lock", found[0])

    def test_no_lock_is_refused(self):
        r = Runner(self)
        p = Project(r, lockfile=None)
        with self.assertRaises(tc.Refused):
            tc.locked(p.root)

    def test_a_pre_release_compares_by_its_release(self):
        self.assertEqual(tc.version("1.2.3rc1"), (1, 2, 3))
        self.assertLess(tc.version("0.9"), tc.version("0.17.0"))


class Unpinned(unittest.TestCase):
    CASES = {
        "uv run --with ty ty check src/": ["ty"],
        "uv run --with ty==0.0.20 ty check src/": [],
        "uv run --with=ty,ruff==0.17.0 ty check": ["ty"],
        "uvx ruff check .": ["ruff"],
        "uvx ruff@0.17.0 check .": [],
        "uvx ruff@latest check .": ["ruff@latest"],
        "uvx --python 3.12 ruff check": ["ruff"],
        "uvx -q --from ruff==0.17.0 ruff check": [],
        "uvx --from ruff ruff check": ["ruff"],
        "uv tool run reuse lint": ["reuse"],
        "pipx run ruff==0.16.10 check .": [],
        "pipx run --spec ruff ruff check": ["ruff"],
        "uv run ruff check .": [],
        "echo done && uvx zizmor .github": ["zizmor"],
        "# uvx ruff check, in a comment": [],
        "echo 'unbalanced": [],
    }

    def test_each_form(self):
        for line, expected in self.CASES.items():
            with self.subTest(line=line):
                self.assertEqual(tc.unpinned(line), expected)


class Workflows(unittest.TestCase):
    def test_only_run_steps_are_read_in_workflows_and_actions(self):
        r = Runner(self)
        p = Project(r)
        p.workflow("ci.yml", """\
            on: pull_request
            jobs:
              lint:
                runs-on: x
                steps:
                  - run: |
                      uv sync --locked
                      uv run --with ty \\
                        ty check src/
                  - uses: some/action@abc
              review:
                uses: o/r/.github/workflows/w.yml@v1
                with:
                  extra-allow: |
                    Bash(uv run --with ruff ruff:*)
            """)
        (p.root / ".github/actions/claude-toolchain").mkdir(parents=True)
        (p.root / ".github/actions/claude-toolchain/action.yml").write_text(
            "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: uvx reuse lint\n")
        p.workflow("empty.yml", "")
        found = tc.workflow_problems(p.root)
        self.assertEqual(len(found), 2, found)
        self.assertTrue(found[0].startswith(".github/workflows/ci.yml: `uv run --with ty"), found[0])
        self.assertIn("runs ty unpinned", found[0])
        self.assertIn(".github/actions/claude-toolchain/action.yml", found[1])

    def test_check_reports_lock_and_workflow_problems_together(self):
        r = Runner(self)
        p = Project(r, lockfile=None)
        p.workflow("ci.yml", "jobs:\n  a:\n    steps:\n      - run: uvx ruff check\n")
        with self.assertRaises(tc.Refused) as cm:
            tc.check(PINS, {"TYPE_CHECKER": "pyright"}, p.root)
        self.assertEqual(len(cm.exception.args), 2)


FAKE_UV = """\
#!/bin/sh
echo "$UV_TOOL_DIR|$UV_TOOL_BIN_DIR|$*" >> "$(dirname "$0")/uv-calls"
case "$*" in *broken*) echo "no such version" >&2; exit 2;; esac
"""


class Install(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        self.r.tool("uv", FAKE_UV)
        self.p = Project(self.r, lockfile=GOOD_LOCK + lock(reuse="99.1")[len("version = 1\n"):])
        self.path = self.r.tmp / "github-path"

    def install(self, tools: str, pins: dict = PINS) -> list[str]:
        env = self.r.env(TOOLS=tools, GITHUB_PATH=str(self.path))
        with mock.patch.dict(os.environ, env):
            return tc.install(pins, env, self.p.root)

    def uv_calls(self) -> list[str]:
        f = self.r.bin / "uv-calls"
        return f.read_text().splitlines() if f.exists() else []

    def test_an_unlocked_tool_is_installed_at_its_pin_in_the_tools_dir(self):
        done = self.install("ruff pyright")
        base = self.r.tmp / tc.TOOLS_DIR
        self.assertEqual(self.uv_calls(), [
            f"{base}/envs|{base}/bin|tool install --quiet ruff=={T['ruff']}",
            f"{base}/envs|{base}/bin|tool install --quiet pyright[nodejs]=={T['pyright']}",
        ])
        self.assertEqual(done, [f"ruff {T['ruff']} (pinned)", f"pyright {T['pyright']} (pinned)"])
        self.assertEqual(self.path.read_text().splitlines(), [f"{base}/bin"])

    def test_a_locked_tool_runs_at_the_locked_version(self):
        self.assertEqual(self.install("reuse"), ["reuse 99.1 (locked)"])
        self.assertIn("reuse==99.1", self.uv_calls()[0])

    def test_a_locked_tool_already_in_the_project_environment_is_used_from_there(self):
        (self.p.root / ".venv/bin").mkdir(parents=True)
        (self.p.root / ".venv/bin/reuse").write_text("")
        self.assertEqual(self.install("reuse"), ["reuse 99.1 (locked, from the project environment)"])
        self.assertEqual(self.uv_calls(), [])
        # GITHUB_PATH prepends, so the project's environment, written last, comes first.
        self.assertEqual(self.path.read_text().splitlines()[-1], str(self.p.root / ".venv/bin"))

    def test_zizmor_brings_the_standard_policy(self):
        self.install("zizmor")
        self.assertEqual(self.r.github_env.read_text(), f"ZIZMOR_CONFIG={ACTION.parent / 'zizmor.yml'}\n")

    def test_an_unknown_tool_is_refused(self):
        with self.assertRaises(tc.Refused) as cm:
            self.install("ty")
        self.assertIn("unknown tool 'ty'", cm.exception.args[0])

    def test_a_failed_install_is_refused_with_uvs_reason(self):
        pins = {**PINS, "tools": {**T, "ruff": "broken"}}
        with self.assertRaises(tc.Refused) as cm:
            self.install("ruff", pins)
        self.assertIn("no such version", cm.exception.args[0])

    def gitleaks_pins(self, digest: str | None) -> dict:
        tarball = self.r.tmp / "gl.tar.gz"
        exe = self.r.tmp / "gitleaks"
        exe.write_text("#!/bin/sh\necho gitleaks\n")
        with tarfile.open(tarball, "w:gz") as tar:
            tar.add(exe, arcname="gitleaks")
        os.environ["AIRIA_GITLEAKS_URL"] = tarball.as_uri()
        self.addCleanup(os.environ.pop, "AIRIA_GITLEAKS_URL")
        arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]
        key = f"{platform.system().lower()}_{arch}"
        digests = {key: digest or hashlib.sha256(tarball.read_bytes()).hexdigest()} if digest != "none" else {}
        return {**PINS, "gitleaks": {"sha256": digests}}

    def test_gitleaks_with_the_pinned_digest_is_installed(self):
        done = self.install("gitleaks", self.gitleaks_pins(None))
        self.assertEqual(done, [f"gitleaks {T['gitleaks']} (pinned, digest checked)"])
        self.assertTrue(os.access(self.r.tmp / tc.TOOLS_DIR / "bin/gitleaks", os.X_OK))

    def test_gitleaks_with_another_digest_is_refused(self):
        with self.assertRaises(tc.Refused) as cm:
            self.install("gitleaks", self.gitleaks_pins("0" * 64))
        self.assertIn("not the pinned", cm.exception.args[0])
        self.assertFalse((self.r.tmp / tc.TOOLS_DIR / "bin/gitleaks").exists())

    def test_gitleaks_on_a_platform_with_no_digest_is_refused(self):
        with self.assertRaises(tc.Refused) as cm:
            self.install("gitleaks", self.gitleaks_pins("none"))
        self.assertIn("no pinned gitleaks digest", cm.exception.args[0])


PASSTHROUGH_UV = """\
#!/bin/sh
# `uv run --no-project --with X python3 ARGS` -> `python3 ARGS`; `uv sync` is recorded.
echo "$*" >> "$(dirname "$0")/uv-calls"
while [ $# -gt 0 ] && [ "$1" != python3 ]; do shift; done
[ $# -gt 0 ] && exec "$@"
exit 0
"""


class Steps(unittest.TestCase):
    """The shipped run: blocks of action.yml, against a scratch project."""

    def setUp(self):
        self.r = Runner(self)
        self.r.tool("uv", PASSTHROUGH_UV)
        self.p = Project(self.r)
        self.path = self.r.tmp / "github-path"

    def run_step(self, name: str, **env):
        return self.r.run(ACTION, name, cwd=self.p.root, GITHUB_PATH=str(self.path), **env)

    def test_standard_writes_what_setup_uv_needs(self):
        p = self.run_step("Check the inputs against the standard", PYTHON_VERSION="", TYPE_CHECKER="pyright",
                          COVERAGE_FLOOR="90")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.r.outputs(), {"uv": T["uv"], "python": "3.12", "pyyaml": PINS["harness"]["pyyaml"]})

    def test_standard_fails_the_step_with_one_error_per_reason(self):
        p = self.run_step("Check the inputs against the standard", PYTHON_VERSION="", TYPE_CHECKER="ty",
                          COVERAGE_FLOOR="80")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("::error title=AIRIA standard::"), 2)

    def test_the_project_is_installed_from_the_lock_only(self):
        self.assertEqual(self.run_step("Install the locked project").returncode, 0)
        self.assertEqual((self.r.bin / "uv-calls").read_text(), "sync --locked\n")

    def test_check_passes_a_conforming_project(self):
        p = self.run_step("Check uv.lock and the workflows against the pins", TYPE_CHECKER="pyright", PYYAML="6.0.3")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("meet the pins", p.stdout)
        self.assertIn("--with pyyaml==6.0.3", (self.r.bin / "uv-calls").read_text())

    def test_check_fails_an_unpinned_workflow(self):
        self.p.workflow("ci.yml", "jobs:\n  a:\n    steps:\n      - run: uv run --with ty ty check\n")
        p = self.run_step("Check uv.lock and the workflows against the pins", TYPE_CHECKER="pyright", PYYAML="6.0.3")
        self.assertEqual(p.returncode, 1)
        self.assertIn("runs ty unpinned", p.stdout)

    def test_install_reports_each_tool_in_the_summary(self):
        p = self.run_step("Install the pinned tools", TOOLS="ruff")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"ruff {T['ruff']} (pinned)", self.r.summary.read_text())

    def test_install_failure_fails_the_step_through_the_pipe(self):
        p = self.run_step("Install the pinned tools", TOOLS="ty")
        self.assertEqual(p.returncode, 1)


class Services(unittest.TestCase):
    """The test job's service slots and job-env, as the respond workflow's contract has them."""

    STARTED = json.dumps({"postgres": {"id": "a", "ports": {"5432": "49153"}}, "qdrant": {"ports": {"6333": "49154"}}})

    def test_started_services_are_addressed_and_mapped(self):
        out = tc.services({"SERVICES": self.STARTED, "JOB_ENV": "# a comment\n\nDB=${CI_POSTGRES_DSN}\nQ=$CI_QDRANT_URL/x\nTERM=dumb"})
        self.assertEqual(out, {
            "CI_POSTGRES_DSN": "postgres://postgres:postgres@localhost:49153/postgres",
            "CI_QDRANT_URL": "http://localhost:49154",
            "DB": "postgres://postgres:postgres@localhost:49153/postgres",
            "Q": "http://localhost:49154/x",
            "TERM": "dumb",
        })

    def test_no_services_and_no_env_export_nothing(self):
        for started in ("", "null", "{}", '{"postgres": {"ports": {}}}'):
            with self.subTest(services=started):
                self.assertEqual(tc.services({"SERVICES": started}), {})

    def test_naming_a_service_that_did_not_start_is_refused(self):
        with self.assertRaises(tc.Refused) as cm:
            tc.services({"SERVICES": "{}", "JOB_ENV": "DB=$CI_POSTGRES_DSN"})
        self.assertIn("not configured", cm.exception.args[0])

    def test_a_line_that_is_not_key_value_is_refused(self):
        for line in ("JUST_A_NAME", "=value"):
            with self.subTest(line=line), self.assertRaises(tc.Refused):
                tc.services({"JOB_ENV": line})

    def test_a_context_that_is_not_json_is_refused(self):
        with self.assertRaises(tc.Refused):
            tc.services({"SERVICES": "{"})

    def test_the_step_writes_github_env(self):
        r = Runner(self)
        p = r.run(ACTION, "Export the service addresses and the job environment", SERVICES=self.STARTED,
                  JOB_ENV="TERM=dumb")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("job environment: CI_POSTGRES_DSN, CI_QDRANT_URL, TERM", p.stdout)
        self.assertEqual(r.github_env.read_text().splitlines()[-1], "TERM=dumb")


class Main(unittest.TestCase):
    def test_main_reports_a_refusal_as_errors_and_exit_1(self):
        r = Runner(self)
        p = Project(r, lockfile=None)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TYPE_CHECKER": "pyright"}), mock.patch.object(tc.Path, "cwd",
                                                                                          return_value=p.root), \
                mock.patch.object(sys, "stdout", out):
            self.assertEqual(tc.main(["check"]), 1)
        self.assertIn("::error title=AIRIA standard::uv.lock is missing", out.getvalue())


class Fixture(unittest.TestCase):
    def test_the_self_test_project_meets_the_pins(self):
        # self-test runs the action against it; it must pass for the right reason.
        project = ROOT / "validation/python-toolchain/project"
        self.assertEqual(tc.lock_problems(PINS, tc.locked(project), "pyright", {}), [])
        self.assertIn((project / ".python-version").read_text().strip(), PINS["python"]["allowed"])
        self.assertEqual(tc.workflow_problems(project), [])


if __name__ == "__main__":
    unittest.main()
