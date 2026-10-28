# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Post the automatic review: one summary issue comment, one PR comment per finding.

    python3 post_review.py        (configured by the environment, see main())

Claude's final message is the review. It ends with a fenced `airia-findings`
block holding the findings as JSON; the block is parsed here, never trusted to
a model's formatting. A finding's text always reaches the PR (airia-ci#98): a
`carried` that is not a boolean is coerced and the summary names the coercion;
a finding failing any other field, an unparseable block or a missing block is
quoted in the summary, which is posted, and then the job fails with the schema
error named ("green means posted"). An empty list is valid.

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
- Every run also posts a pull-request review (event COMMENT, never APPROVE) with `commit_id` the
  reviewed head, so a re-review with no findings still leaves a review on its head (airia-ci#99).
  Its body points at the summary and has no footer, so each run is still counted once.
- With no review text the check fails (airia-ci#54) and says why: a usage or rate limit named in the
  result or error text is titled `Claude usage limit` with the reset time when the text gives one, and a
  run that errored after at most one turn with no model usage and no cost is titled as a likely usage
  limit or auth failure (airia-ci#103). Only the reset time is printed, never the text itself.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
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
# A usage or rate limit as Claude Code reports it ("Claude AI usage limit reached|<epoch>", "5-hour limit reached ∙
# resets 3pm", "You've hit your limit · resets 7pm (Europe/Berlin)", an assistant message's `error: rate_limit`).
LIMIT = re.compile(r"\b(?:usage|rate|5-hour|five-hour|session|weekly)[ _-]limit|\blimit reached|hit your limit"
                   r"|\blimit (?:will )?resets?\b", re.IGNORECASE)
EPOCH = re.compile(r"limit reached\|(\d{9,11})\b", re.IGNORECASE)
# Only a short run of time-like characters after "resets"/"reset at" is ever printed, never the text around it.
RESETS = re.compile(r"\bresets?(?:\s+at|\s+on)?\s+(\w[\w :+/()-]{0,40})", re.IGNORECASE)
RETRY = "re-run this job after the reset or comment `@claude please review`."


class ReviewError(Exception):
    """The review cannot be posted as it stands; the job fails with this text under `title`."""

    def __init__(self, message: str, title: str = "No review posted"):
        super().__init__(message)
        self.title = title


def last_result(msgs: list) -> dict:
    return next((m for m in reversed(msgs) if m.get("type") == "result"), {})


def load_message(path: str) -> tuple[str, list[str], list]:
    """Claude's final message from the execution file, the diagnostics to print, and the file's messages."""
    try:
        with open(path) as fh:
            msgs = json.load(fh)
    except (OSError, ValueError) as exc:
        return "", [f"No execution file ({exc}); Claude did not run to completion."], []
    result = last_result(msgs)
    lines = [f"subtype: {result.get('subtype')}, turns: {result.get('num_turns')}, is_error: {result.get('is_error')}"]
    # Denials are not failures on their own (a denied push is the settings
    # working), but they are the first thing to read when a review is thin.
    denials = result.get("permission_denials") or []
    lines.append(f"permission denials: {len(denials)}")
    lines += [f"- {d.get('tool_name')}: {json.dumps(d.get('tool_input'))[:200]}" for d in denials[:20]]
    text = (result.get("result") or "").strip()
    if result.get("is_error") or not text:
        return "", [*lines, "No review text in the final message."], msgs
    return text, lines, msgs


def failure_texts(msgs: list) -> list[str]:
    """The result and error texts of a run: what a limit is recognised by. Never printed."""
    result = last_result(msgs)
    texts = [result.get("result"), result.get("error"), *(result.get("errors") or [])]
    for m in msgs:
        # An API error reaches the transcript as an assistant message flagged `error`; its text is the error's.
        if m.get("type") == "assistant" and m.get("error"):
            content = (m.get("message") or {}).get("content") or []
            texts += [m["error"], *(b.get("text") for b in content if isinstance(b, dict))]
    return [t for t in texts if isinstance(t, str)]


def reset_time(texts: list[str]) -> str | None:
    """When the limit resets, if the text says so: an epoch as UTC, or the time phrase after "resets"."""
    for t in texts:
        if m := EPOCH.search(t):
            return datetime.fromtimestamp(int(m.group(1)), UTC).strftime("%Y-%m-%d %H:%M UTC")
        for m in RESETS.finditer(t):
            when = m.group(1).strip()
            if any(c.isdigit() for c in when):
                return when
    return None


def no_review_error(msgs: list, pr: str) -> ReviewError:
    """Why there is no review: a usage limit, its zero-usage signature, or the generic failure (airia-ci#103)."""
    unreviewed = f"Claude wrote no review for PR #{pr}, so treat it as UNREVIEWED"
    texts = failure_texts(msgs)
    if any(LIMIT.search(t) for t in texts):
        when = reset_time(texts)
        resets = f"the limit resets {when}" if when else "the text gives no reset time"
        return ReviewError(f"{unreviewed}: the run reports a usage or rate limit; {resets}; {RETRY}",
                           title="Claude usage limit")
    result = last_result(msgs)
    turns = result.get("num_turns")
    if (result.get("is_error") and not result.get("modelUsage") and result.get("total_cost_usd") == 0
            and isinstance(turns, int) and turns <= 1):
        return ReviewError(f"{unreviewed}: the run errored after {turns} turn{'s' if turns != 1 else ''} with no model usage and no cost, "
                           "which is the signature of a usage limit or an auth failure (the log does not say which); "
                           f"if it is the limit, {RETRY}", title="Likely Claude usage limit or auth failure")
    return ReviewError(f"claude-review produced no review text for PR #{pr}. Treat this PR as UNREVIEWED; "
                       "comment `@claude please review` to retry.")


def defuse(text: str) -> str:
    """Text safe to quote in the summary: the review footer inside it would count as a second review."""
    return text.replace("_Automatic review by", "Automatic review by")


def fenced(text: str) -> str:
    """The text in a code fence longer than any run of backticks inside it."""
    ticks = "`" * max([3, *(len(run) + 1 for run in re.findall(r"`+", text))])
    return f"{ticks}text\n{defuse(text)}\n{ticks}"


def coerce_carried(f: dict) -> tuple[bool, str | None]:
    """The `carried` flag of a finding and, when it was not a JSON boolean, what was assumed."""
    value = f.get("carried")
    if isinstance(value, bool):
        return value, None
    if "carried" not in f:
        return False, "carried was missing: treated as false (a new finding)"
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        read = value.strip().lower()
        return read == "true", f"carried was the string {json.dumps(value)}: read as {read}"
    return False, f"carried was {json.dumps(value)}, not true or false: treated as false (a new finding)"


def check_finding(f: object, seen: set[str]) -> str | None:
    """Why the finding cannot be posted as a comment, or None when it can."""
    if not isinstance(f, dict):
        return "is not an object"
    if not isinstance(f.get("id"), str) or not ID.fullmatch(f["id"]):
        return "has no usable id (letters, digits, . _ -; at most 40)"
    if f["id"] in seen:
        return "the id is used twice"
    if not isinstance(f.get("file"), str) or not f["file"].strip():
        return "has no file"
    line = f.get("line")
    if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
        return "line must be a positive integer or absent"
    if f.get("severity") not in SEVERITIES:
        return f"severity must be one of {', '.join(SEVERITIES)}"
    if not isinstance(f.get("text"), str) or not f["text"].strip():
        return "has no text"
    if FOOTER_START in f["text"]:
        return "the text carries the review footer, which would count as a review"
    return None


def recovered(f: object) -> str:
    """Whatever of a finding that failed validation can still be read, as markdown for the summary."""
    if isinstance(f, dict) and isinstance(f.get("text"), str) and f["text"].strip():
        head = " · ".join(str(f[k]) for k in ("id", "severity", "file") if isinstance(f.get(k), (str, int)))
        quoted = "\n".join("  > " + line for line in defuse(f["text"]).strip().splitlines())
        return (f"  {head}\n" if head else "") + quoted
    return fenced(json.dumps(f, indent=1))


def parse_findings(text: str) -> tuple[str, list[dict], list[str], list[tuple[str, str]]]:
    """Never drops a finding's text: (summary, postable findings, coercion notes, problems).

    A problem is (what is wrong, the recovered text to show with it); the job fails after posting when
    there is one. A message with no usable block is its own summary, and its problem has no recovered text.
    """
    blocks = list(BLOCK.finditer(text))
    if not blocks or text[blocks[-1].end():].strip():
        return defuse(text), [], [], [("the final message does not end with an `airia-findings` block", "")]
    last = blocks[-1]
    summary = defuse(text[:last.start()] + text[last.end():]).strip()
    try:
        findings = json.loads(last.group(1))["findings"]
        if not isinstance(findings, list):
            raise TypeError("`findings` is not a list")
    except (ValueError, KeyError, TypeError) as exc:
        return summary, [], [], [(f"the `airia-findings` block is not JSON of the form {{\"findings\": [...]}} ({exc})",
                                  "The block as the review wrote it:\n\n" + fenced(last.group(1)))]
    good: list[dict] = []
    notes: list[str] = []
    problems: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, f in enumerate(findings, 1):
        why = check_finding(f, seen)
        if why:
            who = f["id"] if isinstance(f, dict) and isinstance(f.get("id"), str) and ID.fullmatch(f["id"]) else i
            problems.append((f"finding {who}: {why}", recovered(f)))
            continue
        seen.add(f["id"])
        carried, note = coerce_carried(f)
        if note:
            notes.append(f"finding {f['id']}: {note}")
        good.append({**f, "carried": carried})
    return summary, good, notes, problems


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


def post_review_object(repo: str, pr: str, head: str, run_url: str, findings: list[dict], problems: list) -> None:
    """Record the verdict as a pull-request review on the reviewed head (airia-ci#99).

    GitHub keeps a review per commit_id; an issue comment is not one, so without this a re-review with no
    findings leaves no review on its head. Event COMMENT only: a bot approval is not the policy. The body
    carries no review footer (the summary comment is the one the guards count) and just points at the summary.
    """
    ids = ", ".join(f["id"] for f in findings)
    verdict = (f"{len(findings)} finding{'s' if len(findings) != 1 else ''} ({ids}), each its own comment"
               if findings else "no findings")
    if problems:
        verdict += "; the findings block broke its schema, see the summary"
    body = (f"Automatic review of `{head[:7]}`: {verdict}. The full review is the summary comment "
            f"of [this run]({run_url}).")
    with tempfile.NamedTemporaryFile("w", suffix=".md", dir=os.environ.get("RUNNER_TEMP"), delete=False) as fh:
        fh.write(body)
    try:
        gh_ok("api", "--method", "POST", f"repos/{repo}/pulls/{pr}/reviews", "-f", f"commit_id={head}",
              "-f", "event=COMMENT", "-F", f"body=@{fh.name}")
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


def problems_section(notes: list[str], problems: list[tuple[str, str]]) -> str:
    """The summary's account of what was assumed and what could not be posted as a comment, text included."""
    parts = []
    if notes:
        parts.append("### Assumed in the findings block\n\n" + "\n".join(f"- {n}" for n in notes))
    if problems:
        rows = "\n".join(f"- {why}" + (f"\n{shown}" if shown else "") for why, shown in problems)
        parts.append("### Findings that could not be posted as comments\n\n"
                     "The findings block broke its schema; the check fails, and what could be read is here.\n\n" + rows)
    return "".join("\n\n" + p for p in parts)


def main() -> int:
    env = os.environ
    repo, pr, run_id = env["GITHUB_REPOSITORY"], env["PR"], env["RUN_ID"]
    text, notes, msgs = load_message(env["EXECUTION_FILE"])
    print("\n".join(notes))
    try:
        if not text:
            raise no_review_error(msgs, pr)
        summary, findings, notes, problems = parse_findings(text)
        earlier: dict[tuple[str, str], str] = {}
        urls: dict[str, str | None] = {}
        head = env.get("HEAD_SHA") or gh_ok("api", f"repos/{repo}/pulls/{pr}", "--jq", ".head.sha")
        if findings:
            earlier = posted_findings(repo, pr)
            # Only a finding the review calls carried is linked to a comment already on the PR, and only
            # when that comment is for the same id and file; a new one is always posted, even on a collision.
            linked = {f["id"]: earlier[(f["id"], f["file"])] for f in findings
                      if f["carried"] and (f["id"], f["file"]) in earlier}
            urls = {f["id"]: linked.get(f["id"]) or post_finding(repo, pr, head, run_id, f) for f in findings}
        carried = {f["id"] for f in findings if f["carried"] and f["id"] in linked}
        orphans = {f["id"] for f in findings if f["carried"]} - carried
        section = findings_section(findings, urls, carried, orphans)
        section += problems_section(notes, problems)
        version = env.get("CLAUDE_VERSION") or "installed by the action"
        footer = f"\n\n---\n{FOOTER_START} — [run log]({env['RUN_URL']}), Claude Code {version}._\n"
        # The review object first: if it fails no summary is posted, so a re-run attempt reviews afresh
        # instead of finding a summary and only refreshing its gate table.
        post_review_object(repo, pr, head, env["RUN_URL"], findings, problems)
        with tempfile.NamedTemporaryFile("w", suffix=".md", dir=env.get("RUNNER_TEMP"), delete=False) as fh:
            fh.write(summary + ("\n\n" + section if section else "") + footer)
        try:
            gh_ok("pr", "comment", pr, "--repo", repo, "--body-file", fh.name)
        finally:
            os.unlink(fh.name)
        if problems:
            raise ReviewError("the review was posted, but its findings block broke the schema: "
                              + "; ".join(why for why, _ in problems), title="Review posted with a malformed findings block")
    except ReviewError as exc:
        print(f"::error title={exc.title}::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
