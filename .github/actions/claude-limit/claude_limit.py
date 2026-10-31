# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""Was a failed Claude run a usage limit? Shared by the review (post_review.py) and the @claude responder (airia-ci#103).

The execution file's result and error texts are what a limit is recognised by; they are never printed, only the
reset time is. As a step it reads EXECUTION_FILE and, when the run hit a limit (or has the zero-usage signature
of a limit or an auth failure), emits a titled `::error::` annotation. It always exits 0: the step that ran Claude
has already failed, and this one only says why. A generic failure prints nothing.
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
# Only a short run of time-like characters after "resets"/"reset at" is ever printed, never the text around it.
RESETS = re.compile(r"\bresets?(?:\s+at|\s+on)?\s+(\w[\w :+/()-]{0,40})", re.IGNORECASE)
RETRY = "comment `@claude` again after the limit resets."


def last_result(msgs: list) -> dict:
    return next((m for m in reversed(msgs) if m.get("type") == "result"), {})


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


def zero_usage_turns(msgs: list) -> int | None:
    """The turn count of a run that errored after at most one turn with no model usage and no cost, else None."""
    result = last_result(msgs)
    turns = result.get("num_turns")
    if (result.get("is_error") and not result.get("modelUsage") and result.get("total_cost_usd") == 0
            and isinstance(turns, int) and turns <= 1):
        return turns
    return None


def hit_limit(msgs: list) -> bool:
    return any(LIMIT.search(t) for t in failure_texts(msgs))


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
        when = reset_time(failure_texts(msgs))
        resets = f"the limit resets {when}" if when else "the text gives no reset time"
        print(f"::error title=Claude usage limit::Claude did not answer: the run reports a usage or rate limit; {resets}; {RETRY}")
    elif (turns := zero_usage_turns(msgs)) is not None:
        print(f"::error title=Likely Claude usage limit or auth failure::Claude did not answer: the run errored after {turns} "
              f"turn{'s' if turns != 1 else ''} with no model usage and no cost, which is the signature of a usage limit "
              f"or an auth failure (the log does not say which); if it is the limit, {RETRY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
