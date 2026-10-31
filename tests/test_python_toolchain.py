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

The ruff floor: a project's ruff configuration (each one, nested ones too)
must enable every rule the template's `select` enables; a narrower select or
a global ignore of a floor rule is refused, a wider one is free, and only a
test path may switch a floor rule off per file. The ratchet: `# noqa`
suppressions per rule may not rise above `.airia/ruff-baseline.json`; with no
baseline they are only warned about; `--write-baseline` records them.
Until pins.toml's cut-over day (UTC) both only warn, in the log and the job
summary, so the release reaches callers before they have onboarded; from it,
or earlier where a project opts in, they fail.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import io
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from steps import FAKE_RUFF, ROOT, Runner

ACTION = ROOT / ".github/actions/python-toolchain/action.yml"
SCRIPT = ACTION.parent / "toolchain.py"
spec = importlib.util.spec_from_file_location("toolchain", SCRIPT)
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)
PINS = tc.load_pins(ROOT / "pins.toml")
T = PINS["tools"]
CUTOVER = PINS["standard"]["ruff-floor-error-from"]
UTC = datetime.UTC


def instant(day: datetime.date, clock: str, offset: int = 0) -> datetime.datetime:
    """`clock` on `day`, at UTC+`offset` hours."""
    h, m = map(int, clock.split(":"))
    return datetime.datetime(day.year, day.month, day.day, h, m, tzinfo=datetime.timezone(datetime.timedelta(hours=offset)))


AFTER = instant(CUTOVER, "12:00")


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
# Like uv: an existing env without a Python is refused, not recreated (#111).
for env in "$UV_TOOL_DIR"/*/; do
  [ -d "$env" ] || continue
  [ -f "${env}bin/python" ] || { echo "error: Invalid environment at $env: missing Python executable" >&2; exit 2; }
done
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
        self.assertIn(f"AIRIA_TOOLCHAIN={SCRIPT}", self.r.github_env.read_text().splitlines(),
                      "the gate's later steps run the ruff floor by this name")

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
        self.assertEqual(self.r.github_env.read_text().splitlines()[-1], f"ZIZMOR_CONFIG={ACTION.parent / 'zizmor.yml'}")

    def test_an_unknown_tool_is_refused(self):
        with self.assertRaises(tc.Refused) as cm:
            self.install("ty")
        self.assertIn("unknown tool 'ty'", cm.exception.args[0])

    def test_a_failed_install_is_refused_with_uvs_reason(self):
        pins = {**PINS, "tools": {**T, "ruff": "broken"}}
        with self.assertRaises(tc.Refused) as cm:
            self.install("ruff", pins)
        self.assertIn("no such version", cm.exception.args[0])

    def tool_env(self, tool: str, python: str | None) -> Path:
        """envs/<tool> as an earlier job left it: bin/python a file, a dangling link, or absent."""
        env = self.r.tmp / tc.TOOLS_DIR / "envs" / tool
        (env / "bin").mkdir(parents=True)
        (env / "marker").write_text("")
        if python == "file":
            (env / "bin/python").write_text("")
        elif python == "dangling":
            (env / "bin/python").symlink_to(self.r.tmp / "uv-python-removed/bin/python3")
        return env

    def test_an_env_whose_python_is_gone_is_rebuilt(self):
        # A RUNNER_TEMP kept between jobs (org-orlando-1) failed every job that landed there.
        for python in ("dangling", None):
            with self.subTest(python=python):
                self.setUp()
                env = self.tool_env("reuse", python)
                with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    self.assertEqual(self.install("reuse"), ["reuse 99.1 (locked)"])
                self.assertFalse(env.exists())
                self.assertEqual(len(self.uv_calls()), 1)
                self.assertIn(f"reuse: rebuilt the tool env {env}", out.getvalue())

    def test_a_healthy_env_is_left_alone(self):
        env = self.tool_env("reuse", "file")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(self.install("reuse"), ["reuse 99.1 (locked)"])
        self.assertTrue((env / "marker").exists())
        self.assertEqual(out.getvalue(), "")

    def test_a_failed_install_after_a_rebuild_is_still_refused_with_uvs_reason(self):
        env = self.tool_env("ruff", None)
        pins = {**PINS, "tools": {**T, "ruff": "broken"}}
        with mock.patch("sys.stdout", new_callable=io.StringIO), self.assertRaises(tc.Refused) as cm:
            self.install("ruff", pins)
        self.assertFalse(env.exists())
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


TEMPLATE_RUFF = """\
[tool.ruff.lint]
select = ["E", "W", "F", "I", "B", "C4", "UP", "SIM"]
ignore = ["E501"]
"""


class RuffFloor(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        self.r.tool("ruff", FAKE_RUFF)
        self.root = self.r.tmp / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def project(self, pyproject: str, **files: str) -> None:
        (self.root / "pyproject.toml").write_text("[project]\nname = 'p'\n" + pyproject)
        for rel, text in files.items():
            path = self.root / re.sub(r"_(py|toml)$", r".\1", rel.replace("__", "/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)

    def ruff(self, write: bool = False, at: datetime.datetime = AFTER, **env) -> list[str]:
        """The check at `at`: by default after the cut-over, where it fails as the standard means it to."""
        env = self.r.env(**env)
        with mock.patch.dict(os.environ, env):
            return tc.ruff(PINS, env, self.root, write, at)

    def refused(self, **kw) -> list[str]:
        with self.assertRaises(tc.Refused) as cm:
            self.ruff(**kw)
        return list(cm.exception.args)

    def test_the_floor_is_what_the_template_enables(self):
        # The template is the one source: its `ignore` of E501 is part of the floor.
        with mock.patch.dict(os.environ, self.r.env()):
            floor = tc.ruff_floor()
        self.assertEqual(floor, {"B006", "B008", "C401", "E401", "E402", "E711", "F401", "F811", "I001",
                                 "SIM108", "UP031", "W291"})
        self.assertIn('select = ["E", "W", "F", "I", "B", "C4", "UP", "SIM"]', tc.TEMPLATE.read_text())

    def test_the_baseline_is_never_docs_so_a_pr_that_raises_it_runs_lint(self):
        gate = (ROOT / ".github/workflows/reusable-python-gate.yml").read_text()
        always_code = re.search(r"always_code=\((.*?)\)", gate, re.S).group(1).split()
        self.assertIn(tc.BASELINE.split("/")[0], always_code)

    def test_a_project_on_the_template_passes(self):
        # init.py writes the template's [tool.ruff] where a project has none, so no select means this.
        self.project(TEMPLATE_RUFF)
        self.assertEqual(self.ruff(), [])

    def test_no_ruff_configuration_is_ruffs_default_set_and_refused(self):
        self.project("")
        (reason,) = self.refused()
        self.assertIn("pyproject.toml: ruff does not enable the floor's B006, B008, C401, I001, SIM108, UP031, W291",
                      reason)

    def test_a_narrower_select_is_refused_and_names_the_missing_rules(self):
        self.project(TEMPLATE_RUFF.replace('"C4", "UP", "SIM"', '"UP"'))
        (reason,) = self.refused()
        self.assertIn("does not enable the floor's C401, SIM108: a narrower `select`", reason)

    def test_a_wider_select_passes(self):
        self.project(TEMPLATE_RUFF + 'extend-select = ["RUF"]\n')
        self.assertEqual(self.ruff(), [])
        self.project(TEMPLATE_RUFF.replace('select = ["E"', 'select = ["ALL", "E"'))
        self.assertEqual(self.ruff(), [])

    def test_a_global_ignore_of_a_floor_rule_is_a_narrowing(self):
        self.project(TEMPLATE_RUFF.replace('ignore = ["E501"]', 'ignore = ["E501", "B008"]'))
        self.assertIn("the floor's B008", self.refused()[0])
        self.project(TEMPLATE_RUFF + 'extend-ignore = ["UP"]\n')
        self.assertIn("the floor's UP031", self.refused()[0])

    def test_only_a_test_path_may_switch_floor_rules_off_per_file(self):
        allowed = ["tests/**", "src/pkg/tests/x.py", "test/a.py", "conftest.py", "src/test_a.py", "a_test.py"]
        lines = "".join(f'"{p}" = ["F401", "E402"]\n' for p in allowed)
        self.project(TEMPLATE_RUFF + "[tool.ruff.lint.per-file-ignores]\n" + lines + '"docs/**" = ["RUF012"]\n')
        self.assertEqual(self.ruff(), [], "a test path, or a rule outside the floor, is free")
        self.project(TEMPLATE_RUFF + '[tool.ruff.lint.per-file-ignores]\n"src/pkg/__init__.py" = ["F401"]\n'
                     '"!tests/**" = ["E402"]\n"contests/x.py" = ["E401"]\n')
        reasons = self.refused()
        self.assertEqual(len(reasons), 3)
        self.assertIn("per-file-ignores 'src/pkg/__init__.py' switches off the floor's F401: only test paths",
                      reasons[0])
        self.assertIn("'!tests/**' switches off the floor's E402", reasons[1], "a negated test path is everything else")

    def test_migrations_are_generated_code_ruff_never_checks(self):
        # Django's makemigrations writes them: switching floor rules off there is free, and their
        # suppression comments are not counted, since ruff skips the directory (Andrea, 2026-10-20).
        self.project(TEMPLATE_RUFF + '[tool.ruff.lint.per-file-ignores]\n"**/migrations/*" = ["F401", "E741"]\n'
                     '"app/migrations/*.py" = ["E402"]\n',
                     app__migrations__0001_initial_py="x  # FINDING(F401) # noqa\ny  # FINDING(E402)\n",
                     app__models_py="z  # FINDING(F401)\n")
        self.assertEqual(self.ruff(), [], "no floor problem, and no suppression outside migrations")
        self.project(TEMPLATE_RUFF, app__models_py="z  # FINDING(F401) # noqa\n")
        (notice,) = self.ruff()
        self.assertIn("1 `# noqa` suppressions (F401)", notice, "outside migrations a suppression still counts")

    def test_the_gate_and_ci_local_skip_what_the_toolchain_skips(self):
        skip = " ".join(tc.RUFF_SKIP)
        gate = (ROOT / ".github/workflows/reusable-python-gate.yml").read_text()
        local = (ROOT / "templates/python/ci-local.sh").read_text()
        self.assertIn(f"skip=({skip})", gate)
        self.assertIn(f"skip=({skip})", local)
        self.assertEqual(len(re.findall(r"uv run ruff (?!.*\$\{skip\[@\]\})", local)), 0,
                         "every ruff call in ci-local.sh skips migrations")

    def test_every_tracked_configuration_is_held_except_under_tests(self):
        self.project(TEMPLATE_RUFF, packages__a__pyproject_toml="[tool.ruff.lint]\nselect = ['F']\n",
                     packages__b__pyproject_toml="[project]\nname = 'b'\n",
                     tools__ruff_toml="[lint]\nselect = ['E']\n",
                     tests__fixtures__pyproject_toml="[tool.ruff.lint]\nselect = ['F']\n")
        reasons = self.refused()
        self.assertEqual([r.split(":")[0] for r in reasons], ["packages/a/pyproject.toml", "tools/ruff.toml"])

    def test_suppressions_without_a_baseline_are_warned_not_refused(self):
        self.project(TEMPLATE_RUFF, src__a_py="x  # FINDING(F401) # noqa\ny  # FINDING(E402)\nSYNTAX\n")
        (notice,) = self.ruff()
        self.assertIn("::warning title=Ruff suppressions::1 `# noqa` suppressions (F401) and no "
                      ".airia/ruff-baseline.json", notice)
        self.assertIn("--onboard", notice)

    def test_the_baseline_is_written_sorted_and_may_only_fall(self):
        src = "a  # FINDING(F401) # noqa\nb  # FINDING(F401) # noqa\nc  # FINDING(E402) # noqa: E402\n"
        self.project(TEMPLATE_RUFF, src__a_py=src)
        (wrote,) = self.ruff(write=True)
        self.assertIn("wrote .airia/ruff-baseline.json: 3 suppressions (E402, F401)", wrote)
        baseline = self.root / tc.BASELINE
        self.assertEqual(baseline.read_text(), '{\n  "E402": 1,\n  "F401": 2\n}\n')
        self.assertEqual(self.ruff(), [], "at the baseline: green, nothing to say")
        (self.root / "src/a.py").write_text(src + "d  # FINDING(F401) # noqa\ne  # FINDING(UP031) # noqa\n")
        reasons = self.refused()
        self.assertEqual([r.split(" ")[0] for r in reasons], ["F401", "UP031"])
        self.assertIn("F401 is suppressed 3 times, above the 2 in .airia/ruff-baseline.json", reasons[0])
        (self.root / "src/a.py").write_text("a  # FINDING(F401) # noqa\nc  # FINDING(E402)\n")
        (notice,) = self.ruff()
        self.assertIn("::notice title=Ruff suppressions::fewer suppressions", notice)
        self.ruff(write=True)
        self.assertEqual(json.loads(baseline.read_text()), {"F401": 1}, "the documented command lowers it")

    def test_the_paths_input_bounds_the_count(self):
        self.project(TEMPLATE_RUFF, src__a_py="a  # FINDING(F401) # noqa\n")
        self.assertEqual(self.ruff(PATHS="src"), self.ruff(PATHS=""))

    def test_the_cut_over_is_one_date_in_pins(self):
        self.assertIsInstance(CUTOVER, datetime.date, "a bare TOML date, so no string can mean another day")
        self.assertNotIsInstance(CUTOVER, datetime.datetime)

    def test_before_the_cut_over_a_narrowed_floor_and_a_risen_count_warn_and_stay_green(self):
        # Released on 2026-10-15 as an error, it would have turned two consumers red at once.
        self.project("", src__a_py="a  # FINDING(F401) # noqa\nb  # FINDING(F401) # noqa\n")
        (self.root / tc.BASELINE).parent.mkdir()
        (self.root / tc.BASELINE).write_text('{"F401": 1}\n')
        floor, risen = self.ruff(at=instant(CUTOVER - datetime.timedelta(days=1), "23:59"))
        title = f"::warning title=AIRIA standard (ruff floor; an error from {CUTOVER})::"
        self.assertTrue(floor.startswith(title + "pyproject.toml: ruff does not enable"), floor)
        self.assertTrue(risen.startswith(title + "F401 is suppressed 2 times, above the 1"), risen)
        summary = self.r.summary.read_text()
        self.assertIn(f"### Ruff floor: 2 problems, warnings until {CUTOVER} (UTC), errors from then", summary)
        self.assertIn("- F401 is suppressed 2 times", summary)
        self.assertIn("ruff check --fix, ruff format, ruff check --add-noqa, then the baseline", summary,
                      "the summary names how to get there")

    def test_from_the_cut_over_day_on_it_fails(self):
        self.project("")
        for at in (instant(CUTOVER, "00:00"), instant(CUTOVER + datetime.timedelta(days=30), "09:00")):
            self.assertIn("does not enable the floor's", self.refused(at=at)[0])
        self.assertFalse(self.r.summary.exists() and self.r.summary.read_text(), "a failure is not also a warning")

    def test_the_day_is_the_utc_day(self):
        self.project("")
        # 23:30 at UTC-2 on the eve is already the cut-over day in UTC; 00:30 at UTC+2 on the day is still the eve.
        self.refused(at=instant(CUTOVER - datetime.timedelta(days=1), "23:30", -2))
        self.assertEqual(len(self.ruff(at=instant(CUTOVER, "00:30", 2))), 1)

    def test_a_project_may_opt_into_failing_early_and_nothing_else(self):
        before = instant(CUTOVER - datetime.timedelta(days=7), "12:00")
        self.project('[tool.airia]\nruff-floor = "error"\n')
        self.assertIn("does not enable the floor's", self.refused(at=before)[0])
        self.project(TEMPLATE_RUFF + '[tool.airia]\nruff-floor = "error"\n')
        self.assertEqual(self.ruff(at=before), [], "opting in costs a project on the floor nothing")
        self.project(TEMPLATE_RUFF + '[tool.airia]\nruff-floor = "warning"\n')
        (reason,) = self.refused(at=AFTER)
        self.assertIn("[tool.airia] ruff-floor = 'warning': the only value is \"error\"", reason,
                      "after the cut-over there is no way back to warnings")

    def test_on_a_laptop_there_is_no_summary_to_write(self):
        self.project("")
        env = {k: v for k, v in self.r.env().items() if k != "GITHUB_STEP_SUMMARY"}
        with mock.patch.dict(os.environ, env, clear=True):
            (notice,) = tc.ruff(PINS, env, self.root, False, instant(CUTOVER - datetime.timedelta(days=1), "12:00"))
        self.assertIn("ruff does not enable", notice)

    def test_without_a_clock_it_reads_the_current_utc_time(self):
        self.project("")
        env = self.r.env()
        clock = mock.Mock(wraps=datetime, UTC=UTC)
        with mock.patch.dict(os.environ, env), mock.patch.object(tc, "datetime", clock):
            clock.datetime.now.return_value = instant(CUTOVER, "00:30", 2)
            self.assertEqual(len(tc.ruff(PINS, env, self.root, False)), 1)
            clock.datetime.now.return_value = AFTER
            self.assertRaises(tc.Refused, tc.ruff, PINS, env, self.root, False)
        clock.datetime.now.assert_called_with(UTC)

    def test_a_ruff_that_fails_or_cannot_be_read_is_refused(self):
        self.project(TEMPLATE_RUFF)
        self.assertIn("--show-settings", self.refused(FAKE_RUFF_FAIL="--show-settings")[0])
        self.assertIn("listed no enabled rules", self.refused(FAKE_RUFF_EMPTY="1")[0])
        self.assertIn("`ruff check` failed: fake ruff: failing --output-format", self.refused(
            FAKE_RUFF_FAIL="--output-format")[0])

    def test_outside_a_git_checkout_the_root_configurations_are_read(self):
        root = self.r.tmp / "plain"
        root.mkdir()
        (root / "ruff.toml").write_text("[lint]\nselect = ['F']\n")
        with mock.patch.dict(os.environ, self.r.env()):
            self.assertEqual([p.name for p in tc.ruff_configs(root)], ["ruff.toml"])

    def test_main_runs_it_and_writes_the_baseline_on_request(self):
        self.project(TEMPLATE_RUFF, src__a_py="a  # FINDING(F401) # noqa\n")
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.r.env()), mock.patch.object(tc.Path, "cwd", return_value=self.root), \
                mock.patch.object(sys, "stdout", out):
            self.assertEqual(tc.main(["ruff", "--write-baseline"]), 0)
            self.assertEqual(tc.main(["ruff"]), 0)
        self.assertIn("ruff: checked the floor and the suppressions", out.getvalue())
        self.assertTrue((self.root / tc.BASELINE).is_file())


class Fixture(unittest.TestCase):
    def test_the_self_test_project_meets_the_pins(self):
        # self-test runs the action against it; it must pass for the right reason.
        project = ROOT / "validation/python-toolchain/project"
        self.assertEqual(tc.lock_problems(PINS, tc.locked(project), "pyright", {}), [])
        self.assertIn((project / ".python-version").read_text().strip(), PINS["python"]["allowed"])
        self.assertEqual(tc.workflow_problems(project), [])


if __name__ == "__main__":
    unittest.main()
