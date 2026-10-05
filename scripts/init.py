# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Wire a repository to airia-ci: the review, `@claude`, its token, a protected main.

From the root of the repository's checkout, logged in with `gh`:

    python3 <(gh api repos/AIRIA-Systems-GmbH/airia-ci/contents/scripts/init.py \\
        -H 'Accept: application/vnd.github.raw')

It writes the files that are missing and never overwrites one:

- `.github/workflows/claude.yml`, the `@claude` caller;
- the review job at the end of the repository's one pull-request workflow, or,
  when there is none, `.github/workflows/merge-gate.yml` with a placeholder
  gate that fails until it is replaced. With several pull-request workflows it
  prints the job to add, because which one is the gate is not for a script to
  guess;
- `.github/claude-review.md`, the repository's own review rules, to fill in.

Then it sets the `CLAUDE_CODE_OAUTH_TOKEN` secret (from the environment
variable of that name, or by running `claude setup-token`; one token serves
every repository of the same Anthropic account), and offers to protect the
default branch with a ruleset: changes only through a pull request whose gate
jobs passed and whose commits are signed and verified, no force-push, no
deletion. Recommended; a private repository of
an organization on the Free plan cannot have one, and it says so.

It commits nothing: review the files, commit them on a branch, open a PR.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TAG = "ci-v3"
HARNESS = "AIRIA-Systems-GmbH/airia-ci/.github/workflows"
RULESET = "Restrict Direct Merge on Main"
THOR = '["self-hosted","linux","x64","thor"]'
APP_URL = "https://github.com/apps/claude"

RESPOND = """\
name: Claude

# `@claude`, answered by the shared AIRIA responder in AIRIA-Systems-GmbH/airia-ci:
# who may start it, what it may run and its prompt all live there.

on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]
  pull_request_review:
    types: [submitted]
  issues:
    types: [opened, assigned]

permissions: {{}}

jobs:
  claude:
    permissions:
      contents: read
      pull-requests: write
      issues: write
      id-token: write
      actions: read
    uses: {harness}/reusable-claude-respond.yml@{tag}
    with:
      runner: '{runner}'
      gate-workflow: {gate}
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{{{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}}}
"""

REVIEW_JOB = """\

  # The automatic review: last, after every gate, and also when one failed.
  claude-review:
    needs: [{needs}]
    if: always() && github.event_name == 'pull_request' && github.event.pull_request.draft == false
    permissions:
      contents: read
      pull-requests: write
      issues: write
      actions: read
    uses: {harness}/reusable-claude-review.yml@{tag}
    with:
      pr-number: ${{{{ github.event.pull_request.number }}}}
      runner: '{runner}'
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{{{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}}}
"""

GATE = """\
name: Merge gate

on:
  pull_request:
    branches: [{branch}]
    types: [opened, synchronize, reopened, ready_for_review]

permissions: {{}}

jobs:
  gates:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - name: Replace with this repository's gates
        run: |
          echo "::error::merge-gate.yml still has the placeholder gate: put the tests, lint and type checks here"
          exit 1
"""

RULES_MD = """\
# Review rules for {name}

Read by the shared automatic review before anything else. Only what is
particular to this repository goes here; the procedure is in airia-ci.

- The gates are the jobs of `.github/workflows/{gate}`. Their results and logs
  are in `ci-results/jobs.md` and `ci-results/logs/`: read them rather than
  re-running a gate.
- How to run each gate locally, and what it must not do: (fill in)
- Traps a reviewer of this repository should know: (fill in)
"""


def gh(*args: str, stdin: str | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], input=stdin, cwd=cwd, capture_output=True, text=True, check=False)


def ask(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"{question} [Y/n] y")
        return True
    return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")


def pr_workflows(root: Path) -> list[Path]:
    wf = root / ".github/workflows"
    found = sorted(list(wf.glob("*.yml")) + list(wf.glob("*.yaml"))) if wf.is_dir() else []
    # Block style (`  pull_request:`) and flow style (`on: pull_request`, `on: [push, pull_request]`).
    return [p for p in found if re.search(r"^  pull_request:|^on:.*\bpull_request\b", p.read_text(), re.M)]


def jobs(text: str) -> list[tuple[str, str]]:
    """(id, check name) of each job: the name GitHub reports is `name:` if set, else the id."""
    found, current = [], None
    in_jobs = False
    for line in text.splitlines():
        if re.match(r"^\S", line):
            in_jobs = line.startswith("jobs:")
            continue
        if not in_jobs:
            continue
        if m := re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line):
            current = m.group(1)
            found.append([current, current])
        elif current and (m := re.match(r"^    name:\s*(.+?)\s*$", line)):
            found[-1][1] = m.group(1).strip("'\"")
    return [tuple(j) for j in found]


def jobs_is_last(text: str) -> bool:
    tops = [line for line in text.splitlines() if re.match(r"^[A-Za-z]", line)]
    return bool(tops) and tops[-1].startswith("jobs:")


def write_new(path: Path, text: str) -> bool:
    if path.exists():
        print(f"kept      {path} (exists)")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    print(f"wrote     {path}")
    return True


def wire_review(root: Path, branch: str, runner: str) -> tuple[str, list[str]]:
    """Add the review job; return the gate workflow's file name and its gate check names."""
    gates = [p for p in pr_workflows(root) if p.name not in ("claude.yml",)]
    if not gates:
        path = root / ".github/workflows/merge-gate.yml"
        write_new(path, GATE.format(branch=branch))
        print("warning: merge-gate.yml's `gates` job fails until its placeholder is replaced, and it is a "
              "required check: put the real gates in the same pull request, or it cannot merge")
        gates = [path]
    if len(gates) > 1:
        print(f"several pull-request workflows ({', '.join(p.name for p in gates)}): add this job, "
              "with needs: set to the gate jobs, to the one that gates merges:")
        print(REVIEW_JOB.format(needs="<gate jobs>", harness=HARNESS, tag=TAG, runner=runner))
        return "", []
    path = gates[0]
    text = path.read_text()
    found = [j for j in jobs(text) if j[0] != "claude-review"]
    checks = [name for _, name in found]
    if "reusable-claude-review.yml@" in text:
        print(f"kept      {path} (already calls the review)")
    elif not jobs_is_last(text):
        print(f"{path}: `jobs:` is not its last top-level key; add this job by hand:")
        print(REVIEW_JOB.format(needs=", ".join(j for j, _ in found), harness=HARNESS, tag=TAG, runner=runner))
    else:
        path.write_text(text.rstrip("\n") + "\n" + REVIEW_JOB.format(
            needs=", ".join(j for j, _ in found), harness=HARNESS, tag=TAG, runner=runner))
        print(f"appended  the review job to {path}")
    if "ready_for_review" not in text:
        print(f"warning: {path.name} does not run on ready_for_review, so a PR opened as a draft is never reviewed")
    return path.name, checks


def set_token(repo: str, assume_yes: bool) -> None:
    p = gh("secret", "list", "--repo", repo, "--json", "name", "--jq", ".[].name")
    if p.returncode == 0 and "CLAUDE_CODE_OAUTH_TOKEN" in p.stdout.split():
        print("kept      the CLAUDE_CODE_OAUTH_TOKEN secret (exists)")
        return
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if not token:
        if not ask("No CLAUDE_CODE_OAUTH_TOKEN secret. Run `claude setup-token` now?", assume_yes):
            print("skipped   the token: the review and @claude fail until the secret is set")
            return
        subprocess.run(["claude", "setup-token"], check=False)
        token = getpass.getpass("Paste the token it printed (not echoed): ").strip()
    if not token:
        print("skipped   the token: none given")
        return
    # On stdin, so the token is in no process list or shell history.
    s = gh("secret", "set", "CLAUDE_CODE_OAUTH_TOKEN", "--repo", repo, stdin=token)
    if s.returncode:
        raise RuntimeError(f"could not set the secret: {s.stderr.strip()}")
    print("set       the CLAUDE_CODE_OAUTH_TOKEN secret")


def protect(repo: str, branch: str, checks: list[str], assume_yes: bool) -> None:
    p = gh("api", f"repos/{repo}/rulesets", "--jq", f'.[] | select(.name == "{RULESET}") | .id')
    if p.returncode == 0 and p.stdout.strip():
        print(f"kept      the {RULESET!r} ruleset (exists)")
        return
    if not ask(f"Protect {branch} (recommended): changes only by pull request, signed and verified "
               f"commits only, required checks {checks or 'none yet'}, no force-push or deletion?", assume_yes):
        print(f"skipped   protection: anyone with write access can push to {branch} directly")
        return
    body = {
        "name": RULESET, "target": "branch", "enforcement": "active", "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"}, {"type": "non_fast_forward"},
            # Every commit a PR brings must verify, even for a squash merge.
            {"type": "required_signatures"},
            {"type": "pull_request", "parameters": {
                "allowed_merge_methods": ["merge", "squash", "rebase"], "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False, "require_last_push_approval": False,
                "required_approving_review_count": 0, "required_review_thread_resolution": True}},
        ],
    }
    if checks:
        body["rules"].append({"type": "required_status_checks", "parameters": {
            "do_not_enforce_on_create": False, "strict_required_status_checks_policy": True,
            "required_status_checks": [{"context": c} for c in checks]}})
    c = gh("api", "-X", "POST", f"repos/{repo}/rulesets", "--input", "-", stdin=json.dumps(body))
    if c.returncode == 0:
        print(f"protected {branch} with the {RULESET!r} ruleset")
    elif "Upgrade to GitHub" in c.stdout + c.stderr:
        print("not protected: GitHub refuses rulesets on this repository under its owner's plan. A private "
              "repository needs GitHub Pro (personal) or Team (organization), or the repository made public.")
    else:
        raise RuntimeError(f"could not create the ruleset: {(c.stdout + c.stderr).strip()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".", help="the repository's checkout (default: here)")
    ap.add_argument("--runner", default=THOR,
                    help=f"runs-on labels as a JSON array (default: the AIRIA Thor pool, {THOR}); "
                         "'[\"ubuntu-latest\"]' for GitHub's runners")
    ap.add_argument("--yes", action="store_true", help="answer yes to every question")
    args = ap.parse_args(argv)
    root = Path(args.dir).resolve()
    try:
        json.loads(args.runner)
        v = gh("repo", "view", "--json", "nameWithOwner,defaultBranchRef", cwd=root)
        if v.returncode:
            raise RuntimeError(f"not a GitHub repository checkout, or gh is not logged in: {v.stderr.strip()}")
        info = json.loads(v.stdout)
        repo, branch = info["nameWithOwner"], info["defaultBranchRef"]["name"]
        print(f"wiring {repo} ({branch}) to airia-ci @{TAG}")
        gate, checks = wire_review(root, branch, args.runner)
        write_new(root / ".github/workflows/claude.yml",
                  RESPOND.format(harness=HARNESS, tag=TAG, runner=args.runner, gate=gate or "''"))
        write_new(root / ".github/claude-review.md",
                  RULES_MD.format(name=repo.split("/")[1], gate=gate or "<the gate workflow>"))
        set_token(repo, args.yes)
        protect(repo, branch, checks, args.yes)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"\nNext: install the Claude GitHub App on {repo} if it is not ({APP_URL}), "
          "then commit the files on a branch and open a pull request.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
