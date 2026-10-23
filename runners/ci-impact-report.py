#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""CI cost/health impact report: before vs after the self-hosted cutover.

Splits every workflow job at the cutover timestamp, classifies it hosted vs
self-hosted, and reports the four things that decide whether the Team plan is
survivable or Enterprise is required:

  1. BILLABLE minutes/day (hosted only -- self-hosted is not billed)
  2. Self-hosted RELIABILITY (failure rate vs the hosted baseline)
  3. Self-hosted QUEUE time (the parallelism you gave up to stop paying)
  4. What is STILL on hosted runners, ranked

Read-only. Usage: ci-impact-report.py [days_after] [days_before]
"""
import collections
import concurrent.futures as cf
import datetime
import json
import os
import subprocess
import sys

CUTOVER = datetime.datetime(2026, 8, 26, 21, 47, 38, tzinfo=datetime.UTC)


def config(name):
    """NAME from the environment, else from runners.env (see runners/README.md)."""
    if os.environ.get(name):
        return os.environ[name]
    conf = os.environ.get("AIRIA_CI_RUNNERS_ENV", os.path.expanduser("~/.config/airia-ci/runners.env"))
    if os.path.exists(conf):
        with open(conf) as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key == name:
                    return value.strip("'\"")
    sys.exit(f"set {name} in {conf}")


# The repositories to report on, space-separated owner/repo. Only private repos bill.
REPOS = config("CI_REPOS").split()
RATE = 0.006          # USD per weighted Linux minute, post-Jan-2026
MULT = {"ubuntu": 1, "windows": 2, "macos": 10}

DAYS_AFTER = int(sys.argv[1]) if len(sys.argv) > 1 else 7
DAYS_BEFORE = int(sys.argv[2]) if len(sys.argv) > 2 else 14


def gh(path):
    p = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except Exception:
        return None


def ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def mins(a, b):
    if not a or not b:
        return 0.0
    return max((b - a).total_seconds() / 60.0, 0.0)


since = (CUTOVER - datetime.timedelta(days=DAYS_BEFORE)).strftime("%Y-%m-%d")


def fetch(full):
    """Two phases: page the run list serially (cheap), then fan the per-run jobs
    calls out in parallel. Serialising the jobs calls made this take ~10 min for
    AIRIA-framework alone, which is not viable for a scheduled weekly run."""
    runs, page = [], 1
    while page <= 12:
        d = gh(f"/repos/{full}/actions/runs?per_page=100&page={page}&created=%3E%3D{since}")
        if not d or not d.get("workflow_runs"):
            break
        runs += d["workflow_runs"]
        if len(d["workflow_runs"]) < 100:
            break
        page += 1

    def one(r):
        j = gh(f"/repos/{full}/actions/runs/{r['id']}/jobs?per_page=100")
        return (r, (j or {}).get("jobs", []))

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        out = list(ex.map(one, runs))
    return full, out


def selfhosted(job):
    lbls = " ".join(job.get("labels") or []).lower()
    if "self-hosted" in lbls or "thor" in lbls:
        return True
    rn = (job.get("runner_name") or "").lower()
    return rn.startswith("thor-")


def osmult(job):
    text = " ".join(job.get("labels") or []).lower()
    for k, v in MULT.items():
        if k in text:
            return k, v
    return ("ubuntu", 1) if selfhosted(job) else ("other", 1)


B = {"billable": 0.0, "sh_min": 0.0, "n_host": 0, "n_sh": 0,
     "fail_host": 0, "fail_sh": 0, "queue": [], "by_wf": collections.Counter()}
A = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in B.items()}
A["queue"], A["by_wf"] = [], collections.Counter()
B["queue"], B["by_wf"] = [], collections.Counter()

with cf.ThreadPoolExecutor(max_workers=8) as ex:
    for full, runs in ex.map(fetch, REPOS):
        print(f"#  {full}: {len(runs)} runs", flush=True)
        for run, jobs in runs:
            created = ts(run.get("created_at"))
            for j in jobs:
                st, en = ts(j.get("started_at")), ts(j.get("completed_at"))
                if not st or not en:
                    continue
                bucket = A if st >= CUTOVER else B
                d = mins(st, en)
                sh = selfhosted(j)
                _, mu = osmult(j)
                failed = j.get("conclusion") == "failure"
                if sh:
                    bucket["n_sh"] += 1
                    bucket["sh_min"] += d
                    bucket["fail_sh"] += failed
                    q = mins(created, st)
                    if q < 240:
                        bucket["queue"].append(q)
                else:
                    bucket["n_host"] += 1
                    bucket["billable"] += d * mu
                    bucket["fail_host"] += failed
                    bucket["by_wf"][f"{full.split('/')[-1]} :: {run.get('name')} :: {j.get('name')}"] += d * mu


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(int(len(s) * p / 100), len(s) - 1)]


print(f"\n{'='*78}\nCUTOVER: {CUTOVER:%Y-%m-%d %H:%M} UTC")
print(f"BEFORE window: {DAYS_BEFORE}d   AFTER window: {DAYS_AFTER}d\n{'='*78}")

for name, b, days in (("BEFORE", B, DAYS_BEFORE), ("AFTER", A, DAYS_AFTER)):
    bpd = b["billable"] / days
    print(f"\n--- {name} ---")
    print(f"  BILLABLE (hosted) : {b['billable']:8.0f} min  =>  {bpd:6.1f} min/day  "
          f"=>  ${bpd*RATE:5.2f}/day  ${bpd*RATE*30:7.2f}/month")
    print(f"  self-hosted (free): {b['sh_min']:8.0f} min  =>  {b['sh_min']/days:6.1f} min/day")
    print(f"  jobs hosted/self  : {b['n_host']} / {b['n_sh']}")
    hf = 100*b['fail_host']/b['n_host'] if b['n_host'] else 0
    sf = 100*b['fail_sh']/b['n_sh'] if b['n_sh'] else 0
    print(f"  failure rate      : hosted {hf:.1f}%   self-hosted {sf:.1f}%")
    if b["queue"]:
        print(f"  self-hosted queue : median {pct(b['queue'],50):.1f} min, "
              f"p90 {pct(b['queue'],90):.1f} min, max {max(b['queue']):.1f} min")

print("\n--- STILL BILLING (after cutover, top 12) ---")
for k, v in A["by_wf"].most_common(12):
    print(f"  {v/DAYS_AFTER:7.1f} min/day  {k}")

bpd_a = A["billable"] / DAYS_AFTER
monthly = bpd_a * RATE * 30
mins_month = bpd_a * 30
print(f"\n{'='*78}\nVERDICT INPUTS")
print(f"  projected hosted minutes/month : {mins_month:8.0f}")
print(f"  Team    (3,000 incl, $8/mo  2 seats): ${8 + max(0, mins_month-3000)*RATE:7.2f}/month")
print(f"  Enterprise (50,000 incl, $42/mo 2 seats): ${42.00:7.2f}/month")
print(f"  self-hosted failure rate: {100*A['fail_sh']/A['n_sh'] if A['n_sh'] else 0:.1f}%")
print("="*78)
