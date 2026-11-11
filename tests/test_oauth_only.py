# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Claude review and respond authenticate only with the OAuth tokens (airia-ci#143).

The Console API credit is not for bot reviews, and the Claude CLI prefers an ANTHROPIC_API_KEY
it can see over the OAuth token. So the key must reach no step at all. It stays declared,
because the callers run `@main` and many still pass it, and passing an undeclared secret to a
reusable workflow is an error.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import unittest

import yaml
from steps import ROOT, Runner, step

WORKFLOWS = ROOT / ".github/workflows"
ACTIONS = ROOT / ".github/actions"
REVIEW = WORKFLOWS / "reusable-claude-review.yml"
RESPOND = WORKFLOWS / "reusable-claude-respond.yml"
GATE = WORKFLOWS / "reusable-python-gate.yml"
DECLARING = (REVIEW, RESPOND, GATE)
KEY = "ANTHROPIC_API_KEY"
CLAUDE_ACTION = "anthropics/claude-code-action@"


def strings(node, path=()):
    """Every key and scalar of a parsed YAML document, with where it sits. Comments are not in it."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield path, str(k)
            yield from strings(v, (*path, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from strings(v, (*path, i))
    elif node is not None:
        yield path, str(node)


def mentions_of_the_key(path):
    """Where a shipped YAML file names the key, except for its declaration under on.workflow_call.secrets."""
    declared = (True, "workflow_call", "secrets")
    return [p for p, s in strings(yaml.safe_load(path.read_text())) if KEY in s and p != declared]


def claude_steps(path):
    doc = yaml.safe_load(path.read_text())
    return [s for job in doc["jobs"].values() for s in job.get("steps", [])
            if str(s.get("uses", "")).startswith(CLAUDE_ACTION)]


class TheKeyReachesNoStep(unittest.TestCase):
    def test_no_shipped_workflow_or_action_names_the_key_outside_its_declaration(self):
        # Any env, input, run block or forwarded secret counts: the CLI would prefer the key over the OAuth token.
        files = sorted([*WORKFLOWS.glob("*.yml"), *ACTIONS.glob("*/action.yml")])
        self.assertIn(REVIEW, files)
        for path in files:
            with self.subTest(file=path.name):
                self.assertEqual(mentions_of_the_key(path), [])

    def test_the_check_catches_an_input_or_an_env(self):
        # The pin above must fail on what #141 shipped: an action input and a step env.
        doc = {True: {"workflow_call": {"secrets": {KEY: {"required": False}}}},
               "jobs": {"j": {"steps": [{"with": {"anthropic_api_key": "${{ secrets.ANTHROPIC_API_KEY }}"}},
                                        {"env": {"HAS_ANTHROPIC_API_KEY": "${{ secrets.ANTHROPIC_API_KEY != '' }}"}}]}}}
        found = [p for p, s in strings(doc) if KEY in s and p != (True, "workflow_call", "secrets")]
        self.assertEqual(len(found), 3, found)

    def test_every_claude_step_gets_the_oauth_token_and_no_key(self):
        expected = {
            (REVIEW, "Run Claude Code Review"): "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}",
            (REVIEW, "Run Claude Code Review on the fallback token"): "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK }}",
            (RESPOND, "Run Claude Code"): "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}",
        }
        found = {(path, s["name"]): s for path in (REVIEW, RESPOND) for s in claude_steps(path)}
        self.assertEqual(set(found), set(expected), "every claude-code-action step is pinned here")
        for where, token in expected.items():
            with self.subTest(step=where[1]):
                s = found[where]
                self.assertNotIn("anthropic_api_key", s["with"])
                self.assertEqual(s["with"]["claude_code_oauth_token"], token)
                self.assertNotIn(KEY, str(s.get("env", {})))

    def test_the_fallback_is_keyed_on_its_own_secret_as_in_140(self):
        primary = step(REVIEW, "Run Claude Code Review")
        self.assertEqual(primary["continue-on-error"], "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK != '' }}")
        decide = step(REVIEW, "Decide whether to retry on the fallback token")
        self.assertEqual(decide["with"]["fallback-token"], "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK }}")


class ACallerPassingTheKeyIsAccepted(unittest.TestCase):
    def test_the_three_reusable_workflows_still_declare_the_key_optional_beside_the_token(self):
        for path in DECLARING:
            with self.subTest(file=path.name):
                secrets = yaml.safe_load(path.read_text())[True]["workflow_call"]["secrets"]
                self.assertIs(secrets[KEY]["required"], False)
                self.assertIs(secrets["CLAUDE_CODE_OAUTH_TOKEN"]["required"], False)

    def test_the_gate_forwards_the_tokens_and_not_the_key(self):
        forwarded = yaml.safe_load(GATE.read_text())["jobs"]["claude-review"]["secrets"]
        self.assertEqual(set(forwarded), {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK"})


class TheCredentialCheckRequiresTheToken(unittest.TestCase):
    STEP = "Require the Claude credential"

    def test_the_check_reads_only_the_token(self):
        for path in (REVIEW, RESPOND):
            with self.subTest(file=path.name):
                self.assertEqual(step(path, self.STEP)["env"],
                                 {"HAS_CLAUDE_TOKEN": "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN != '' }}"})

    def test_no_token_fails_naming_only_the_oauth_fix_even_when_the_key_is_set(self):
        for path in (REVIEW, RESPOND):
            with self.subTest(file=path.name):
                p = Runner(self).run(path, self.STEP, HAS_CLAUDE_TOKEN="false", HAS_ANTHROPIC_API_KEY="true")
                self.assertEqual(p.returncode, 1)
                self.assertIn("::error title=No Claude credential::", p.stdout)
                self.assertIn("gh secret set CLAUDE_CODE_OAUTH_TOKEN -R o/r", p.stdout)
                self.assertNotIn(KEY, p.stdout)

    def test_the_token_passes(self):
        for path in (REVIEW, RESPOND):
            with self.subTest(file=path.name):
                self.assertEqual(Runner(self).run(path, self.STEP, HAS_CLAUDE_TOKEN="true").returncode, 0)


class OurCallersAndInitPassOnlyTheTokens(unittest.TestCase):
    def test_this_repositorys_callers_do_not_pass_the_key(self):
        for name in ("self-test.yml", "claude.yml"):
            with self.subTest(file=name):
                text = (WORKFLOWS / name).read_text()
                self.assertNotIn(KEY, text)
                self.assertIn("CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}", text)

    def test_init_sets_and_passes_only_the_tokens(self):
        spec = importlib.util.spec_from_file_location("init", ROOT / "scripts/init.py")
        init = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(init)
        self.assertNotIn(KEY, (ROOT / "scripts/init.py").read_text())
        review = init.REVIEW_JOB.format(needs="gates", harness=init.HARNESS, tag=init.TAG, runner=init.THOR)
        gate = init.PYTHON_GATE.format(branch="main", harness=init.HARNESS, tag=init.TAG, runner=init.THOR)
        respond = init.RESPOND.format(harness=init.HARNESS, tag=init.TAG, runner=init.THOR, gate="ci.yml")
        for name, text in (("review", review), ("gate", gate)):
            with self.subTest(caller=name):
                self.assertIn("CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}", text)
                self.assertIn("CLAUDE_CODE_OAUTH_TOKEN_FALLBACK: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK }}", text)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}", respond)

    def test_the_classifiers_name_no_key(self):
        for path in (ACTIONS / "claude-limit/claude_limit.py", ACTIONS / "review-post/post_review.py"):
            with self.subTest(file=path.name):
                self.assertNotIn(KEY, path.read_text())
