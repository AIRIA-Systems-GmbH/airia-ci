#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Does this pull request change anything outside the paths a lane has audited as safe to skip?
# (airia-ci#170.) Reads the changed-file list from the GitHub API, so the lane needs no checkout.
#
# Env: EVENT (github.event_name), REPO (owner/name), PR_NUMBER, SKIP_PATHS (git pathspecs, one per
# line or space-separated), MODIFIED_SKIP_PATHS and RECORDS_PATHS (same form, both optional), GH_TOKEN.
# Writes `code=true|false` and `scope=none|records|all` to $GITHUB_OUTPUT and a one-line summary to
# $GITHUB_STEP_SUMMARY.
#
# scope `none` (code `false`): EVERY changed path (both sides of a rename) matches SKIP_PATHS, or matches
# MODIFIED_SKIP_PATHS and is a file whose status is `modified` (an added, removed or renamed file there is
# code). scope `records` (code `true`): every path is of that kind or matches RECORDS_PATHS, the record
# paths the lane's tests DO read, and at least one is a records path: the lane runs only its records tests.
# scope `all` (code `true`): anything else. Anything unclear is `all`: not a pull_request event, an empty
# list, 3000 files (the API's cap), a failed or truncated read, all three lists empty, a pathspec git
# refuses, a path that cannot be told apart.
#
# The matching is git's own pathspec matching (the dialect reusable-python-gate's `docs-paths` uses):
# the changed paths are put into a scratch index, with no file behind them, and `git ls-files` says
# which match. A pattern is taken as git reads it: `*` crosses `/` unless it starts `:(glob)`.
set -uo pipefail

LIMIT=3000

verdict() { # verdict <code> <scope> <reason>
  echo "code=$1" >> "$GITHUB_OUTPUT"
  echo "scope=$2" >> "$GITHUB_OUTPUT"
  echo "changes: code=$1 scope=$2 ($3)" >> "$GITHUB_STEP_SUMMARY"
  echo "code=$1 scope=$2: $3"
  exit 0
}
fail_open() { verdict true all "$1; every gate runs"; }

[ "${EVENT:-}" = pull_request ] || fail_open "not a pull request (${EVENT:-no event})"
read -ra patterns <<< "$(tr '\n' ' ' <<< "${SKIP_PATHS:-}")"
read -ra modpatterns <<< "$(tr '\n' ' ' <<< "${MODIFIED_SKIP_PATHS:-}")"
read -ra recpatterns <<< "$(tr '\n' ' ' <<< "${RECORDS_PATHS:-}")"
[ "$(( ${#patterns[@]} + ${#modpatterns[@]} + ${#recpatterns[@]} ))" -gt 0 ] || fail_open "skip-paths is empty"
[[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]] || fail_open "no pull request number"

# One JSON line per changed file: [filename, previous_filename|null, status].
files="$(gh api --paginate "repos/$REPO/pulls/$PR_NUMBER/files" --jq '.[] | [.filename, .previous_filename, .status] | @json')" \
  || fail_open "the changed-file list could not be read"
count="$(grep -c . <<< "$files")"
[ "$count" -gt 0 ] || fail_open "the pull request lists no changed file"
[ "$count" -lt "$LIMIT" ] || fail_open "$count changed files: the API stops at $LIMIT, so the list may be cut"
names="$(jq -r '.[0], .[1] | select(. != null)' <<< "$files")"
# The files whose status is `modified` (a renamed file is `renamed`, never `modified`).
modified="$(jq -r 'select(.[2] == "modified") | .[0]' <<< "$files")" || fail_open "the changed-file list is not readable"
case "$names" in *$'\r'*) fail_open "a path holds a carriage return" ;; esac
[ "$(jq -s '[.[][0:2][] | select(. != null and test("\n"))] | length' <<< "$files")" = 0 ] || fail_open "a path holds a newline"

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
cd "$scratch" || fail_open "no scratch directory"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
git init -q . || fail_open "git cannot make a scratch index"
paths="$(sort -u <<< "$names")"
total="$(grep -c . <<< "$paths")"
empty=e69de29bb2d1d6434b8b29ae775ad8c2e48c5391
while IFS= read -r p; do
  printf '100644 %s\t%s\0' "$empty" "$p"
done <<< "$paths" | git update-index --add --replace --info-only -z --index-info || fail_open "the paths could not be indexed"
git ls-files -z > "$scratch/all" || fail_open "the scratch index cannot be read"
[ "$(tr -cd '\0' < "$scratch/all" | wc -c | tr -d ' ')" -eq "$total" ] || fail_open "two changed paths collide as file and directory"
matching() { # matching <pathspec>...: the changed paths those pathspecs match, one per line, sorted; none given, none matched
  [ "$#" -gt 0 ] || return 0
  git ls-files -z -- "$@" > "$scratch/m" || return 1
  tr '\0' '\n' < "$scratch/m" | sort -u
}
skipped="$(matching ${patterns[@]+"${patterns[@]}"})" || fail_open "git refused a skip-paths pattern"
modskip="$(matching ${modpatterns[@]+"${modpatterns[@]}"})" || fail_open "git refused a modified-skip-paths pattern"
records="$(matching ${recpatterns[@]+"${recpatterns[@]}"})" || fail_open "git refused a records-paths pattern"
modskip="$(comm -12 <(printf '%s\n' "$modskip" | sed '/^$/d' | sort -u) <(printf '%s\n' "$modified" | sed '/^$/d' | sort -u))"
none="$(printf '%s\n%s\n' "$skipped" "$modskip" | sed '/^$/d' | sort -u)"
covered="$(printf '%s\n%s\n' "$none" "$records" | sed '/^$/d' | sort -u)"
n_none="$(printf '%s\n' "$none" | grep -c . || true)"
n_covered="$(printf '%s\n' "$covered" | grep -c . || true)"

if [ "$n_none" -eq "$total" ]; then
  verdict false none "all $total changed paths match skip-paths: the heavy jobs are skipped"
fi
if [ "$n_covered" -eq "$total" ]; then
  verdict true records "all $total changed paths are skippable or records ($n_none skippable): only the records tests run"
fi
verdict true all "$n_covered of $total changed paths match skip-paths, modified-skip-paths or records-paths; every gate runs"
