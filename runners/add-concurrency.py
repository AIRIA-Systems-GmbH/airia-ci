#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Insert a top-level `concurrency:` block immediately before the top-level `jobs:`
key of each given workflow file. Idempotent: skips files that already have one.
Validates YAML before and after, and refuses to write if the parsed `jobs` keys change."""
import io
import sys

import yaml

BLOCK = """
# Supersede in-flight runs for the same branch. Without this, every push to a PR
# branch started a fresh full run while the previous one kept burning billable
# minutes to completion. main is exempt: post-merge runs are the release gate.
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}

"""

for path in sys.argv[1:]:
    with open(path) as f:
        src = f.read()
    before = yaml.safe_load(io.StringIO(src))
    if before.get("concurrency") is not None:
        print(f"SKIP (already has concurrency): {path}")
        continue
    lines = src.splitlines(keepends=True)
    idx = next((i for i, line in enumerate(lines) if line.startswith("jobs:")), None)
    if idx is None:
        print(f"SKIP (no top-level jobs:): {path}")
        continue
    out = "".join(lines[:idx]) + BLOCK.lstrip("\n") + "".join(lines[idx:])
    after = yaml.safe_load(io.StringIO(out))
    if list(after["jobs"].keys()) != list(before["jobs"].keys()):
        print(f"ABORT (jobs changed): {path}")
        continue
    if after.get("concurrency", {}).get("group") is None:
        print(f"ABORT (concurrency not parsed): {path}")
        continue
    with open(path, "w") as f:
        f.write(out)
    print(f"OK: {path}  jobs={list(after['jobs'].keys())}")
