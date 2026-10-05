# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The shared @claude responder's shipped steps, against a fake `gh`.

The write-access check runs before anything from the repository does, on the
production host, so its refusals are pinned first.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from steps import ROOT, Runner, bare, heredoc, step

WORKFLOW = ROOT / ".github/workflows/reusable-claude-respond.yml"


class RequireWriteAccess(unittest.TestCase):
    STEP = "Require write access"

    def check(self, reply):
        r = Runner(self, [[r"collaborators/alice/permission", reply]])
        return r.run(WORKFLOW, self.STEP, ACTOR="alice")

    def test_write_and_admin_may_start_it(self):
        for level in ("write", "admin"):
            p = self.check({"json": {"permission": level}})
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertIn(f"alice has {level} access", p.stdout)

    def test_read_and_none_may_not(self):
        # `COLLABORATOR` in the job's `if:` includes read and triage; this is the write check.
        for level in ("read", "none"):
            p = self.check({"json": {"permission": level}})
            self.assertEqual(p.returncode, 1)
            self.assertIn(f"alice has {level} access to o/r; @claude runs only for write or admin", p.stdout)

    def test_an_unreadable_permission_is_a_refusal_not_a_crash(self):
        p = self.check({"stderr": "HTTP 404\n", "exit": 1})
        self.assertEqual(p.returncode, 1)
        self.assertIn("alice has no readable access", p.stdout)


class RequireTheCredential(unittest.TestCase):
    def test_a_missing_token_fails_with_the_fix(self):
        p = Runner(self).run(WORKFLOW, "Require the Claude credential", HAS_CLAUDE_TOKEN="false")
        self.assertEqual(p.returncode, 1)
        self.assertIn("gh secret set CLAUDE_CODE_OAUTH_TOKEN -R o/r", p.stdout)

    def test_a_present_token_passes(self):
        self.assertEqual(Runner(self).run(WORKFLOW, "Require the Claude credential", HAS_CLAUDE_TOKEN="true").returncode, 0)


class JobEnvironment(unittest.TestCase):
    """`job-env` maps the service addresses onto the repository's own variable names."""

    STEP = "Export the service addresses and the job environment"
    PG = {"PG_PORT": "55432", "PG_USER": "u", "PG_PASSWORD": "p", "PG_DB": "d"}

    def export(self, **env):
        r = Runner(self)
        p = subprocess.run([sys.executable, str(heredoc(WORKFLOW, self.STEP))],
                           env=bare(GITHUB_ENV=str(r.github_env), **env), capture_output=True, text=True)
        written = r.github_env.read_text().splitlines() if r.github_env.exists() else []
        return p, dict(line.split("=", 1) for line in written)

    def test_no_services_and_no_job_env_exports_nothing(self):
        p, env = self.export()
        self.assertEqual((p.returncode, env), (0, {}))
        self.assertIn("job environment: (none)", p.stdout)

    def test_the_service_addresses(self):
        p, env = self.export(**self.PG, QD_PORT="56333")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(env, {"CI_POSTGRES_DSN": "postgres://u:p@localhost:55432/d",
                               "CI_QDRANT_URL": "http://localhost:56333"})

    def test_both_reference_spellings_are_replaced_and_nothing_else_expands(self):
        job_env = "# a comment\n\nAPP_DSN=$CI_POSTGRES_DSN\n  APP_URL=${CI_QDRANT_URL}/v1\nLITERAL=$HOME\n"
        p, env = self.export(**self.PG, QD_PORT="1", JOB_ENV=job_env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(env["APP_DSN"], "postgres://u:p@localhost:55432/d")
        self.assertEqual(env["APP_URL"], "http://localhost:1/v1")
        self.assertEqual(env["LITERAL"], "$HOME")

    def test_a_reference_to_a_service_that_is_not_running_fails(self):
        p, _ = self.export(JOB_ENV="APP_DSN=$CI_POSTGRES_DSN")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("uses CI_POSTGRES_DSN, but that service is not configured", p.stderr)

    def test_a_line_that_is_not_key_value_fails(self):
        for line in ("JUST_A_NAME", "=value"):
            p, _ = self.export(JOB_ENV=line)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("job-env line is not KEY=VALUE", p.stderr)

    def test_the_step_runs_its_python(self):
        r = Runner(self)
        p = r.run(WORKFLOW, self.STEP, JOB_ENV="A=1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(r.github_env.read_text(), "A=1\n")


class FindTheGateRun(unittest.TestCase):
    STEP = "Find the gate run for this PR"

    def find(self, runs):
        r = Runner(self, [
            [r"^pr view 9 --repo o/r --json headRefOid", {"json": {"headRefOid": "abc123"}}],
            [r"^run list --repo o/r --workflow ci.yml --commit abc123 --limit 1", {"json": runs}],
        ])
        p = r.run(WORKFLOW, self.STEP, PR="9", WORKFLOW="ci.yml")
        self.assertEqual(p.returncode, 0, p.stderr)
        return r.outputs()

    def test_the_latest_run_on_the_head_commit(self):
        self.assertEqual(self.find([{"databaseId": 777}]), {"run-id": "777"})

    def test_no_run_yet_is_an_empty_id(self):
        # ci-results then writes "No CI run was found" rather than failing.
        self.assertEqual(self.find([]), {"run-id": ""})


class StagingForClaude(unittest.TestCase):
    def test_ci_results_is_excluded_and_the_status_staged(self):
        r = Runner(self)
        repo = r.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (r.tmp / "claude-toolchain-status.md").write_text("ok\n")
        self.assertEqual(r.run(WORKFLOW, "Keep ci-results/ out of git", cwd=repo).returncode, 0)
        p = r.run(WORKFLOW, "Stage the toolchain status for Claude", cwd=repo, GITHUB_WORKSPACE=str(repo))
        self.assertEqual(p.returncode, 0, p.stderr)
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo,
                                capture_output=True, text=True, check=True).stdout
        self.assertEqual(status, "", "@claude must never commit ci-results/")
        self.assertEqual((repo / "ci-results/claude-toolchain-status.md").read_text(), "ok\n")

    def test_the_harness_note_carries_no_inputs(self):
        # The respond inputs include postgres-password and qdrant-api-key:
        # nothing Claude reads, and could quote in a comment, may hold them.
        r = Runner(self)
        repo = r.tmp / "repo"
        repo.mkdir()
        p = r.run(WORKFLOW, "Stage the toolchain status for Claude", cwd=repo, GITHUB_WORKSPACE=str(repo),
                  HARNESS_REF="o/airia-ci/.github/workflows/reusable-claude-respond.yml@refs/tags/ci-v3",
                  HARNESS_SHA="ab7b0e6", CALLER_INPUTS='{"postgres-password": "s3cret"}')
        self.assertEqual(p.returncode, 0, p.stderr)
        harness = (repo / "ci-results/harness.md").read_text()
        self.assertIn("- Commit: ab7b0e6", harness)
        self.assertNotIn("s3cret", harness)


class NewerMajor(unittest.TestCase):
    """The responder warns too: a repository may run @claude more often than reviews."""

    STEP = "Look for a newer airia-ci major"
    MAJOR = step(WORKFLOW, STEP)["env"]["HARNESS_MAJOR"]

    def look(self, reply):
        r = Runner(self, [[r"airia-ci/git/matching-refs/tags/ci-v", reply]])
        p = r.run(WORKFLOW, self.STEP, HARNESS_MAJOR=self.MAJOR)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def test_a_newer_major_warns(self):
        refs = [{"ref": f"refs/tags/ci-v{n}"} for n in (self.MAJOR, int(self.MAJOR) + 1)]
        self.assertIn(f"::warning title=Newer airia-ci major::airia-ci ci-v{int(self.MAJOR) + 1} exists",
                      self.look({"json": refs}).stdout)

    def test_the_newest_major_says_nothing(self):
        self.assertNotIn("::warning", self.look({"json": [{"ref": f"refs/tags/ci-v{self.MAJOR}"}]}).stdout)

    def test_an_unreadable_tag_list_never_fails_the_job(self):
        self.assertIn("::notice title=airia-ci version not checked::", self.look({"exit": 1}).stdout)


class SystemPrompt(unittest.TestCase):
    STEP = "Compose the system prompt"

    def compose(self, has_gate="true", extra=""):
        r = Runner(self)
        p = subprocess.run([sys.executable, str(heredoc(WORKFLOW, self.STEP))],
                           env=bare(GITHUB_OUTPUT=str(r.output), HAS_GATE=has_gate, EXTRA_PROMPT=extra),
                           capture_output=True, text=True)
        return p, r.outputs().get("args")

    def test_one_single_quoted_append_system_prompt_argument(self):
        p, args = self.compose()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(args.startswith("--append-system-prompt '") and args.endswith("'"))
        self.assertEqual(args.count("'"), 2, "the prompt travels inside one single-quoted argument")

    def test_with_a_gate_it_reads_ci_results_by_a_relative_path(self):
        _, args = self.compose("true")
        self.assertIn("`cat ci-results/jobs.md` next", args)
        self.assertIn("do not use `gh pr checks`", args)
        self.assertNotIn("RUNNER_TEMP", args)

    def test_without_a_gate_it_runs_the_gates_itself(self):
        _, args = self.compose("false")
        self.assertIn("has no gate workflow", args)
        self.assertNotIn("ci-results/jobs.md", args)

    def test_extra_prompt_is_appended_on_one_line(self):
        _, args = self.compose(extra="Use  make check.\nNever touch vendor/.")
        self.assertTrue(args.endswith(" Use make check. Never touch vendor/.'"))

    def test_a_single_quote_in_extra_prompt_is_refused(self):
        p, args = self.compose(extra="don't")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("may not contain a single quote", p.stderr)
        self.assertIsNone(args, "nothing may be written for claude_args")

    def test_the_step_runs_its_python(self):
        r = Runner(self)
        p = r.run(WORKFLOW, self.STEP, HAS_GATE="false", EXTRA_PROMPT="")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("args", r.outputs())


if __name__ == "__main__":
    unittest.main()
