# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Put a re-run's gate table into the automatic review it belongs to.

    python3 review_refresh.py <comment-body> <jobs.md> <attempt>   -> new body on stdout

The section goes between two HTML-comment markers just above the review's
footer, and a later attempt replaces it. The footer is left byte-for-byte:
the once-per-PR guard keys on it. Nothing here is a model's judgement; it is
the ci-results table, minus the Log column (a path in the review job's
checkout, which means nothing on the PR).
"""

from __future__ import annotations

import re
import sys

START = "<!-- ci-rerun-results:start -->"
END = "<!-- ci-rerun-results:end -->"
FOOTER = "\n---\n_Automatic review by `claude-code-review.yml`"


def gate_table(jobs_md: str) -> str:
    rows = [line for line in jobs_md.splitlines() if line.startswith("|")]
    if len(rows) < 3:  # header, separator, at least one job
        raise ValueError("jobs.md has no gate table")
    return "\n".join("|" + "|".join(row.strip().strip("|").split("|")[:-1]) + "|" for row in rows)


def refresh(body: str, jobs_md: str, attempt: int) -> str:
    at = body.rfind(FOOTER)
    if at < 0:
        raise ValueError("not an automatic review: its footer is missing")
    review = re.sub(re.escape(START) + r".*?" + re.escape(END), "", body[:at], flags=re.DOTALL).rstrip("\n")
    section = (
        f"{START}\n"
        f"### Gate results after re-run (attempt {attempt})\n\n"
        "The review above was written against an earlier attempt of this run, on the same commit. "
        "These are the gates' latest results; the review text was not regenerated.\n\n"
        f"{gate_table(jobs_md)}\n"
        f"{END}"
    )
    return f"{review}\n\n{section}\n{body[at:]}"


if __name__ == "__main__":
    body_path, jobs_path, attempt = sys.argv[1:]
    with open(body_path) as fh_body, open(jobs_path) as fh_jobs:
        sys.stdout.write(refresh(fh_body.read(), fh_jobs.read(), int(attempt)))
