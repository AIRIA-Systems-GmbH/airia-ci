#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Does this pull request change anything outside the paths a lane has audited as safe to skip?
# (airia-ci#170.) Reads the changed-file list from the GitHub API, so the lane needs no checkout.
#
# Env: EVENT (github.event_name), REPO (owner/name), PR_NUMBER, SKIP_PATHS (git pathspecs, one per
# line or space-separated), GH_TOKEN. Writes `code=true|false` to $GITHUB_OUTPUT and a one-line
# summary to $GITHUB_STEP_SUMMARY.
#
# `false` only when EVERY changed path (both sides of a rename) matches SKIP_PATHS. Anything unclear is
# `true`: not a pull_request event, an empty list, 3000 files (the API's cap), a failed or truncated
# read, an empty SKIP_PATHS, a pathspec git refuses, a path that cannot be told apart.
#
# The matching is git's own pathspec matching (the dialect reusable-python-gate's `docs-paths` uses):
# the changed paths are put into a scratch index, with no file behind them, and `git ls-files` says
# which match. A pattern is taken as git reads it: `*` crosses `/` unless it starts `:(glob)`.
set -uo pipefail

LIMIT=3000

verdict() { # verdict <code> <reason>
  echo "code=$1" >> "$GITHUB_OUTPUT"
  echo "changes: code=$1 ($2)" >> "$GITHUB_STEP_SUMMARY"
  echo "code=$1: $2"
  exit 0
}
fail_open() { verdict true "$1; every gate runs"; }

[ "${EVENT:-}" = pull_request ] || fail_open "not a pull request (${EVENT:-no event})"
read -ra patterns <<< "$(tr '\n' ' ' <<< "${SKIP_PATHS:-}")"
[ "${#patterns[@]}" -gt 0 ] || fail_open "skip-paths is empty"
[[ "${PR_NUMBER:-}" =~ ^[0-9]+$ ]] || fail_open "no pull request number"

# One JSON line per changed file: [filename, previous_filename|null].
files="$(gh api --paginate "repos/$REPO/pulls/$PR_NUMBER/files" --jq '.[] | [.filename, .previous_filename] | @json')" \
  || fail_open "the changed-file list could not be read"
count="$(grep -c . <<< "$files")"
[ "$count" -gt 0 ] || fail_open "the pull request lists no changed file"
[ "$count" -lt "$LIMIT" ] || fail_open "$count changed files: the API stops at $LIMIT, so the list may be cut"
names="$(jq -r '.[] | select(. != null)' <<< "$files")" || fail_open "the changed-file list is not readable"
case "$names" in *$'\r'*) fail_open "a path holds a carriage return" ;; esac
[ "$(jq -s '[.[][] | select(. != null and test("\n"))] | length' <<< "$files")" = 0 ] || fail_open "a path holds a newline"

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
git ls-files -z -- "${patterns[@]}" > "$scratch/matched" || fail_open "git refused a skip-paths pattern"
matched="$(tr -cd '\0' < "$scratch/matched" | wc -c | tr -d ' ')"

if [ "$matched" -eq "$total" ]; then
  verdict false "all $total changed paths match skip-paths: the heavy jobs are skipped"
fi
verdict true "$matched of $total changed paths match skip-paths; every gate runs"
