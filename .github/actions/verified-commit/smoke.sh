#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
#
# Post-setup proof that the AIRIA commit App makes Verified commits:
#
#   bash smoke.sh <client-id> <private-key.pem> <owner/repo>
#
# Run once, right after the App is installed on <owner/repo>, from any machine
# with gh (read access to the repo), openssl, curl and python3. It mints a
# contents-only installation token from the App's key, commits one file to a
# new branch `verified-commit-smoke/<UTC time>` through verified_commit.py, and
# exits 0 only if GitHub reports that commit Verified. The branch is left for
# you to inspect and delete (the last line prints the command).
#
# SMOKE_JWT_ONLY=1 prints the App JWT and stops: no network, for testing.
set -euo pipefail

client_id="$1"; key="$2"; repo="${3:-}"
here="$(cd "$(dirname "$0")" && pwd)"
api="${GITHUB_API_URL:-https://api.github.com}"

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
now="$(date +%s)"
header="$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)"
payload="$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - 60))" "$((now + 540))" "$client_id" | b64url)"
signature="$(printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "$key" -binary | b64url)"
jwt="$header.$payload.$signature"
if [ "${SMOKE_JWT_ONLY:-}" = "1" ]; then echo "$jwt"; exit 0; fi

call() { curl -fsS -H "Authorization: Bearer $1" -H "Accept: application/vnd.github+json" "${@:2}"; }
installation="$(call "$jwt" "$api/repos/$repo/installation" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
token="$(call "$jwt" -X POST "$api/app/installations/$installation/access_tokens" \
  -d "{\"repositories\":[\"${repo#*/}\"],\"permissions\":{\"contents\":\"write\"}}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"

work="$(mktemp -d)"
trap 'call "$token" -X DELETE "$api/installation/token" >/dev/null || true; rm -rf "$work"' EXIT
gh repo clone "$repo" "$work/repo" -- --depth 1 --quiet
branch="verified-commit-smoke/$(date -u +%Y%m%dT%H%M%SZ)"
date -u +%FT%TZ > "$work/repo/.verified-commit-smoke"
GH_TOKEN="$token" python3 "$here/verified_commit.py" --repo "$repo" --branch "$branch" \
  --cwd "$work/repo" --paths .verified-commit-smoke --message "chore: verified-commit smoke test"
echo "PASS. Clean up with: gh api -X DELETE repos/$repo/git/refs/heads/$branch"
