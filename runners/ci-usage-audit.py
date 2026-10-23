#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Aggregate GitHub Actions job minutes per repo/workflow/job over the last N days.
Uses /actions/runs/{id}/jobs (started_at..completed_at), which always has data,
unlike /timing whose `billable` block is empty on the new Actions architecture.
Read-only."""
import collections
import concurrent.futures as cf
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 14

def gh(path):
    p = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except Exception:
        return None

since = (datetime.datetime.now(datetime.UTC)
         - datetime.timedelta(days=DAYS)).strftime("%Y-%m-%d")

# repos = every git remote under ~/Development (covers org-owned repos too)
repos = set()
base = os.path.expanduser("~/Development")
for d in os.listdir(base):
    cfg = os.path.join(base, d, ".git", "config")
    if os.path.exists(cfg):
        for m in re.finditer(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?\s", Path(cfg).read_text()):
            repos.add(m.group(1))
repos = sorted(repos)
print(f"# window: last {DAYS} days (since {since}); repos from git remotes: {len(repos)}", flush=True)

def dur(job):
    try:
        s = datetime.datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
        e = datetime.datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
        return max((e - s).total_seconds() / 60.0, 0)
    except Exception:
        return 0.0

MULT = {"ubuntu": 1, "windows": 2, "macos": 10}
def osmult(labels):
    text = " ".join(labels or []).lower()
    for k, v in MULT.items():
        if k in text:
            return k, v
    return "other", 1

by = collections.defaultdict(lambda: {"min": 0.0, "wmin": 0.0, "jobs": 0, "os": collections.Counter()})
runmeta = collections.defaultdict(lambda: {"runs": 0, "concl": collections.Counter(), "ev": collections.Counter()})

def fetch_repo(full):
    out = []
    page = 1
    while page <= 5:
        data = gh(f"/repos/{full}/actions/runs?per_page=100&page={page}&created=%3E%3D{since}")
        if not data or not data.get("workflow_runs"):
            break
        for run in data["workflow_runs"]:
            jobs = gh(f"/repos/{full}/actions/runs/{run['id']}/jobs?per_page=100")
            out.append((run, (jobs or {}).get("jobs", [])))
        if len(data["workflow_runs"]) < 100:
            break
        page += 1
    return full, out

with cf.ThreadPoolExecutor(max_workers=6) as ex:
    for full, runs in ex.map(fetch_repo, repos):
        if runs:
            print(f"#   {full}: {len(runs)} runs", flush=True)
        for run, jobs in runs:
            wf = run.get("name") or run.get("path", "?")
            runmeta[(full, wf)]["runs"] += 1
            runmeta[(full, wf)]["concl"][run.get("conclusion")] += 1
            runmeta[(full, wf)]["ev"][run.get("event")] += 1
            for j in jobs:
                osname, mult = osmult(j.get("labels"))
                k = (full, wf, j.get("name", "?"))
                d = dur(j)
                by[k]["min"] += d
                by[k]["wmin"] += d * mult
                by[k]["jobs"] += 1
                by[k]["os"][osname] += 1

rows = sorted(by.items(), key=lambda kv: -kv[1]["wmin"])
tw = sum(v["wmin"] for v in by.values())
tr = sum(v["min"] for v in by.values())

print(f"\n{'WEIGHT':>8} {'RAW':>8} {'JOBS':>5} {'AVG':>6}  REPO :: WORKFLOW :: JOB   [os]")
print("-" * 108)
for (full, wf, job), v in rows[:35]:
    if v["wmin"] < 1:
        continue
    print(f"{v['wmin']:8.0f} {v['min']:8.0f} {v['jobs']:5d} {v['min']/max(v['jobs'],1):6.1f}  {full} :: {wf} :: {job}  {dict(v['os'])}")
print("-" * 108)
print(f"{tw:8.0f} {tr:8.0f}  TOTAL over {DAYS}d  =>  {tw/DAYS:.0f} weighted min/day, {tr/DAYS:.0f} raw min/day")
print(f"# est ${tw/DAYS*0.006:.2f}/day @ $0.006/weighted-min\n")

print("RUN-LEVEL (top workflows by run count):")
for k, v in sorted(runmeta.items(), key=lambda kv: -kv[1]["runs"])[:12]:
    print(f"  {v['runs']:4d} runs  {k[0]} :: {k[1]}  concl={dict(v['concl'])} ev={dict(v['ev'])}")
