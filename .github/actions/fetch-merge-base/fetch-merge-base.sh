#!/usr/bin/env bash
# Fetch the full commit graph of HEAD and one base revision, commits only, and print their
# merge base (empty when the histories are unrelated). See action.yml for why.
#
#   fetch-merge-base.sh <base> [<head>]
#     base  a branch name (fetched to refs/remotes/origin/<base>) or a 40-hex commit
#     head  a revision already in the repo, or a 40-hex commit to fetch; default HEAD
#
# FETCH_TOKEN (optional) authenticates https://github.com remotes when the checkout did not
# persist credentials. Exit status is non-zero only when the fetch itself fails.
set -euo pipefail

base=${1:?usage: fetch-merge-base.sh <base> [<head>]}
head=${2:-HEAD}

is_sha() { [[ $1 =~ ^[0-9a-f]{40}$ ]]; }

if is_sha "$head"; then
  head_sha=$head
else
  head_sha=$(git rev-parse --verify "${head}^{commit}")
fi
if is_sha "$base"; then
  base_spec=$base
  base_rev=$base
else
  base_spec="+refs/heads/${base}:refs/remotes/origin/${base}"
  base_rev="refs/remotes/origin/${base}"
fi

git_auth=()
url=$(git remote get-url origin)
if [[ -n ${FETCH_TOKEN:-} && $url == https://github.com/* ]] \
   && ! git config --get-regexp '^http\..*\.extraheader$' >/dev/null 2>&1; then
  basic=$(printf 'x-access-token:%s' "$FETCH_TOKEN" | base64 | tr -d '\n')
  git_auth=(-c "http.https://github.com/.extraheader=AUTHORIZATION: basic ${basic}")
fi

# tree:0 = commit objects only: the whole history of both sides is tens of MB, where the
# full-history checkout it replaces pulled every head, tag, tree and blob of the repo. The repo
# becomes a partial clone, so a later diff/archive/worktree of the merge base lazily fetches that
# commit's trees and blobs (credentials must reach those commands too).
depth_args=()
if [[ $(git rev-parse --is-shallow-repository) == true ]]; then
  depth_args=(--unshallow)
fi
start=$SECONDS
git "${git_auth[@]}" fetch --quiet --no-tags --no-recurse-submodules --filter=tree:0 \
  "${depth_args[@]}" origin "$head_sha" "$base_spec"
echo "fetch-merge-base: fetched ${base_rev} and ${head_sha} history in $((SECONDS - start))s" >&2

# Exit 1 with no output is "no common ancestor"; anything else (a missing object) is an error,
# never a silent "unrelated".
rc=0
git merge-base "$base_rev" "$head_sha" || rc=$?
if (( rc > 1 )); then
  exit "$rc"
fi
