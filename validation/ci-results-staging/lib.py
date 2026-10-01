# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Extract the SHIPPED shell from this repository's action / workflow YAML.

The harness never runs a copy of the code under test: it pulls the `run:`
block out of the file that consumers actually execute.

    python3 lib.py <yaml> <step-name> [<job>]   -> prints that step's `run:`
"""

import sys

import yaml


def step_run(path: str, step_name: str, job: str | None = None) -> str:
    doc = yaml.safe_load(open(path))
    if "runs" in doc:  # composite action
        steps = doc["runs"]["steps"]
    else:  # workflow
        jobs = doc["jobs"]
        steps = jobs[job or next(iter(jobs))]["steps"]
    matches = [s for s in steps if s.get("name") == step_name]
    if len(matches) != 1:
        sys.exit(f"{path}: expected one step named {step_name!r}, found {len(matches)}")
    return matches[0]["run"]


if __name__ == "__main__":
    print(step_run(*sys.argv[1:]))
