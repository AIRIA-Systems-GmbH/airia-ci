# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Was a failed Claude run a usage limit? Shared by the review (post_review.py) and the @claude responder (airia-ci#103).

A limit is recognised by the CLI's `rate_limit_event` with status `rejected` only when the run also has the unpaid
signature (at most one turn, `total_cost_usd` 0) — claude-code-action writes every SDK message into the execution
file, and the retry watchdog emits `rejected`, waits, then carries on, so a paid run that later fails keeps its
real cause — or by the result and error texts, which count on any run. The texts are never printed, only the reset
time is (the event's `resetsAt` as UTC, else the time phrase in the text). As a step it reads EXECUTION_FILE and,
when the run hit a limit (or has the zero-usage signature of a limit or an auth failure), emits a titled `::error::`
annotation. It always exits 0: the step that ran Claude has already failed, and this one only says why. A generic
failure prints nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime

# A usage or rate limit as Claude Code reports it ("Claude AI usage limit reached|<epoch>", "5-hour limit reached ∙
# resets 3pm", "You've hit your limit · resets 7pm (Europe/Berlin)", an assistant message's `error: rate_limit`).
# "limit reached" counts only qualified (a context or budget limit is another failure) or in the epoch form.
LIMIT = re.compile(r"\b(?:usage|rate|5-hour|five-hour|session|weekly)[ _-]limit|\blimit reached\|\d{9,11}\b"
                   r"|hit your limit|\blimit (?:will )?resets?\b", re.IGNORECASE)
EPOCH = re.compile(r"limit reached\|(\d{9,11})\b", re.IGNORECASE)
# Only a time after "resets"/"reset at" is ever printed, never the text around it (#113): "3pm", "15:00:00Z",
# "2026-11-01 15:00", "Oct 6, 1pm", "Monday 3pm" or "in 2 hours", then an optional " UTC" or an IANA zone in
# parentheses ("(Europe/Berlin)"). Anything else after "resets" is no reset time, and no newline is ever taken.
_TIME = r"(?:\d{1,2}(?::\d{2}){1,2}(?: ?(?i:[ap]m)\b|(?i:z)\b|\b)|\d{1,2} ?(?i:[ap]m)\b)"
_MONTH = (r"(?i:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
          r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_DAY = r"(?i:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?i:day)?\.?"
_ZONE = (r"(?: (?:UTC|GMT)\b| \((?:Africa|America|Antarctica|Asia|Atlantic|Australia|Europe|Indian|Pacific|Etc)"
         r"(?:/[A-Z][A-Za-z_+-]*)+\))?")
RESETS = re.compile(rf"(?i:\bresets?(?:\s+at|\s+on)?)\s+((?:\d{{4}}-\d{{2}}-\d{{2}}(?:[ T]{_TIME})?"
                    rf"|{_MONTH} \d{{1,2}}\b(?:,? (?i:at )?{_TIME})?|(?:{_DAY},? (?i:at )?)?{_TIME}"
                    rf"|(?i:in \d{{1,3}} (?:minutes?|hours?|days?))\b){_ZONE})")
RETRY = "comment `@claude` again after the limit resets."
# Said only when the limit is certain: a limit is the account's answer to an authenticated request, so the
# CLAUDE_CODE_OAUTH_TOKEN secret is not what to chase (Portfolio-Dashboard run 42495279385 cost an hour of that).
TOKEN_OK = "The CLAUDE_CODE_OAUTH_TOKEN authenticated, so this is not a secrets problem."


def last_result(msgs: list) -> dict:
    return next((m for m in reversed(msgs) if m.get("type") == "result"), {})


def rejected_limit(msgs: list) -> dict | None:
    """The `rate_limit_info` of the last `rate_limit_event` with status `rejected`, or None.

    Claude Code emits `{"type": "rate_limit_event", "rate_limit_info": {"status": ..., "resetsAt": <epoch>, ...}}`
    on every turn; `allowed` (and `allowed_warning`) carry a `resetsAt` too and mean nothing. Only `rejected` can be
    a limit, and only together with the unpaid signature (`hit_limit`): the retry watchdog emits the same event
    while it waits, then the run carries on.
    """
    for m in reversed(msgs):
        info = m.get("rate_limit_info")
        if m.get("type") == "rate_limit_event" and isinstance(info, dict) and info.get("status") == "rejected":
            return info
    return None


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
        if m := RESETS.search(t):
            return m.group(1)
    return None


def limit_reset(msgs: list) -> str | None:
    """When the limit resets: the rejected event's `resetsAt` as UTC first, else what the texts say."""
    info = rejected_limit(msgs)
    if info and isinstance(info.get("resetsAt"), (int, float)) and not isinstance(info["resetsAt"], bool):
        return datetime.fromtimestamp(int(info["resetsAt"]), UTC).strftime("%Y-%m-%d %H:%M UTC")
    return reset_time(failure_texts(msgs))


def unpaid(msgs: list) -> bool:
    """Did the run pay for no model turn (at most one turn, no cost)? A limit stops it there; an answer never does."""
    result = last_result(msgs)
    turns = result.get("num_turns")
    return isinstance(turns, int) and turns <= 1 and result.get("total_cost_usd") == 0


def limit_notice(msgs: list) -> bool:
    """Is a result that is not an error a limit notice rather than an answer?

    A limit can end a run with subtype `success`, `is_error` unset, one turn, no cost and the limit text as the
    `result` (this repository's own dispatch run, 2026-11-08: "You've hit your weekly limit · resets Nov 11 at
    10am (Africa/Johannesburg)", with a rejected `rate_limit_event`). The unpaid signature is what keeps a real
    answer that mentions a rate limit, or one written after the retry watchdog waited out a limit, from counting.
    """
    return unpaid(msgs) and hit_limit(msgs)


def zero_usage_turns(msgs: list) -> int | None:
    """The turn count of a run that errored after at most one turn with no model usage and no cost, else None."""
    result = last_result(msgs)
    turns = result.get("num_turns")
    if (result.get("is_error") and not result.get("modelUsage") and result.get("total_cost_usd") == 0
            and isinstance(turns, int) and turns <= 1):
        return turns
    return None


def hit_limit(msgs: list) -> bool:
    """A rejected event on an unpaid run, or a usage or rate limit named in the result or error text.

    The text match counts on any run. The event alone counts only with `unpaid` (at most one turn, no cost): a paid
    run that waited the rejection out and then failed for another reason is not a limit.
    """
    return ((rejected_limit(msgs) is not None and unpaid(msgs))
            or any(LIMIT.search(t) for t in failure_texts(msgs)))


def load(path: str) -> list:
    try:
        with open(path) as fh:
            msgs = json.load(fh)
    except (OSError, ValueError):
        return []
    return [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []


def main() -> int:
    msgs = load(os.environ["EXECUTION_FILE"])
    if hit_limit(msgs):
        when = limit_reset(msgs)
        resets = f"the limit resets {when}" if when else "the text gives no reset time"
        print(f"::error title=Claude usage limit::Claude did not answer: the run reports a usage or rate limit; {resets}; "
              f"{RETRY} {TOKEN_OK}")
    elif (turns := zero_usage_turns(msgs)) is not None:
        print(f"::error title=Likely Claude usage limit or auth failure::Claude did not answer: the run errored after {turns} "
              f"turn{'s' if turns != 1 else ''} with no model usage and no cost, which is the signature of a usage limit "
              f"or an auth failure (the log does not say which); if it is the limit, {RETRY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
