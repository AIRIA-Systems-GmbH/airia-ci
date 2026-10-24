# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Post the automatic review: one summary issue comment, one PR comment per finding.

    python3 post_review.py        (configured by the environment, see main())

Claude's final message is the review. It ends with a fenced `airia-findings`
block holding the findings as JSON; the block is parsed here, never trusted to
a model's formatting. A missing or malformed block fails the job, as a missing
review does ("green means posted"); an empty list is valid.

- Each finding is posted as an inline review comment on its line, or as a
  file-level comment when the line is not in the diff. Every one carries a
  hidden marker naming the run and the finding id.
- A finding whose file is not in the diff at all cannot hold a threaded reply,
  so it gets no comment: the summary carries its text instead.
- A finding the review marks `carried` (still open from an earlier review) is
  not posted again when a marker with its id AND file is on the PR; the
  summary links that comment. A finding that is not carried is always posted
  as new, and a carried one with no such marker is posted as new and noted.
- The summary stays ONE issue comment with the footer the once-per-head guard
  counts. A finding comment never carries that footer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote, unquote

FOOTER_START = "_Automatic review by `claude-code-review.yml`"
SEVERITIES = ("blocker", "major", "minor", "nit")
ID = re.compile(r"[A-Za-z0-9._-]{1,40}")
BLOCK = re.compile(r"^```airia-findings[ \t]*\n(.*?)\n```[ \t]*$", re.DOTALL | re.MULTILINE)
MARKER = re.compile(r"<!-- airia-finding run=\d+ id=(\S+) file=(\S+) -->")
# GitHub's 422 for a comment whose line or path is not in the PR's diff. Its docs give no wording, so these
# are the stable fragments of the messages it returns ("...line could not be resolved", "...must be part of
# the diff"); any other 422 (a stale commit_id, a rejected body) is a real failure.
NOT_IN_THE_DIFF = re.compile(r"could not be resolved|must be part of the diff", re.IGNORECASE)
NOT_IN_DIFF = "not in the diff: answer in a PR comment that quotes its id"


class ReviewError(Exception):
    """The review cannot be posted as it stands; the job fails with this text."""


def load_message(path: str) -> tuple[str, list[str]]:
    """Claude's final message from the execution file, and the diagnostics to print."""
    try:
        with open(path) as fh:
            msgs = json.load(fh)
    except (OSError, ValueError) as exc:
        return "", [f"No execution file ({exc}); Claude did not run to completion."]
    result = next((m for m in reversed(msgs) if m.get("type") == "result"), {})
    lines = [f"subtype: {result.get('subtype')}, turns: {result.get('num_turns')}, is_error: {result.get('is_error')}"]
    # Denials are not failures on their own (a denied push is the settings
    # working), but they are the first thing to read when a review is thin.
    denials = result.get("permission_denials") or []
    lines.append(f"permission denials: {len(denials)}")
    lines += [f"- {d.get('tool_name')}: {json.dumps(d.get('tool_input'))[:200]}" for d in denials[:20]]
    text = (result.get("result") or "").strip()
    if result.get("is_error") or not text:
        return "", [*lines, "No review text in the final message."]
    return text, lines


def parse_findings(text: str) -> tuple[str, list[dict]]:
    """The summary (the message without its findings block) and the validated findings."""
    blocks = list(BLOCK.finditer(text))
    if not blocks or text[blocks[-1].end():].strip():
        raise ReviewError("the final message does not end with an `airia-findings` block")
    last = blocks[-1]
    try:
        findings = json.loads(last.group(1))["findings"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ReviewError(f"the `airia-findings` block is not JSON of the form {{\"findings\": [...]}} ({exc})") from exc
    if not isinstance(findings, list):
        raise ReviewError("`findings` in the `airia-findings` block is not a list")
    seen: set[str] = set()
    for i, f in enumerate(findings, 1):
        where = f"finding {i}"
        if not isinstance(f, dict):
            raise ReviewError(f"{where} is not an object")
        if not isinstance(f.get("id"), str) or not ID.fullmatch(f["id"]):
            raise ReviewError(f"{where} has no usable id (letters, digits, . _ -; at most 40)")
        where = f"finding {f['id']}"
        if f["id"] in seen:
            raise ReviewError(f"{where}: the id is used twice")
        seen.add(f["id"])
        if not isinstance(f.get("file"), str) or not f["file"].strip():
            raise ReviewError(f"{where} has no file")
        line = f.get("line")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
            raise ReviewError(f"{where}: line must be a positive integer or absent")
        if f.get("severity") not in SEVERITIES:
            raise ReviewError(f"{where}: severity must be one of {', '.join(SEVERITIES)}")
        if not isinstance(f.get("text"), str) or not f["text"].strip():
            raise ReviewError(f"{where} has no text")
        if not isinstance(f.get("carried"), bool):
            raise ReviewError(f"{where}: carried must be true or false")
        if FOOTER_START in f["text"]:
            raise ReviewError(f"{where}: the text carries the review footer, which would count as a review")
    return (text[:last.start()] + text[last.end():]).strip(), findings


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def gh_ok(*args: str) -> str:
    p = gh(*args)
    if p.returncode:
        raise ReviewError(f"gh {' '.join(args[:2])} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def posted_findings(repo: str, pr: str) -> dict[tuple[str, str], str]:
    """(finding id, file) -> URL of the review comment already carrying it.

    Any bot counts (an app token as well as Actions'); a person quoting a marker never does.
    """
    jq = ('.[] | select(.user.type == "Bot") | select(.body | contains("<!-- airia-finding ")) '
          "| {html_url, body} | tojson")
    found: dict[tuple[str, str], str] = {}
    for line in gh_ok("api", "--paginate", f"repos/{repo}/pulls/{pr}/comments", "--jq", jq).splitlines():
        comment = json.loads(line)
        for fid, file in MARKER.findall(comment["body"]):
            found.setdefault((fid, unquote(file)), comment["html_url"])
    return found


def comment_body(finding: dict, run_id: str) -> str:
    return (f"**{finding['severity']}** · {finding['id']}\n\n{finding['text'].strip()}\n\n"
            f"<!-- airia-finding run={run_id} id={finding['id']} file={quote(finding['file'], safe='/')} -->\n")


def post_finding(repo: str, pr: str, head: str, run_id: str, finding: dict) -> str | None:
    """Post one finding as narrowly as the diff allows; its URL, or None when its file is not in the diff."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", dir=os.environ.get("RUNNER_TEMP"), delete=False) as fh:
        fh.write(comment_body(finding, run_id))
    try:
        base = ["api", "--method", "POST", f"repos/{repo}/pulls/{pr}/comments", "--jq", ".html_url",
                "-F", f"body=@{fh.name}", "-f", f"commit_id={head}", "-f", f"path={finding['file']}"]
        attempts = []
        if finding.get("line"):
            attempts.append([*base, "-F", f"line={finding['line']}", "-f", "side=RIGHT"])
        attempts.append([*base, "-f", "subject_type=file"])
        for args in attempts:
            p = gh(*args)
            if not p.returncode:
                return p.stdout.strip()
            # A 422 about the line or the path means it is not in the diff: try the next, wider attempt. `gh api`
            # puts only "gh: Validation Failed (HTTP 422)" on stderr; the errors[].message detail is in the body on stdout.
            if not (("HTTP 422" in p.stderr) and NOT_IN_THE_DIFF.search(p.stderr + "\n" + p.stdout)):
                raise ReviewError(f"finding {finding['id']}: gh api failed: {p.stderr.strip()}")
        return None
    finally:
        os.unlink(fh.name)


def findings_section(findings: list[dict], urls: dict[str, str | None], carried: set[str], orphans: set[str]) -> str:
    if not findings:
        return ""
    rows = []
    for f in findings:
        at = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
        head = f"- **{f['id']}** ({f['severity']}) `{at}`"
        if f["id"] in carried:
            head += " (still open from an earlier review)"
        if f["id"] in orphans:
            head += " (marked as carried, but no earlier comment has this id and file: posted as new)"
        if urls[f["id"]]:
            rows.append(f"{head} — [comment]({urls[f['id']]})")
        else:
            body = "\n".join("  > " + line for line in f["text"].strip().splitlines())
            rows.append(f"{head} ({NOT_IN_DIFF})\n{body}")
    return "### Findings\n\nEach finding is its own comment: reply in its thread.\n\n" + "\n".join(rows)


def main() -> int:
    env = os.environ
    repo, pr, run_id = env["GITHUB_REPOSITORY"], env["PR"], env["RUN_ID"]
    text, notes = load_message(env["EXECUTION_FILE"])
    print("\n".join(notes))
    try:
        if not text:
            raise ReviewError(f"claude-review produced no review text for PR #{pr}. Treat this PR as UNREVIEWED; "
                              "comment `@claude please review` to retry.")
        summary, findings = parse_findings(text)
        earlier: dict[tuple[str, str], str] = {}
        urls: dict[str, str | None] = {}
        if findings:
            head = env.get("HEAD_SHA") or gh_ok("api", f"repos/{repo}/pulls/{pr}", "--jq", ".head.sha")
            earlier = posted_findings(repo, pr)
            # Only a finding the review calls carried is linked to a comment already on the PR, and only
            # when that comment is for the same id and file; a new one is always posted, even on a collision.
            linked = {f["id"]: earlier[(f["id"], f["file"])] for f in findings
                      if f["carried"] and (f["id"], f["file"]) in earlier}
            urls = {f["id"]: linked.get(f["id"]) or post_finding(repo, pr, head, run_id, f) for f in findings}
        carried = {f["id"] for f in findings if f["carried"] and f["id"] in linked}
        orphans = {f["id"] for f in findings if f["carried"]} - carried
        section = findings_section(findings, urls, carried, orphans)
        version = env.get("CLAUDE_VERSION") or "installed by the action"
        footer = f"\n\n---\n{FOOTER_START} — [run log]({env['RUN_URL']}), Claude Code {version}._\n"
        with tempfile.NamedTemporaryFile("w", suffix=".md", dir=env.get("RUNNER_TEMP"), delete=False) as fh:
            fh.write(summary + ("\n\n" + section if section else "") + footer)
        try:
            gh_ok("pr", "comment", pr, "--repo", repo, "--body-file", fh.name)
        finally:
            os.unlink(fh.name)
    except ReviewError as exc:
        print(f"::error title=No review posted::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
