# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""The python-toolchain action's logic: the AIRIA standard, checked and installed.

    toolchain.py [--pins FILE] standard   validate the caller's inputs; print uv/python/pyyaml to GITHUB_OUTPUT
    toolchain.py [--pins FILE] check      the project's uv.lock and workflows against the pins
    toolchain.py [--pins FILE] install    put the gate tools on GITHUB_PATH, at the pinned or locked version
    toolchain.py [--pins FILE] services   export the service addresses and the caller's job-env to GITHUB_ENV
    toolchain.py [--pins FILE] ruff [--write-baseline]
                                          the ruff floor and the suppression ratchet (below)

Run from the root of the caller's checkout. Inputs come from the environment:
PYTHON_VERSION (empty: `.python-version`), TYPE_CHECKER, COVERAGE_FLOOR,
TOOLS (space-separated), GITHUB_REPOSITORY, SERVICES (`toJSON(job.services)`),
JOB_ENV. Every problem found is reported,
as one `::error::` line each, before it exits 1: a red gate should name all of
its causes at once, not one per push.

Which version of a tool runs. One the project locks in uv.lock runs at the
locked version, so CI runs what `uv run <tool>` runs on a laptop; the locked
version may be newer than the pin, never older. One it does not lock runs at
the pin. The tools that must import the project (pytest, pytest-cov, coverage;
mypy under mypy-strict; import-linter when it is configured) must be locked:
they run inside the project's own environment, which uv.lock alone defines.

The ruff floor. The `select` of templates/python/pyproject.toml is the
floor: every rule it enables (as `ruff --show-settings` resolves it, at the
ruff that runs) must be enabled by each of the project's ruff configurations.
A narrower `select`, or a global `ignore`/`extend-ignore` of a floor rule, is
refused; extending is free. A `per-file-ignores` entry may switch floor rules
off only for test paths (tests/, test/, conftest.py, test_*.py, *_test.py);
anywhere else the finding takes a `# noqa`, which the ratchet counts.

The ratchet. `.airia/ruff-baseline.json` records the `# noqa` suppressions per
rule (findings with `--ignore-noqa` minus findings without). A count above its
baseline is refused; with no baseline file every suppression is only warned
about, until `--write-baseline` records it. A count below its baseline passes,
with a notice that the baseline can fall. `.airia/` is never docs, so a PR
that touches the baseline always runs the lint job. The count is held to the
file, not to the base branch's copy: a baseline raised in a PR (by hand or by
`--write-baseline`) passes the gate and is caught in the diff and the review.

`standard` and `ruff` use only the standard library: `standard` runs before uv
is installed, and `ruff` runs with ruff on PATH and nothing else.
`check` needs PyYAML (the action runs it through `uv run --with pyyaml==<pin>`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PINS = HERE.parents[2] / "pins.toml"
# Must be in uv.lock: they run inside the project's environment.
IN_PROJECT = ("pytest", "pytest-cov", "coverage")
# May run outside it: the locked version if the project locks one, else the pin.
STANDALONE = {"ruff": "ruff", "pyright": "pyright", "reuse": "reuse", "pip-audit": "pip-audit", "zizmor": "zizmor"}
# What `uv tool install` gets for a tool: pyright brings its own Node.js.
SPEC = {"pyright": "pyright[nodejs]"}
TOOLS_DIR = "airia-tools"
TEMPLATE = HERE.parents[2] / "templates/python/pyproject.toml"
BASELINE = ".airia/ruff-baseline.json"
RULE = re.compile(r"\(([A-Z]+[0-9]+)\)")
# Where a per-file ignore may switch a floor rule off (proposed, pending Andrea, 2026-10-15).
TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)conftest\.py$|(^|/)test_[^/]*\.py$|_test\.py$")
ONBOARD = ("ci-local.sh --onboard (ruff check --fix, ruff format, ruff check --add-noqa, then the baseline; "
           "airia-ci HOWTO.md, Quick start step 2)")


class Refused(Exception):
    """The standard is not met; each argument is one reason."""


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def version(text: str) -> tuple[int, ...]:
    """The numeric release of a version string (`1.2.3rc1` is (1, 2, 3)); enough for these tools."""
    return tuple(int(p) for p in re.findall(r"\d+", re.match(r"[\d.]*", text).group()))


def load_pins(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def output(name: str, value: str, file: str = "GITHUB_OUTPUT") -> None:
    with open(os.environ[file], "a") as fh:
        fh.write(f"{name}={value}\n")


def python_version(pins: dict, requested: str, root: Path) -> str:
    """The interpreter to provision: the caller's input, else `.python-version`; one the standard allows."""
    if not requested:
        f = root / ".python-version"
        if not f.is_file():
            raise Refused(".python-version is missing: commit it (the standard provisions Python from it), "
                          "or pass python-versions")
        requested = f.read_text().split("\n", 1)[0].strip()
    allowed = pins["python"]["allowed"]
    if ".".join(requested.split(".")[:2]) not in allowed:
        raise Refused(f"Python {requested!r} is not one the standard allows ({', '.join(allowed)})")
    return requested


def standard(pins: dict, env: dict, root: Path) -> dict[str, str]:
    """Validate the inputs the standard constrains; return what the next steps need."""
    errors = []
    checker = env.get("TYPE_CHECKER", "")
    if checker not in pins["standard"]["type-checkers"]:
        errors.append(f"type-checker {checker!r} is not one the standard accepts "
                      f"({', '.join(pins['standard']['type-checkers'])}); ty is not accepted before its 1.0")
    elif checker == "mypy-strict" and env.get("GITHUB_REPOSITORY") not in pins["standard"]["mypy-strict-repositories"]:
        errors.append(f"type-checker mypy-strict is accepted only where it was already the gate "
                      f"({', '.join(pins['standard']['mypy-strict-repositories'])}); use pyright")
    floor = pins["standard"]["coverage-floor"]
    try:
        if float(env.get("COVERAGE_FLOOR", "")) < floor:
            errors.append(f"coverage-floor {env['COVERAGE_FLOOR']} is below the standard's {floor}: "
                          "a project may raise it, never lower it")
    except ValueError:
        errors.append(f"coverage-floor {env.get('COVERAGE_FLOOR', '')!r} is not a number")
    try:
        python = python_version(pins, env.get("PYTHON_VERSION", ""), root)
    except Refused as exc:
        errors += exc.args
        python = ""
    if errors:
        raise Refused(*errors)
    return {"uv": pins["tools"]["uv"], "python": python, "pyyaml": pins["harness"]["pyyaml"]}


def locked(root: Path) -> dict[str, str]:
    """{normalised name: version} of every package in uv.lock."""
    f = root / "uv.lock"
    if not f.is_file():
        raise Refused("uv.lock is missing: the standard installs the project with `uv sync --locked`; "
                      "run `uv lock` and commit it")
    return {norm(p["name"]): p.get("version", "") for p in tomllib.loads(f.read_text()).get("package", [])}


def lock_problems(pins: dict, lock: dict[str, str], checker: str, pyproject: dict) -> list[str]:
    required = list(IN_PROJECT)
    if checker == "mypy-strict":
        required.append("mypy")
    if "importlinter" in pyproject.get("tool", {}):
        required.append("import-linter")
    errors = []
    for tool in required:
        if tool not in lock:
            errors.append(f"{tool} is not in uv.lock; it runs inside the project's environment: "
                          f"`uv add --dev {tool}=={pins['tools'][tool]}`")
    for tool, pin in pins["tools"].items():
        if tool in lock and version(lock[tool]) < version(pin):
            errors.append(f"uv.lock resolves {tool} {lock[tool]}, below the standard's {pin}: "
                          f"`uv add --dev {tool}=={pin}`")
    return errors


# `uvx X`, `uv tool run X`, `pipx run X`: X runs at whatever was released last.
RUNNERS = re.compile(r"(?:^|[\s;&|(])(uvx|uv\s+tool\s+run|pipx\s+run)\s+(.*)")
VALUED = {"--from", "--with", "--python", "-p", "--index", "--index-url", "--extra-index-url", "--default-index",
          "--constraint", "-c", "--with-editable", "--with-requirements", "--spec", "--pip-args"}


def pinned(spec: str, at: bool) -> bool:
    spec = spec.strip("'\"")
    if "==" in spec:
        return True
    return at and bool(re.search(r"@(?!latest\b)[\w.]+$", spec))


def unpinned(line: str) -> list[str]:
    """The tools a shell line runs unpinned: through uvx and friends, or `uv run --with`."""
    try:
        words = shlex.split(line, comments=True)
    except ValueError:
        words = line.split()
    found = []
    for i, w in enumerate(words):
        if w == "--with" and i + 1 < len(words):
            found += [s for s in words[i + 1].split(",") if not pinned(s, at=False)]
        elif w.startswith("--with="):
            found += [s for s in w[7:].split(",") if not pinned(s, at=False)]
    m = RUNNERS.search(" ".join(words))
    if m:
        rest, tool, source = m.group(2).split(), None, None
        j = 0
        while j < len(rest):
            if rest[j] in VALUED:
                if rest[j] == "--from" and j + 1 < len(rest):
                    source = rest[j + 1]
                j += 2
            elif rest[j].startswith("-"):
                j += 1
            else:
                tool = rest[j]
                break
        spec = source or tool
        if spec and not pinned(spec, at=source is None):
            found.append(spec)
    return found


def run_blocks(root: Path):
    """(file, run: text) of every step in the repository's workflows and composite actions."""
    import yaml  # noqa: PLC0415 (only `check` needs it, and the action provides it)

    files = sorted((root / ".github/workflows").glob("*.y*ml")) + sorted((root / ".github/actions").glob("*/action.y*ml"))
    for f in files:
        doc = yaml.safe_load(f.read_text()) or {}
        jobs = list(doc.get("jobs", {}).values()) if isinstance(doc.get("jobs"), dict) else []
        steps = [s for j in jobs for s in (j.get("steps") or [])] + list((doc.get("runs") or {}).get("steps") or [])
        for s in steps:
            if isinstance(s, dict) and isinstance(s.get("run"), str):
                yield f.relative_to(root).as_posix(), s["run"]


def workflow_problems(root: Path) -> list[str]:
    errors = []
    for where, run in run_blocks(root):
        for line in run.replace("\\\n", " ").splitlines():
            for spec in unpinned(line):
                errors.append(f"{where}: `{line.strip()}` runs {spec} unpinned; pin it ({spec}==<version>) "
                              "or lock it in a dev group and call it with `uv run`")
    return errors


def check(pins: dict, env: dict, root: Path) -> None:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text()) if (root / "pyproject.toml").is_file() else {}
    try:
        errors = lock_problems(pins, locked(root), env.get("TYPE_CHECKER", ""), pyproject)
    except Refused as exc:
        errors = list(exc.args)
    errors += workflow_problems(root)
    if errors:
        raise Refused(*errors)


def gitleaks(pins: dict, bin_dir: Path) -> None:
    """Download the pinned gitleaks for this platform and refuse it unless its digest matches the pin."""
    v = pins["tools"]["gitleaks"]
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower(), "")
    key = f"{platform.system().lower()}_{arch}"
    digest = pins["gitleaks"]["sha256"].get(key)
    if not digest:
        raise Refused(f"no pinned gitleaks digest for {key}: add it to pins.toml from the release's checksums.txt")
    url = os.environ.get("AIRIA_GITLEAKS_URL") or (
        f"https://github.com/gitleaks/gitleaks/releases/download/v{v}/gitleaks_{v}_{key}.tar.gz")
    archive = bin_dir.parent / "gitleaks.tar.gz"
    with urllib.request.urlopen(url) as r:  # noqa: S310 (a fixed https URL, or a test's file URL)
        archive.write_bytes(r.read())
    got = hashlib.sha256(archive.read_bytes()).hexdigest()
    if got != digest:
        raise Refused(f"gitleaks {v} for {key} has sha256 {got}, not the pinned {digest}: refused")
    with tarfile.open(archive) as tar:
        (bin_dir / "gitleaks").write_bytes(tar.extractfile("gitleaks").read())
    (bin_dir / "gitleaks").chmod(0o755)


def install(pins: dict, env: dict, root: Path) -> list[str]:
    """Put each requested tool on GITHUB_PATH; return one line per tool saying which version and why."""
    base = Path(env["RUNNER_TEMP"]) / TOOLS_DIR
    bin_dir = base / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    venv_bin = root / ".venv/bin"
    lock = locked(root) if (root / "uv.lock").is_file() else {}
    done = []
    for tool in env.get("TOOLS", "").split():
        if tool == "gitleaks":
            gitleaks(pins, bin_dir)
            done.append(f"gitleaks {pins['tools']['gitleaks']} (pinned, digest checked)")
            continue
        if tool not in STANDALONE:
            raise Refused(f"unknown tool {tool!r}: the action installs {', '.join([*STANDALONE, 'gitleaks'])}")
        if (venv_bin / STANDALONE[tool]).exists() and tool in lock:
            done.append(f"{tool} {lock[tool]} (locked, from the project environment)")
            continue
        v, why = (lock[tool], "locked") if tool in lock else (pins["tools"][tool], "pinned")
        p = subprocess.run(["uv", "tool", "install", "--quiet", f"{SPEC.get(tool, tool)}=={v}"],
                           env={**env, "UV_TOOL_DIR": str(base / "envs"), "UV_TOOL_BIN_DIR": str(bin_dir)},
                           capture_output=True, text=True, check=False)
        if p.returncode:
            raise Refused(f"`uv tool install {tool}=={v}` failed: {p.stderr.strip()}")
        done.append(f"{tool} {v} ({why})")
    # The later steps of the job run this file's `ruff` command by this name.
    output("AIRIA_TOOLCHAIN", str(HERE / "toolchain.py"), "GITHUB_ENV")
    if "zizmor" in env.get("TOOLS", "").split():
        # The standard's zizmor policy: hash pins, except this harness's own moving tag.
        output("ZIZMOR_CONFIG", str(HERE / "zizmor.yml"), "GITHUB_ENV")
    # GITHUB_PATH prepends: the project's environment ends up first, so a locked tool wins.
    with open(env["GITHUB_PATH"], "a") as fh:
        fh.write(f"{bin_dir}\n")
        if venv_bin.is_dir():
            fh.write(f"{venv_bin}\n")
    return done


def ruff_settings(config: Path) -> tuple[set[str], list[tuple[str, bool, set[str]]]]:
    """(enabled rules, per-file ignores as (pattern, negated, rules)) of one config, as ruff resolves it."""
    p = subprocess.run(["ruff", "check", "--show-settings", str(config)], capture_output=True, text=True, check=False)
    if p.returncode:
        raise Refused(f"`ruff check --show-settings {config}` failed: {(p.stdout + p.stderr).strip()}")
    enabled: set[str] = set()
    ignores: list[tuple[str, bool, set[str]]] = []
    block, data = None, False
    for line in p.stdout.splitlines():
        if line.startswith("linter.rules.enabled = ["):
            block = "enabled"
        elif line.startswith("linter.per_file_ignores = {"):
            block = "ignores"
        elif block == "enabled":
            if line == "]":
                block = None
            enabled.update(RULE.findall(line))
        elif block == "ignores":
            if line == "}":
                block = None
            elif m := re.match(r'\s*basename_matcher = "(.*)"$', line):
                ignores.append((m.group(1), False, set()))
            elif line.strip() == "negated = true" and ignores:
                ignores[-1] = (ignores[-1][0], True, ignores[-1][2])
            elif line.strip() == "data = [":
                data = True
            elif data and line == "]":
                data = False
            elif data and ignores:
                ignores[-1][2].update(RULE.findall(line))
    if not enabled:
        raise Refused(f"`ruff check --show-settings {config}` listed no enabled rules: this ruff's output is not "
                      "one the standard can read")
    return enabled, ignores


def ruff_floor() -> set[str]:
    """Every rule the template's ruff configuration enables, resolved by the ruff that runs."""
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "pyproject.toml"
        config.write_text(TEMPLATE.read_text())
        return ruff_settings(config)[0]


def ruff_configs(root: Path) -> list[Path]:
    """The project's ruff configurations: the root pyproject.toml always, then every tracked one outside tests."""
    names = ("pyproject.toml", "ruff.toml", ".ruff.toml")
    p = subprocess.run(["git", "ls-files", "--", *(f"*{n}" for n in names)], cwd=root,
                       capture_output=True, text=True, check=False)
    tracked = p.stdout.split() if p.returncode == 0 else [n for n in names if (root / n).is_file()]
    found = [root / "pyproject.toml"] if (root / "pyproject.toml").is_file() else []
    for rel in tracked:
        path = root / rel
        if path in found or Path(rel).name not in names or TEST_PATH.search(rel) or not path.is_file():
            continue
        if path.name != "pyproject.toml" or re.search(r"^\[tool\.ruff", path.read_text(), re.M):
            found.append(path)
    return found


def codes(rules: set[str], most: int = 12) -> str:
    listed = sorted(rules)
    return ", ".join(listed[:most]) + (f" and {len(listed) - most} more" if len(listed) > most else "")


def floor_problems(root: Path, floor: set[str]) -> list[str]:
    errors: list[str] = []
    for config in ruff_configs(root):
        where = config.relative_to(root).as_posix()
        enabled, ignores = ruff_settings(config)
        if missing := floor - enabled:
            errors.append(f"{where}: ruff does not enable the floor's {codes(missing)}: a narrower `select` or an "
                          "`ignore` of a floor rule; the floor is the `select` of airia-ci's "
                          "templates/python/pyproject.toml, which a project may extend, never narrow")
        for pattern, negated, rules in ignores:
            off = rules & floor
            shown = "!" * negated + pattern
            message = (f"{where}: per-file-ignores {shown!r} switches off the floor's {codes(off)}: only test "
                       "paths may; elsewhere mark the line `# noqa: <rule>`, which the suppression baseline counts")
            if off and (negated or not TEST_PATH.search(pattern)) and message not in errors:
                errors.append(message)
    return errors


def suppressions(root: Path, paths: list[str]) -> dict[str, int]:
    """`# noqa` suppressions per rule: what ruff finds when told to ignore them, minus what it finds otherwise."""
    def findings(*extra: str) -> dict[str, int]:
        p = subprocess.run(["ruff", "check", "--exit-zero", "--output-format", "json", *extra, *paths],
                           cwd=root, capture_output=True, text=True, check=False)
        if p.returncode:
            raise Refused(f"`ruff check` failed: {(p.stdout + p.stderr).strip()}")
        counts: dict[str, int] = {}
        for finding in json.loads(p.stdout):
            if finding.get("code"):
                counts[finding["code"]] = counts.get(finding["code"], 0) + 1
        return counts
    every, shown = findings("--ignore-noqa"), findings()
    return {code: n - shown.get(code, 0) for code, n in sorted(every.items()) if n > shown.get(code, 0)}


def ruff(pins: dict, env: dict, root: Path, write: bool) -> list[str]:
    """Hold the project to the ruff floor and its suppression baseline; return the notices to print."""
    errors = floor_problems(root, ruff_floor())
    if errors and pins["standard"].get("ruff-floor") == "warning":
        notices = [f"::warning title=AIRIA standard (ruff floor)::{e}" for e in errors]
        errors = []
    else:
        notices = []
    now = suppressions(root, shlex.split(env.get("PATHS", "")) or ["."])
    total = sum(now.values())
    baseline = root / BASELINE
    if write:
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps(now, indent=2) + "\n")
        notices.append(f"wrote {BASELINE}: {total} suppressions ({codes(set(now)) or 'none'}); commit it")
    elif not baseline.is_file():
        if total:
            notices.append(f"::warning title=Ruff suppressions::{total} `# noqa` suppressions ({codes(set(now))}) "
                           f"and no {BASELINE} to hold them: record it once with {ONBOARD}")
    else:
        recorded = json.loads(baseline.read_text())
        for code, n in now.items():
            if n > recorded.get(code, 0):
                errors.append(f"{code} is suppressed {n} times, above the {recorded.get(code, 0)} in {BASELINE}: "
                              "fix the finding instead of adding `# noqa`; the baseline may only fall")
        if any(now.get(code, 0) < n for code, n in recorded.items()):
            notices.append(f"::notice title=Ruff suppressions::fewer suppressions than {BASELINE} records: lower it "
                           "with `ci-local.sh --write-baseline` and commit it")
    if errors:
        raise Refused(*errors)
    return notices


def services(env: dict) -> dict[str, str]:
    """CI_POSTGRES_DSN / CI_QDRANT_URL for the slots that started, then the caller's KEY=VALUE lines.

    The same contract as the respond workflow's `job-env`: a value may name
    either address, and naming one whose service did not start is refused.
    """
    try:
        started = json.loads(env.get("SERVICES") or "{}") or {}
    except ValueError as exc:
        raise Refused(f"the services context is not JSON: {exc}") from None
    out = {}
    if pg := ((started.get("postgres") or {}).get("ports") or {}).get("5432"):
        out["CI_POSTGRES_DSN"] = f"postgres://postgres:postgres@localhost:{pg}/postgres"
    if qd := ((started.get("qdrant") or {}).get("ports") or {}).get("6333"):
        out["CI_QDRANT_URL"] = f"http://localhost:{qd}"
    for line in env.get("JOB_ENV", "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise Refused(f"job-env line is not KEY=VALUE: {line!r}")
        for name in ("CI_POSTGRES_DSN", "CI_QDRANT_URL"):
            for ref in ("${" + name + "}", "$" + name):
                if ref in value:
                    if name not in out:
                        raise Refused(f"job-env {key.strip()} uses {name}, but that service is not configured")
                    value = value.replace(ref, out[name])
        out[key.strip()] = value
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pins", type=Path, default=PINS)
    ap.add_argument("command", choices=["standard", "check", "install", "services", "ruff"])
    ap.add_argument("--write-baseline", action="store_true",
                    help=f"ruff: record the current suppressions as {BASELINE}")
    args = ap.parse_args(argv)
    pins, env, root = load_pins(args.pins), dict(os.environ), Path.cwd()
    try:
        if args.command == "standard":
            for name, value in standard(pins, env, root).items():
                output(name, value)
        elif args.command == "check":
            check(pins, env, root)
            print("uv.lock and the workflows meet the pins")
        elif args.command == "install":
            for line in install(pins, env, root):
                print(line)
        elif args.command == "ruff":
            for line in ruff(pins, env, root, args.write_baseline):
                print(line)
            print("ruff: checked the floor and the suppressions")
        else:
            exported = services(env)
            for name, value in exported.items():
                output(name, value, "GITHUB_ENV")
            print("job environment:", ", ".join(exported) or "(none)")
    except Refused as exc:
        for reason in exc.args:
            print(f"::error title=AIRIA standard::{reason}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
