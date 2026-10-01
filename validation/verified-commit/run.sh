#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Exercises the SHIPPED verified-commit script (.github/actions/verified-commit/
# commit.sh) against a local bare "remote" and a fake GitHub (fake-gh) that
# applies createCommitOnBranch with git plumbing and enforces expectedHeadOid.
# Proves the packaging, the refusals, the fast-forward, never-force and the
# parity check. It cannot prove the signature: the self-test workflow does.
#
#   bash validation/verified-commit/run.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
script="$(cd "$here/../.." && pwd)/.github/actions/verified-commit/commit.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin"; cp "$here/fake-gh" "$T/bin/gh"; chmod +x "$T/bin/gh"
export PATH="$T/bin:$PATH" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
fail=0
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $what"; else echo "FAIL  $what"; fail=1; fi; }

fresh() { # a bare remote with one commit on main, and a clone of it at $W
  rm -rf "$T/remote.git" "$T/work" "$T/other"
  git init -q --bare -b main "$T/remote.git"
  git clone -q "$T/remote.git" "$T/work" 2>/dev/null
  cd "$T/work"
  printf 'one\n' > a.txt; mkdir dir; printf 'b\n' > dir/b.txt; printf 'bye\n' > gone.txt
  printf '#!/bin/sh\n' > exec.sh; chmod +x exec.sh; printf 'ignored.log\n' > .gitignore
  git add -A && git commit -q -m init && git push -q origin main
  export FAKE_REMOTE="$T/remote.git" W="$T/work"
}
run() { # run <extra env...> -- commit.sh in $W; output in $T/out, outputs in $T/gho
  : > "$T/gho"
  ( cd "$W" && env REPO=o/r MESSAGE=$'bump\n\nbody line' GH_TOKEN=fake PAYLOAD="$T/payload.json" \
      GITHUB_OUTPUT="$T/gho" "$@" bash "$script" ) > "$T/out" 2>&1 || echo "exit=$?" >> "$T/out"
}
remote_head() { git --git-dir="$T/remote.git" rev-parse refs/heads/main; }
payload_paths() { jq -r ".variables.input.fileChanges.$1[].path" "$T/payload.json" | sort | tr '\n' ' '; }

# ── dry run: what is packaged, and what is left alone ─────────────────────
fresh; base="$(remote_head)"
printf 'two\n' > a.txt; head -c 4096 /dev/urandom > dir/new.bin; rm gone.txt
mkdir -p c/d; printf 'deep\n' > c/d/e.txt; printf 'noise\n' > ignored.log
git add a.txt; staged_before="$(git diff --cached --name-only)"
run DRY_RUN=true
cat "$T/out"
check "dry run exits 0" bash -c "grep -q 'dry run: nothing sent' '$T/out' && ! grep -q ^exit= '$T/out'"
check "additions = modified + new binary + new nested untracked" test "$(payload_paths additions)" = "a.txt c/d/e.txt dir/new.bin "
check "deletions = the removed file" test "$(payload_paths deletions)" = "gone.txt "
check ".gitignore'd file is not sent" bash -c "! grep -q ignored.log '$T/payload.json'"
check "binary contents round-trip byte-for-byte" bash -c \
  "jq -r '.variables.input.fileChanges.additions[] | select(.path==\"dir/new.bin\") | .contents' '$T/payload.json' | base64 -d | cmp - '$W/dir/new.bin'"
check "expectedHeadOid = the remote head" test "$(jq -r .variables.input.expectedHeadOid "$T/payload.json")" = "$base"
check "headline and body split" test "$(jq -r '.variables.input.message | .headline + "|" + .body' "$T/payload.json")" = "bump|body line"
check "the caller's index is untouched" test "$(git diff --cached --name-only)" = "$staged_before"
check "dry run sent nothing (remote unchanged)" test "$(remote_head)" = "$base"
check "dry run reports committed=false" grep -qx committed=false "$T/gho"

run DRY_RUN=true PATHS=$'dir\n'
check "paths: only dir/ is packaged" test "$(payload_paths additions)" = "dir/new.bin "

# ── refusals: before any API call ─────────────────────────────────────────
fresh; run; check "empty change: committed=false, exit 0" grep -qx committed=false "$T/gho"
fresh; ln -s a.txt link; run DRY_RUN=true
check "refuses a new symlink" grep -q "link: A on a symlink" "$T/out"
fresh; printf 'x\n' > new.sh; chmod +x new.sh; run DRY_RUN=true
check "refuses a new executable file" grep -q "new.sh: A on an executable file" "$T/out"
fresh; chmod +x a.txt; run DRY_RUN=true
check "refuses a mode change" grep -q "a.txt: M on a mode change 100644->100755" "$T/out"
fresh; printf 'echo\n' >> exec.sh; run DRY_RUN=true
check "refuses editing an executable (it would land as 100644)" grep -q "exec.sh: M on an executable file" "$T/out"
fresh; printf 'x\n' > a.txt; git commit -qam local; printf 'y\n' > a.txt; run DRY_RUN=true
check "refuses unpushed local commits" grep -q "is not origin/main" "$T/out"
fresh; git clone -q "$T/remote.git" "$T/other" && (cd "$T/other" && git commit -q --allow-empty -m moved && git push -q origin main)
printf 'x\n' > a.txt; run DRY_RUN=true
check "refuses when the remote moved" grep -q "is not origin/main" "$T/out"
fresh; git checkout -q --detach; printf 'x\n' > a.txt; run DRY_RUN=true
check "refuses a detached HEAD without a branch" grep -q "HEAD is detached" "$T/out"
fresh; git checkout -q -b feature; printf 'x\n' > a.txt; run DRY_RUN=true
check "refuses a branch missing on origin" grep -q "does not exist on origin" "$T/out"
fresh; git checkout -q -b feature; git push -q origin feature; printf 'x\n' > a.txt; run DRY_RUN=true BRANCH=main
check "refuses committing to a branch other than the checked-out one" grep -q "the checkout is on feature, not main" "$T/out"

# ── a real commit through the fake server ─────────────────────────────────
fresh; base="$(remote_head)"
printf 'two\n' > a.txt; head -c 2048 /dev/urandom > dir/new.bin; rm gone.txt; printf 'noise\n' > ignored.log
run; cat "$T/out"
sha="$(sed -n 's/^sha=//p' "$T/gho")"
check "committed=true and a sha" test -n "$sha"
check "the remote branch is at the new commit" test "$(remote_head)" = "$sha"
check "the new commit's parent is the old head (no force)" test "$(git --git-dir="$T/remote.git" rev-parse "$sha^")" = "$base"
check "the local branch fast-forwarded to it" test "$(git rev-parse main)" = "$sha"
check "still on the branch, not detached" test "$(git symbolic-ref --short HEAD)" = main
check "parity: working tree == committed tree" test -z "$(git status --porcelain --untracked-files=all)"
check "ignored file still on disk, not committed" bash -c "test -f ignored.log && ! git -C '$W' cat-file -e '$sha:ignored.log'"
check "verified=true reported (FAKE server's canned answer)" grep -qx verified=true "$T/gho"

# a PR checkout: detached HEAD, branch given
fresh; git checkout -q --detach; printf 'pr\n' > a.txt
run BRANCH=main
check "detached + branch: commits, HEAD moves to the new commit" test "$(git rev-parse HEAD)" = "$(remote_head)"

# someone pushes right after our commit: the local ref must not follow
fresh; base="$(remote_head)"; printf 'race\n' > a.txt
run FAKE_RACE=1
check "race: the action fails loud (non-zero)" bash -c "grep -q 'moved again after the commit' '$T/out' && grep -q ^exit=1 '$T/out'"
check "race: the local ref is left where it was" test "$(git rev-parse main)" = "$base"
check "race: the working tree is intact" test "$(cat a.txt)" = race

# a server commit that is not the local change: parity must catch it
fresh; printf 'two\n' > a.txt; printf 'new\n' > z.txt
run FAKE_DROP=1
check "parity: a commit missing a file fails loud" bash -c "grep -q 'differs from the working tree' '$T/out' && grep -q '?? z.txt' '$T/out' && grep -q ^exit=1 '$T/out'"

# the server rejects a stale expectedHeadOid (GitHub's own guard)
fresh; printf 'x\n' > a.txt; run DRY_RUN=true
(cd "$T" && git clone -q "$T/remote.git" other2 && cd other2 && git commit -q --allow-empty -m moved && git push -q origin main)
check "stale expectedHeadOid is rejected by the server" bash -c "! PATH='$PATH' FAKE_REMOTE='$T/remote.git' gh api graphql --input '$T/payload.json'"

exit "$fail"
