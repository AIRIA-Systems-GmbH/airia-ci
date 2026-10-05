# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The harness every step test runs through: the SHIPPED code, never a copy.

Each `run:` block is read out of the action / workflow YAML that consumers
execute, written to build/coverage/src/, and run there with the shell GitHub
uses for it:

    composite action, `shell: bash`   bash --noprofile --norc -eo pipefail
    workflow step, no `shell:`        bash -e      (no pipefail, no -u)

A Python heredoc (`python3 - ... <<'PY'`) inside a block is also written out
as its own .py file, because coverage.py cannot measure a script read from
stdin; tests run that file with the arguments the block passes.

With KCOV_OUT set (tests/coverage.sh sets it), every block runs under kcov.
`materialise()` writes EVERY shipped block up front, so a block no test runs
counts as uncovered instead of being left out of the figure.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "build/coverage/src"
SHIPPED = sorted(ROOT.glob(".github/actions/*/action.yml")) + sorted(ROOT.glob(".github/workflows/reusable-*.yml"))
HEREDOC = re.compile(r"<<'PY'[^\n]*\n(.*?)\n\s*PY(?:\n|$)", re.DOTALL)


def _steps(path: Path) -> list[dict]:
    doc = yaml.safe_load(path.read_text())
    if "runs" in doc:
        return doc["runs"]["steps"]
    return [s for job in doc["jobs"].values() for s in job.get("steps", [])]


def _slug(path: Path, step_name: str) -> str:
    owner = path.parent.name if path.name == "action.yml" else path.stem
    return owner + "--" + re.sub(r"[^a-z0-9]+", "-", step_name.lower()).strip("-")


def step(path: Path, step_name: str) -> dict:
    (found,) = [s for s in _steps(path) if s.get("name") == step_name]
    return found


def _header(path: Path, s: dict) -> str:
    if s.get("shell") == "bash":
        return "#!/bin/bash\nset -eo pipefail\n"
    if s.get("shell") is None and path.name != "action.yml":
        return "#!/bin/bash\nset -e\n"
    raise ValueError(f"{path}: step {s.get('name')!r} uses shell {s.get('shell')!r}, which the harness does not model")


def script(path: Path, step_name: str) -> Path:
    """The step's `run:` block as an executable file, with GitHub's shell flags."""
    s = step(path, step_name)
    if "${{" in s["run"]:
        raise ValueError(f"{path}: {step_name!r} has an expression inside run:, so a sliced copy is not the shipped code")
    SRC.mkdir(parents=True, exist_ok=True)
    out = SRC / (_slug(path, step_name) + ".sh")
    out.write_text(_header(path, s) + s["run"])
    out.chmod(0o755)
    return out


def heredoc(path: Path, step_name: str) -> Path:
    """The Python heredoc inside a step, as a file `python3 <file> ARGS` runs like `python3 - ARGS`."""
    (body,) = HEREDOC.findall(step(path, step_name)["run"])
    SRC.mkdir(parents=True, exist_ok=True)
    out = SRC / (_slug(path, step_name).replace("-", "_") + ".py")
    out.write_text(textwrap.dedent(body) + "\n")
    return out


def materialise() -> None:
    for path in SHIPPED:
        for s in _steps(path):
            if "run" in s:
                script(path, s["name"])
                if HEREDOC.search(s["run"]):
                    heredoc(path, s["name"])


def bare(**env) -> dict:
    """A deliberately bare child environment that coverage.py still follows (it passes itself on in COVERAGE_*)."""
    return {**{k: v for k, v in os.environ.items() if k.startswith("COVERAGE_")}, **env}


def execute(file: Path, args: list[str], env: dict, cwd: Path) -> subprocess.CompletedProcess:
    """Run a shell script (under kcov when measuring); never raises on a non-zero exit, the test asserts it."""
    cmd = ["bash", "--noprofile", "--norc", str(file), *args]
    if os.environ.get("KCOV_OUT"):
        cmd = ["kcov", f"--include-path={SRC},{ROOT / '.github'}", os.environ["KCOV_OUT"], str(file), *args]
    return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True)


def run_block(path: Path, step_name: str, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    """Run the shipped `run:` block of one step; a composite action's sees its own directory, as on GitHub."""
    if path.name == "action.yml":
        env = {"GITHUB_ACTION_PATH": str(path.parent), **env}
    return execute(script(path, step_name), [], env, cwd)


FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    # A fake `gh`: the first rule whose pattern matches the joined argv answers.
    import json, os, re, subprocess, sys
    args = sys.argv[1:]
    joined = " ".join(args)
    files = {}
    for i, a in enumerate(args):
        if a in ("--body-file", "--notes-file"):
            files[a] = open(args[i + 1]).read()
        elif "=@" in a:
            files[a] = open(a.split("=@", 1)[1]).read()
    if args[:2] == ["secret", "set"] or "--input" in args and args[args.index("--input") + 1] == "-":
        files["stdin"] = sys.stdin.read()
    with open(os.environ["FAKE_GH_LOG"], "a") as log:
        log.write(json.dumps({"args": args, "files": files}) + "\\n")
    for pattern, reply in json.load(open(os.environ["FAKE_GH_RULES"])):
        if re.search(pattern, joined):
            break
    else:
        print(f"fake gh: no rule for {joined}", file=sys.stderr); sys.exit(97)
    sys.stderr.write(reply.get("stderr", ""))
    if "json" in reply:
        jq = args[args.index("--jq") + 1] if "--jq" in args else "."
        p = subprocess.run(["jq", "-r", jq], input=json.dumps(reply["json"]), text=True, capture_output=True)
        sys.stdout.write(p.stdout); sys.stderr.write(p.stderr)
        sys.exit(p.returncode or reply.get("exit", 0))
    sys.stdout.write(reply.get("text", ""))
    sys.exit(reply.get("exit", 0))
    """
)


class Runner:
    """A scratch directory with a fake `gh` (and any other fake tools) first on PATH."""

    def __init__(self, test, rules: list | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.tool("gh", FAKE_GH)
        self.rules(rules or [])
        self.log = self.tmp / "gh-calls.jsonl"
        self.output = self.tmp / "github-output"
        self.summary = self.tmp / "step-summary"
        self.github_env = self.tmp / "github-env"

    def rules(self, rules: list) -> None:
        (self.tmp / "gh-rules.json").write_text(json.dumps(rules))

    def tool(self, name: str, source: str) -> None:
        f = self.bin / name
        f.write_text(source)
        f.chmod(0o755)

    def run(self, path: Path, step_name: str, cwd: Path | None = None, **env) -> subprocess.CompletedProcess:
        return run_block(path, step_name, self.env(**env), cwd or self.tmp)

    def script(self, file: Path, *args: str, cwd: Path | None = None, **env) -> subprocess.CompletedProcess:
        return execute(file, list(args), self.env(**env), cwd or self.tmp)

    def env(self, **env) -> dict:
        return {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_GH_RULES": str(self.tmp / "gh-rules.json"),
            "FAKE_GH_LOG": str(self.log),
            "GH_TOKEN": "x",
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_ENV": str(self.github_env),
            "RUNNER_TEMP": str(self.tmp),
            **env,
        }

    def outputs(self) -> dict[str, str]:
        lines = self.output.read_text().splitlines() if self.output.exists() else []
        return dict(line.split("=", 1) for line in lines)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
