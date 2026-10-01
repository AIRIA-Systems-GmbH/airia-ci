#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# The verified-commit action's body (action.yml says why it exists). Inputs
# come from the environment:
#   MESSAGE  commit message (required)     REPO     owner/name (required)
#   BRANCH   remote branch (default: the checked-out one)
#   PATHS    pathspecs, one per line (default: everything, as `git add -A`)
#   DRY_RUN  `true`: package and print only  PAYLOAD  where the request goes
#   GH_TOKEN the token the API call and verification check use
set -euo pipefail

die() { echo "::error title=verified-commit::$*" >&2; exit 1; }
output() { echo "$1"; if [ -n "${GITHUB_OUTPUT:-}" ]; then echo "$1" >> "$GITHUB_OUTPUT"; fi; }

: "${MESSAGE:?MESSAGE is required}" "${REPO:?REPO is required}"
DRY_RUN="${DRY_RUN:-false}"
PAYLOAD="${PAYLOAD:-${RUNNER_TEMP:-/tmp}/verified-commit-payload.json}"
cd "$(git rev-parse --show-toplevel)"

# ── which branch, and is local exactly the remote head ────────────────────
current="$(git symbolic-ref -q --short HEAD || true)"
branch="${BRANCH:-$current}"
[ -n "$branch" ] || die "HEAD is detached: pass the branch to commit to"
[ -z "$current" ] || [ "$current" = "$branch" ] \
  || die "the checkout is on $current, not $branch: check out $branch first (only the checked-out ref is fast-forwarded)"
head="$(git rev-parse HEAD)"
remote_head="$(git ls-remote origin "refs/heads/$branch" | cut -f1)"
[ -n "$remote_head" ] \
  || die "$branch does not exist on origin: createCommitOnBranch commits only to an existing branch, so create it first"
[ "$head" = "$remote_head" ] \
  || die "local HEAD $head is not origin/$branch ($remote_head): push or rebase first. This action commits the working tree on top of the exact remote head and never forces"

# ── stage the change in a throwaway index; the caller's index is untouched ─
specs=()
while IFS= read -r line; do
  line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
  if [ -n "$line" ]; then specs+=("$line"); fi
done <<< "${PATHS:-}"
if [ "${#specs[@]}" -eq 0 ]; then specs=(.); fi  # from the top level: everything
idx="$(mktemp)"; trap 'rm -f "$idx" "$idx.raw"' EXIT
GIT_INDEX_FILE="$idx" git read-tree HEAD
GIT_INDEX_FILE="$idx" git add -A -- "${specs[@]}"

# ── package it as createCommitOnBranch, refusing what that cannot express ──
GIT_INDEX_FILE="$idx" git diff --cached --raw -z --no-renames HEAD > "$idx.raw"
if python3 - "$idx.raw" "$PAYLOAD" "$REPO" "$branch" "$head" <<'PY'
import base64, json, os, subprocess, sys

raw, payload, repo, branch, head = sys.argv[1:6]
fields = open(raw, "rb").read().split(b"\0")
additions, deletions, refused = [], [], []
for meta, path in zip(fields[0::2], fields[1::2]):
    if not meta:
        continue
    old_mode, new_mode, _old, new_sha, status = meta[1:].decode().split(" ")
    name = path.decode()
    if status == "D":
        if old_mode == "160000":
            refused.append(f"{name}: removes a submodule")
        deletions.append({"path": name})
    elif status in ("A", "M") and new_mode == "100644" and old_mode in ("000000", "100644"):
        blob = subprocess.run(["git", "cat-file", "blob", new_sha], check=True, capture_output=True).stdout
        additions.append({"path": name, "contents": base64.b64encode(blob).decode()})
    else:
        kinds = {"120000": "a symlink", "160000": "a submodule", "100755": "an executable file"}
        what = kinds.get(new_mode if status == "A" else old_mode, f"mode {old_mode}->{new_mode}")
        if status == "M" and old_mode != new_mode:
            what = f"a mode change {old_mode}->{new_mode}"
        refused.append(f"{name}: {status} on {what}")
if refused:
    sys.exit("createCommitOnBranch sends file contents only (every file lands as 100644), "
             "so this change cannot be committed faithfully:\n  " + "\n  ".join(refused))

headline, _, body = os.environ["MESSAGE"].strip().partition("\n")
message = {"headline": headline.strip()}
if body.strip():
    message["body"] = body.strip()
request = {
    "query": "mutation($input: CreateCommitOnBranchInput!) {"
             " createCommitOnBranch(input: $input) { commit { oid url } } }",
    "variables": {"input": {
        "branch": {"repositoryNameWithOwner": repo, "branchName": branch},
        "expectedHeadOid": head,
        "message": message,
        "fileChanges": {"additions": additions, "deletions": deletions},
    }},
}
with open(payload, "w") as fh:
    json.dump(request, fh)
print(f"{repo}@{branch} on {head}: {len(additions)} addition(s), {len(deletions)} deletion(s)")
for a in additions:
    print(f"  + {a['path']} ({len(base64.b64decode(a['contents']))} bytes)")
for d in deletions:
    print(f"  - {d['path']}")
PY
then :; else die "the change could not be packaged (see above); nothing was committed"; fi

changes="$(jq '.variables.input.fileChanges | (.additions | length) + (.deletions | length)' "$PAYLOAD")"
if [ "$changes" -eq 0 ]; then
  echo "nothing to commit"
  output committed=false
  exit 0
fi
if [ "$DRY_RUN" = true ]; then
  echo "dry run: nothing sent; the request is in $PAYLOAD"
  output committed=false
  exit 0
fi

# ── commit; GitHub signs it ───────────────────────────────────────────────
[ -n "${GH_TOKEN:-}" ] || die "no token: the App token was not minted"
sha="$(gh api graphql --input "$PAYLOAD" --jq .data.createCommitOnBranch.commit.oid)" \
  || die "createCommitOnBranch failed (the branch may have moved since $head); nothing was committed"
[ -n "$sha" ] || die "createCommitOnBranch returned no commit"
output "sha=$sha"
output committed=true
echo "committed $sha to $REPO@$branch"

# ── fast-forward the local ref to it, never anything else ─────────────────
git fetch -q origin "refs/heads/$branch"
fetched="$(git rev-parse FETCH_HEAD)"
[ "$fetched" = "$sha" ] \
  || die "origin/$branch is at $fetched, not the new commit $sha: it moved again after the commit. The local ref is left at $head"
git merge-base --is-ancestor "$head" "$sha" || die "$sha does not descend from $head; refusing to move the local ref"
git reset -q --mixed "$sha"

# ── parity: what GitHub committed is exactly what is on disk ──────────────
drift="$(git status --porcelain --untracked-files=all -- "${specs[@]}")"
[ -z "$drift" ] || die "the commit $sha differs from the working tree:
$drift"

verified="$(gh api "repos/$REPO/commits/$sha" --jq .commit.verification.verified)"
output "verified=$verified"
[ "$verified" = true ] \
  || die "GitHub does not report $sha as verified ($(gh api "repos/$REPO/commits/$sha" --jq .commit.verification.reason))"
