<!--
SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
SPDX-License-Identifier: Apache-2.0
-->

# Design: a debt ratchet for the shared Python gate

Architect's record, 2026-11-06. Status: decided; the coordinator files the Issue from it.

## Problem

`reusable-python-gate.yml` is red or green. A consumer with known debt (61 failing tests,
244 pyright errors, a measured coverage far under the default floor of 90) cannot adopt it
without being red on every PR. It ratchets locally against committed baseline files and asks
for the same in the shared gate.

## 1. Decision on "tighten-only with an expiry": accept, with three amendments

Accepted because it is the only form that lets the debt be *carried* without lowering the
standard for anyone else: the standard stays 90 %, zero failing tests, zero type errors; a
consumer holds a dated, shrinking exception to it. This is the same shape as the ruff baseline
(floor, committed counts that may only fall), so it conforms to that precedent.

Amendments:

- **A. "May only fall" is checked against the base branch, not only against the file.** The ruff
  precedent compares counts with the file and leaves a raised baseline to the diff and the
  review (HOWTO, "What is not machine-checked"). That is too weak here: the file also holds the
  expiry, and a consumer could raise a figure and the date in one commit. On a pull request the
  gate reads the base's copy (`HEAD^1:.airia/debt-baseline.json`) and fails if any figure is
  above it, any test id is new, the coverage floor is lower, or `expires` is later. Ruff's
  own baseline keeps its behaviour; it is not touched.
- **B. The expiry is a hard stop, with no renewal in v1.** A renewable date is not an expiry.
- **C. A maximum horizon is enforced by the gate**, so the first date cannot be set far away.

Consequence, accepted: after the expiry the consumer's only green is a repository at the
standard. If a consumer cannot get there, the extension is a human ruling in an airia-ci Issue
and a change to the gate, never a consumer edit.

## 2. What is baselined, and how it is compared

One committed file, `.airia/debt-baseline.json` (`.airia/` is never docs, so a PR that touches
it always runs lint and test). Every section is optional; an absent section means the standard
for that gate, unchanged.

```json
{
  "version": 1,
  "set": "2026-11-06",
  "expires": "2027-02-04",
  "tests": {"failing": ["tests/test_a.py::test_x", "tests/test_b.py::TestC::test_y[1]"]},
  "types": {"pyright": {"reportAttributeAccessIssue": 120, "reportArgumentType": 31}},
  "coverage": {"floor": 41}
}
```

- **Tests: failing node ids, not a count.** A count lets a new failure hide behind a fixed
  one. The gate runs pytest once, takes the failing and erroring ids from the `-rfE` summary
  (`FAILED id - msg` and `ERROR id - msg`, normalised to the id), and compares as sets: every
  failing id must be in the baseline. An id in the baseline that now passes passes the gate and
  is reported as tightened. Pytest exit codes 2-5 fail outright, and exit 1 with no parsed id
  fails (a crash is not "no failures"). Exit 0 with a non-empty baseline is fine.
- **Types: pyright error count per rule** (`--outputjson`, `severity == "error"`, keyed by
  `rule`), from the same run the lint job already makes. Each rule's count must be at or
  below its baseline, and a rule absent from the file counts as 0. Per rule, not per file: it
  is the ruff precedent, it survives moving code between files, and a per-file key churns on
  every rename. The weakness (one error traded for another of the same rule) is accepted.
  Not a single total: a total lets one rule rise while another falls.
- **Coverage: the floor value**, a number in `[0, 100]`. While the ratchet is valid, the
  effective floor is `coverage.floor` instead of 90; measured coverage must reach it. It may
  only rise. Only the baseline can go under 90; the `coverage-floor` input stays refused under
  90 as today.
- **Who may raise a figure: nobody.** There is no input, label or comment that raises one. A
  raise fails on the PR (amendment A). Deleting the file or the `debt-ratchet: true` line is
  visible in the diff and puts that gate back at the standard, which is red for a consumer
  with debt: the safe direction.
- **Not baselined:** ruff (it has its own baseline), format, import-linter, security. mypy
  strict (one repository) is out of scope.

## 3. The expiry

- **Where:** `expires` (UTC date, `YYYY-MM-DD`) and `set` (the day the file was first written)
  in the baseline file. One date for the whole file, not per section.
- **After it:** the gate goes **red**, not a warning. Past `expires` (UTC, run date), the
  baselined sections are ignored, the standard applies, and the error says so and names the
  date. Within 14 days of it the job emits a `::warning` with the days left.
- **Maximum horizon: 90 days**, `expires - set <= 90`, checked on every run, so it holds on
  a push to `main` too. On a PR, `set` and `expires` cannot move later than the base's copy
  (amendment A); a first file (absent at base) must have `set` within 1 day of the run date,
  so a far-future `set` cannot buy extra days.

## 4. Input surface of `reusable-python-gate.yml`

One new input, on the workflow and on its calls into the actions:

| Input | Type | Default | Meaning |
|---|---|---|---|
| `debt-ratchet` | boolean | `false` | Hold the baselined gates to `.airia/debt-baseline.json` |

A boolean with a fixed path, not a path input: the ruff baseline's path is fixed, and a
second name is flexibility nobody asked for. Default `false` leaves every existing caller
byte-for-byte as today.

With `debt-ratchet: true` the gate **fails loud**, as an `::error` in the lint and test
jobs, never a fallback to the standard or a silent pass, on: the file missing; not valid JSON;
an unknown key or a wrong type; `version` other than 1; a malformed date; `expires` not after
`set`; a horizon over 90 days; a negative count; a coverage floor outside `[0, 100]`; a
duplicate test id. A file present with `debt-ratchet: false` is ignored and notes that.
Pytest and pyright still run normally; only their pass rule changes.

## 5. Reporting a tightened baseline

When a measured figure is better than its baseline (a baselined test now passes, a pyright
rule's count fell, coverage floored to a whole point is above `floor`), the gate **passes**
and writes to the job summary and as a `::notice`: what improved, and the full replacement
JSON. The consumer commits the lower figure with `ci-local.sh --write-debt-baseline`, the
twin of `--write-baseline`. The writer never raises: it takes the minimum of the old file
and the measurement, drops fixed ids and rules at 0, keeps `set` and `expires`, and refuses to
write a measurement that is worse. A bot does not commit it: no token here can push to a
consumer's branch, and a person commits the lower figure, as with the ruff baseline.

## 6. Slices (each one PR; tests slice the shipped `run:` blocks per CLAUDE.md)

New Python goes in a file beside the action, `.github/actions/python-toolchain/debt.py`, not a
heredoc. The Issue announcing the call contract is filed before slice 3 merges (slices 1-2
add no input).

1. **Baseline file: load, validate, expiry, horizon.** `debt.py check` and the writer
   skeleton. Tests (`tests/test_debt_baseline.py`): missing file, bad JSON, unknown key,
   wrong types, bad dates, horizon 90 / 91 days, expired (run date injected), warning at
   14 days, and a valid file. Every fail-loud case of section 4 has one test.
2. **Only-tighten against the base.** `debt.py compare BASE HEAD` plus a `fetch-depth: 2` on
   the lint checkout. Tests: a raised count, a new id, a lower floor, a later `expires`, a
   later `set`, a first file (absent at base), no base on a push, and each tightened pair
   passing.
3. **Coverage section and the input.** `debt-ratchet` declared and passed to the
   python-toolchain action; the toolchain accepts a floor under 90 only from a valid file;
   the test job's pytest step uses the baseline floor. Tests: floor 0, 41 and 90, measured
   below / above, input off ignores the file, the 90 refusal of `coverage-floor` still holds,
   and the existing `Pytest at the coverage floor` tests unchanged.
4. **Pyright per rule.** The lint job's type-check step with `--outputjson`, parsed by
   `debt.py`; mypy-strict with `debt-ratchet` and a `types` section fails loud. Tests: a fake
   pyright emitting JSON, a rule above / equal / below, a rule absent from the file, a
   non-error severity ignored, pyright output that is not JSON.
5. **Failing test ids.** `-rfE` parsing and the set comparison in the test job. Tests: a fake
   pytest output with `FAILED` and `ERROR` lines, a new id, a fixed id, parametrised ids with
   brackets and spaces, exit codes 1-5, exit 1 with no parsed id, a matrix of two Pythons
   each compared on its own.
6. **Writer, summary, docs.** `ci-local.sh --write-debt-baseline` (only falls), the notice and
   summary of section 5, HOWTO (a section next to the ruff one; "May not" gets the dated
   exception), and a case in `tests/test_python_toolchain.py` style for each. Tests: the
   writer refuses to raise, drops fixed ids, keeps the dates, and the notice text carries
   JSON that round-trips through `check`.

## 7. Out of scope

- Renewing or extending an expiry; a human ruling and an Issue if it is ever needed.
- Per-file pyright baselines, mypy-strict, flaky-test quarantine, and baselining `security`.
- A bot that commits the lowered figure; migrating the consumer's local script (the consumer's
  own PR, after slice 5).
- A check in the weekly conformance scan for expired or nearly expired baselines: a worthwhile
  follow-up, not needed for the gate to be safe.
- Any change to the ruff baseline, its cut-over, or its warn-then-fail behaviour.
