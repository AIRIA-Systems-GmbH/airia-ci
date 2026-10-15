# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""reusable-python-gate.yml: the standard's gates, and the one check that sums them.

What these tests hold: no input switches a gate off; `gate` needs every gate
job and is red unless each one, and each of the caller's extra jobs,
succeeded (skipped counts as not passed); the review runs after `gate`, even a
red one, at the same tag as the actions; each gate step runs every check it
names and fails on any; pytest without a coverage figure fails rather than
passing unmeasured, and always prints its slowest tests.

The docs-only fast path: with `docs-paths` empty, or on any event but a pull
request, `changes` says code changed and everything runs, as before; a PR
whose every path (old and new, for a rename) is docs skips lint and test, and
`gate` accepts exactly those two skips and nothing else.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
import unittest
from pathlib import Path

import yaml
from steps import ROOT, Runner

WORKFLOW = ROOT / ".github/workflows/reusable-python-gate.yml"
DOC = yaml.safe_load(WORKFLOW.read_text())
JOBS = DOC["jobs"]
INPUTS = DOC[True]["workflow_call"]["inputs"]  # PyYAML reads the key `on` as True
PINS = tomllib.loads((ROOT / "pins.toml").read_text())

# A fake tool: records its argv, exits with $FAKE_<NAME>_EXIT.
FAKE = """\
#!/bin/sh
name="$(basename "$0" | tr 'a-z-' 'A-Z_')"
echo "$(basename "$0") $*" >> "$(dirname "$0")/calls"
eval "code=\\${{FAKE_${{name}}_EXIT:-0}}"
exit "$code"
"""
# pytest: also writes the JSON coverage report it was asked for, at $FAKE_PERCENT.
FAKE_PYTEST = """\
#!/bin/sh
echo "pytest $*" >> "$(dirname "$0")/calls"
for a in "$@"; do
  case "$a" in --cov-report=json:*) [ -n "$FAKE_PERCENT" ] && echo "{\\"totals\\": {\\"percent_covered\\": $FAKE_PERCENT}}" > "${a#--cov-report=json:}";; esac
done
exit "${FAKE_PYTEST_EXIT:-0}"
"""


class Shape(unittest.TestCase):
    def test_no_input_switches_a_gate_off(self):
        # May add, may not weaken: nothing named skip/disable/allow-failure exists to pass.
        for name in INPUTS:
            self.assertNotRegex(name, r"skip|disable|no-|allow-fail|continue")
        self.assertEqual(set(INPUTS), {
            "type-checker", "coverage-floor", "python-versions", "paths", "extra-jobs", "postgres-image",
            "qdrant-image", "job-env", "runner", "durations", "docs-paths", "extra-allow", "extra-deny",
            "extra-prompt"})

    def test_the_new_inputs_change_nothing_by_default(self):
        # Every existing caller keeps today's behaviour: no fast path, the 25 slowest tests listed.
        self.assertEqual(INPUTS["docs-paths"]["default"], "")
        self.assertEqual(INPUTS["durations"]["default"], 25)

    def test_the_defaults_are_the_standard(self):
        self.assertEqual(INPUTS["coverage-floor"]["default"], PINS["standard"]["coverage-floor"])
        self.assertEqual(INPUTS["type-checker"]["default"], PINS["standard"]["type-checkers"][0])

    def test_gate_needs_every_gate_job_and_runs_when_one_failed(self):
        gates = set(JOBS) - {"gate", "claude-review"}
        self.assertEqual(gates, {"changes", "lint", "test", "security"})
        self.assertEqual(set(JOBS["gate"]["needs"]), gates)
        self.assertEqual(JOBS["gate"]["if"], "always()")
        self.assertEqual(JOBS["gate"]["steps"][0]["env"]["CODE"], "${{ needs.changes.outputs.code }}")

    def test_no_gate_job_has_a_condition_or_tolerates_failure(self):
        # A job that can be skipped, or a step that may fail, is a gate that can be switched off.
        # The one condition: lint and test wait for `changes` and skip on a docs-only PR.
        # security never skips: REUSE and gitleaks hold for a doc too.
        conditions = {"lint": "needs.changes.outputs.code == 'true'", "test": "needs.changes.outputs.code == 'true'"}
        for name in ("changes", "lint", "test", "security"):
            job = JOBS[name]
            with self.subTest(job=name):
                self.assertEqual(job.get("if"), conditions.get(name))
                self.assertEqual(job.get("needs"), ["changes"] if name in conditions else None)
                self.assertNotIn("continue-on-error", job)
                for s in job["steps"]:
                    self.assertNotIn("continue-on-error", s)
                    if not s.get("uses", "").startswith("actions/checkout@"):
                        self.assertNotIn("if", s)

    def test_changes_reads_the_merge_commit_only_when_it_needs_to(self):
        checkout, diff = JOBS["changes"]["steps"]
        self.assertEqual(checkout["if"], "github.event_name == 'pull_request' && inputs.docs-paths != ''")
        self.assertEqual(checkout["with"], {"fetch-depth": 2, "persist-credentials": False})
        self.assertNotIn("ref", checkout["with"])  # the default on a pull request: refs/pull/N/merge
        self.assertEqual(JOBS["changes"]["outputs"], {"code": f"${{{{ steps.{diff['id']}.outputs.code }}}}"})

    def test_every_gate_job_starts_from_the_standard_toolchain(self):
        for name in ("lint", "test", "security"):
            checkout, toolchain = JOBS[name]["steps"][:2]
            with self.subTest(job=name):
                self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
                self.assertFalse(checkout["with"]["persist-credentials"])
                self.assertRegex(toolchain["uses"], r"^AIRIA-Systems-GmbH/airia-ci/\.github/actions/python-toolchain@")
                self.assertEqual(toolchain["with"]["coverage-floor"], "${{ inputs.coverage-floor }}")
                self.assertEqual(toolchain["with"]["type-checker"], "${{ inputs.type-checker }}")

    def test_the_review_runs_after_the_gate_even_a_red_one_and_at_the_same_tag(self):
        review = JOBS["claude-review"]
        self.assertEqual(review["needs"], ["gate"])
        self.assertTrue(review["if"].startswith("always() && github.event_name == 'pull_request'"))
        self.assertEqual(review["secrets"], {"CLAUDE_CODE_OAUTH_TOKEN": "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"})
        for name in ("runner", "extra-allow", "extra-deny", "extra-prompt"):
            self.assertEqual(review["with"][name], f"${{{{ inputs.{name} }}}}")
        tags = set(re.findall(r"AIRIA-Systems-GmbH/airia-ci/\S+@(\S+)", WORKFLOW.read_text()))
        self.assertEqual(len(tags), 1, tags)

    def test_every_review_input_the_gate_passes_exists_on_the_review(self):
        review = yaml.safe_load((ROOT / ".github/workflows/reusable-claude-review.yml").read_text())
        self.assertLessEqual(set(JOBS["claude-review"]["with"]), set(review[True]["workflow_call"]["inputs"]))

    def test_the_header_names_the_checks_github_reports(self):
        # `<caller job> / <called job>`, nested once more for the review.
        text = WORKFLOW.read_text()
        self.assertIn("`ci / gate` and `ci / claude-review / claude-review`", text)
        self.assertIn("gate", JOBS)
        review = yaml.safe_load((ROOT / ".github/workflows/reusable-claude-review.yml").read_text())
        self.assertEqual(list(review["jobs"]), ["claude-review"])


class Steps(unittest.TestCase):
    def setUp(self):
        self.r = Runner(self)
        for tool in ("ruff", "pyright", "mypy", "lint-imports", "reuse", "uv", "pip-audit", "gitleaks", "zizmor"):
            self.r.tool(tool, FAKE.format())
        self.r.tool("pytest", FAKE_PYTEST)
        self.project = self.r.tmp / "project"
        self.project.mkdir()
        (self.project / "pyproject.toml").write_text("[project]\nname = 'p'\n")

    def run_step(self, name: str, **env):
        return self.r.run(WORKFLOW, name, cwd=self.project, **env)

    def calls(self) -> list[str]:
        f = self.r.bin / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def test_ruff_runs_both_checks_on_every_path_even_when_the_first_fails(self):
        p = self.run_step("Ruff", PATHS="src tests", FAKE_RUFF_EXIT="1")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.calls(), ["ruff check src tests", "ruff format --check src tests"])

    def test_ruff_passes_when_both_pass(self):
        self.assertEqual(self.run_step("Ruff", PATHS=".").returncode, 0)

    def test_the_ruff_floor_step_runs_the_toolchains_check_on_the_paths(self):
        # The python-toolchain action exports AIRIA_TOOLCHAIN; its `ruff` command is tested with the action.
        fake = self.r.tmp / "toolchain.py"
        fake.write_text('import os, sys\nprint("toolchain", *sys.argv[1:], os.environ["PATHS"])\n'
                        'sys.exit(int(os.environ.get("FAKE_FLOOR_EXIT", "0")))\n')
        p = self.run_step("Ruff floor and suppressions", PATHS="src tests", AIRIA_TOOLCHAIN=str(fake))
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "toolchain ruff src tests"))
        p = self.run_step("Ruff floor and suppressions", PATHS=".", AIRIA_TOOLCHAIN=str(fake), FAKE_FLOOR_EXIT="1")
        self.assertEqual(p.returncode, 1)

    def test_pyright_checks_against_the_project_environment(self):
        p = self.run_step("Type check", PATHS="src", TYPE_CHECKER="pyright")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.calls(), [f"pyright --pythonpath {self.project.resolve()}/.venv/bin/python src"])

    def test_mypy_strict_is_strict(self):
        p = self.run_step("Type check", PATHS="src", TYPE_CHECKER="mypy-strict", FAKE_MYPY_EXIT="1")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.calls(), ["mypy --strict src"])

    def test_an_unknown_type_checker_fails(self):
        p = self.run_step("Type check", PATHS="src", TYPE_CHECKER="ty")
        self.assertEqual(p.returncode, 1)
        self.assertIn("no type checker ty", p.stdout)

    def test_import_contracts_run_when_pyproject_configures_them(self):
        (self.project / "pyproject.toml").write_text("[tool.importlinter]\nroot_package = 'p'\n")
        p = self.run_step("Import contracts", FAKE_LINT_IMPORTS_EXIT="1")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.calls(), ["lint-imports "])

    def test_import_contracts_run_from_an_importlinter_file(self):
        (self.project / ".importlinter").write_text("[importlinter]\n")
        self.assertEqual(self.run_step("Import contracts").returncode, 0)
        self.assertEqual(self.calls(), ["lint-imports "])

    def test_no_import_contracts_is_said_not_failed(self):
        p = self.run_step("Import contracts")
        self.assertEqual(p.returncode, 0)
        self.assertIn("No import-linter contracts", p.stdout)
        self.assertEqual(self.calls(), [])

    def test_pytest_at_or_over_the_floor_passes(self):
        p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="90.0", DURATIONS="25")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (call,) = self.calls()
        self.assertIn("--cov-fail-under=90", call)
        self.assertIn(f"--cov-report=json:{self.r.tmp}/coverage.json", call)

    def test_pytest_always_lists_the_slowest_tests(self):
        # The report is how a slow suite is found from CI; 0 lists every test, it does not switch it off.
        for n in ("25", "0", "100"):
            with self.subTest(durations=n):
                (self.r.bin / "calls").unlink(missing_ok=True)
                p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="95", DURATIONS=n)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                (call,) = self.calls()
                self.assertTrue(call.startswith(f"pytest --durations={n} "), call)

    def test_durations_that_are_not_a_count_fail_before_pytest(self):
        for n in ("", "-1", "2.5", "all"):
            with self.subTest(durations=n):
                p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="95", DURATIONS=n)
                self.assertEqual(p.returncode, 1)
                self.assertIn("::error title=durations::", p.stdout)
                self.assertEqual(self.calls(), [])

    def test_pytest_under_the_floor_fails_even_if_pytest_did_not(self):
        # pytest-cov's own fail-under is the first line; the report check is the second.
        p = self.run_step("Pytest at the coverage floor", FLOOR="92.5", FAKE_PERCENT="92.4", DURATIONS="25")
        self.assertEqual(p.returncode, 1)

    def test_no_coverage_report_fails_with_how_to_measure(self):
        # `--no-cov` in addopts: pytest passes, writes no report, and holds no floor.
        (self.r.tmp / "coverage.json").write_text('{"totals": {"percent_covered": 100}}')  # a stale one
        p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="", DURATIONS="25")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=No coverage figure::", p.stdout)
        self.assertIn("--cov=<package>", p.stdout)

    def test_a_failing_test_fails_the_step(self):
        p = self.run_step("Pytest at the coverage floor", FLOOR="90", FAKE_PERCENT="100", FAKE_PYTEST_EXIT="1",
                          DURATIONS="25")
        self.assertEqual(p.returncode, 1)

    def test_reuse_lints_the_whole_tree(self):
        self.assertEqual(self.run_step("Licences (REUSE)", FAKE_REUSE_EXIT="1").returncode, 1)
        self.assertEqual(self.calls(), ["reuse lint"])

    def test_pip_audit_reads_the_locked_requirements_without_installing(self):
        p = self.run_step("Known vulnerabilities in the locked dependencies")
        self.assertEqual(p.returncode, 0)
        req = f"{self.r.tmp}/requirements.txt"
        self.assertEqual(self.calls(), [
            f"uv export --locked --no-emit-workspace --format requirements-txt --output-file {req}",
            f"pip-audit --disable-pip --requirement {req}",
        ])

    def test_a_lock_that_does_not_export_fails_before_the_audit(self):
        p = self.run_step("Known vulnerabilities in the locked dependencies", FAKE_UV_EXIT="2")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(len(self.calls()), 1)

    def test_gitleaks_scans_the_tree_redacted(self):
        self.assertEqual(self.run_step("Secrets in the tree", FAKE_GITLEAKS_EXIT="1").returncode, 1)
        self.assertEqual(self.calls(), ["gitleaks dir --redact --no-banner ."])

    def test_zizmor_reads_github_with_the_standard_policy_offline(self):
        p = self.run_step("Workflow security", ZIZMOR_CONFIG="/a/zizmor.yml")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.calls(), ["zizmor --offline --config /a/zizmor.yml .github"])


def needs(**results: str) -> str:
    return json.dumps({job: {"result": r, "outputs": {}} for job, r in results.items()})


def git(cwd: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env={**os.environ, **env}).stdout


class Changes(unittest.TestCase):
    """`changes` on a real checkout: the PR's merge commit, as actions/checkout leaves it."""

    STEP = "Did the change touch code"
    DOCS = "docs/**\n**/*.md"

    def setUp(self):
        self.r = Runner(self)
        self.repo = self.r.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.write({"README.md": "r", "src/app.py": "a", "docs/guide.md": "g", ".airia/notes.md": "n"})
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()

    def write(self, files: dict[str, str | None]) -> None:
        for name, text in files.items():
            f = self.repo / name
            if text is None:
                f.unlink()
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text)

    def pr(self, files: dict[str, str | None] | None = None, rename: tuple[str, str] | None = None) -> None:
        """A PR branch with these changes, merged into main --no-ff: HEAD is the merge commit."""
        git(self.repo, "checkout", "-qb", "pr")
        if rename:
            (self.repo / rename[1]).parent.mkdir(parents=True, exist_ok=True)
            git(self.repo, "mv", *rename)
        self.write(files or {})
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "pr")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge", "pr")

    def code(self, docs: str | None = None, event: str = "pull_request"):
        p = self.r.run(WORKFLOW, self.STEP, cwd=self.repo, EVENT=event, DOCS_PATHS=self.DOCS if docs is None else docs)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return self.r.outputs()["code"], p.stdout

    def test_a_docs_only_pr_skips_the_code_gates(self):
        self.pr({"docs/guide.md": "g2", "docs/new/page.md": "p", "README.md": "r2"})
        code, out = self.code()
        self.assertEqual(code, "false")
        self.assertIn("docs/new/page.md", out)
        self.assertIn("code changed: false", self.r.summary.read_text())

    def test_a_pr_that_touches_code_runs_everything_and_names_it(self):
        self.pr({"docs/guide.md": "g2", "src/app.py": "a2"})
        code, out = self.code()
        self.assertEqual(code, "true")
        self.assertIn("  src/app.py", out)
        self.assertNotIn("  docs/guide.md", out)

    def test_no_docs_paths_is_todays_behaviour(self):
        # Opt-in: without the input, a docs-only PR still runs every gate, and nothing is diffed.
        self.pr({"docs/guide.md": "g2"})
        for docs in ("", " \n "):
            with self.subTest(docs=docs):
                code, out = self.code(docs=docs)
                self.assertEqual(code, "true")
                self.assertIn("Every gate runs: no docs-paths", out)

    def test_a_push_runs_everything(self):
        self.pr({"docs/guide.md": "g2"})
        code, out = self.code(event="push")
        self.assertEqual(code, "true")
        self.assertIn("not a pull request (push)", out)

    def test_moving_code_into_docs_is_a_code_change(self):
        # Without --no-renames, git would list only docs/app.md, and the deleted source would pass as docs.
        self.pr(rename=("src/app.py", "docs/app.md"))
        code, out = self.code()
        self.assertEqual(code, "true")
        self.assertIn("  src/app.py", out)

    def test_moving_docs_within_docs_is_docs(self):
        self.pr(rename=("docs/guide.md", "docs/old/guide.md"))
        self.assertEqual(self.code()[0], "false")

    def test_an_empty_diff_runs_everything(self):
        self.pr()
        code, out = self.code()
        self.assertEqual(code, "true")
        self.assertIn("changes no file", out)

    def test_some_paths_are_never_docs_whatever_the_patterns_say(self):
        # `.airia/` is read by suites; workflows, tests and tool configuration change what the gates do.
        for path in (".airia/notes.md", ".github/workflows/ci.yml", "tests/data/case.md", "src/tests/case.json", "pyproject.toml",
                     "uv.lock", ".python-version", "docs/conf.py", "pytest.ini", "ruff.toml", ".coveragerc",
                     "requirements-dev.txt", "docker/Dockerfile"):
            with self.subTest(path=path):
                git(self.repo, "reset", "-q", "--hard", self.base)
                git(self.repo, "branch", "-qD", "pr") if "pr" in git(self.repo, "branch") else None
                self.pr({path: "changed"})
                code, out = self.code(docs="**")
                self.assertEqual(code, "true")
                self.assertIn(f"  {path}", out)

    def test_a_checkout_that_is_not_a_merge_commit_runs_everything(self):
        self.write({"docs/guide.md": "g2"})
        git(self.repo, "commit", "-qam", "straight onto main")
        code, out = self.code()
        self.assertEqual(code, "true")
        self.assertIn("::notice title=Every gate runs::", out)


class Gate(unittest.TestCase):
    STEP = "Every gate succeeded"

    def verdict(self, own: str, extra: str = "{}", code: str = "true"):
        r = Runner(self)
        p = r.run(WORKFLOW, self.STEP, OWN=own, EXTRA=extra, CODE=code)
        return p, r

    def test_docs_only_passes_with_lint_and_test_skipped(self):
        p, r = self.verdict(needs(changes="success", lint="skipped", test="skipped", security="success"), code="false")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Docs only: lint and test were not run.", r.summary.read_text())
        self.assertIn("- test: skipped", r.summary.read_text())

    def test_a_skipped_test_on_a_code_change_fails(self):
        p, _ = self.verdict(needs(changes="success", lint="success", test="skipped", security="success"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Gate not passed::test: skipped", p.stdout)

    def test_a_failing_test_fails_even_a_docs_only_gate(self):
        # Only a skip is excused, and only for lint and test.
        p, _ = self.verdict(needs(changes="success", lint="skipped", test="failure", security="success"), code="false")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Gate not passed::test: failure", p.stdout)

    def test_docs_only_never_excuses_security_changes_or_an_extra_job(self):
        own = needs(changes="success", lint="skipped", test="skipped", security="skipped")
        p, _ = self.verdict(own, needs(e2e="skipped"), code="false")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("::error title=Gate not passed::"), 2)
        self.assertIn("security: skipped", p.stdout)
        self.assertIn("extra: e2e: skipped", p.stdout)

    def test_a_failed_changes_job_fails_the_gate(self):
        # No answer from `changes` (CODE empty) is not "docs only".
        p, _ = self.verdict(needs(changes="failure", lint="skipped", test="skipped", security="success"), code="")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("::error title=Gate not passed::"), 3)

    def test_all_green_is_green_and_listed(self):
        p, r = self.verdict(needs(lint="success", test="success", security="success"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("- security: success", r.summary.read_text())

    def test_one_failure_fails_it_by_name(self):
        p, _ = self.verdict(needs(lint="success", test="failure", security="success"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Gate not passed::test: failure", p.stdout)

    def test_a_skipped_or_cancelled_gate_is_not_a_pass(self):
        p, _ = self.verdict(needs(lint="skipped", test="cancelled", security="success"))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout.count("::error title=Gate not passed::"), 2)

    def test_the_callers_extra_jobs_count_and_cannot_shadow_ours(self):
        own = needs(lint="failure", test="success", security="success")
        p, r = self.verdict(own, needs(lint="success", e2e="failure"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=Gate not passed::lint: failure", p.stdout)
        self.assertIn("::error title=Gate not passed::extra: e2e: failure", p.stdout)
        self.assertIn("- extra: lint: success", r.summary.read_text())

    def test_extra_jobs_that_are_not_needs_json_fail(self):
        p, _ = self.verdict(needs(lint="success"), "e2e")
        self.assertEqual(p.returncode, 1)
        self.assertIn("::error title=extra-jobs::", p.stdout)


if __name__ == "__main__":
    unittest.main()
