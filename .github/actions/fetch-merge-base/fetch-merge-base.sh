#!/usr/bin/env bash
# Fetch the full commit graph of HEAD and one base revision, commits only, and print their
# merge base (empty when the histories are unrelated). See action.yml for why.
#
#   fetch-merge-base.sh <base> [<head>]
#     base  a branch name (fetched to refs/remotes/origin/<base>) or a 40-hex commit
#     head  a revision already in the repo, or a 40-hex commit to fetch; default HEAD
#
# FETCH_TOKEN (optional) authenticates https://github.com remotes when the checkout did not
# persist credentials. If the commits-only (--filter=tree:0) fetch fails, it is retried once as a
# plain full-history fetch and the partial-clone config is put back as it was (see "Fallback"
# below). Exit status is non-zero only when that fallback fetch fails too, or when the merge
# base's tree still cannot be read after it.
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
#
# Fallback: git 2.55.0 on the Blacksmith runners intermittently dies in that filtered fetch
# ("BUG: builtin/pack-objects.c:5004: should_include_obj should only be called on existing
# objects" ... "fatal: index-pack failed", exit 128). A fetch from a promisor remote runs
# index-pack in promisor mode, which "repacks local links" (local objects the new promisor pack
# references), and pack-objects aborts there. It hit the second call of a job (repo-guards' "Fetch
# PR merge base"), on a checkout the first call had already unshallowed into a partial clone.
# When the filtered fetch fails for any reason, the script prints one
# "filtered fetch failed (rc=N): <last stderr line>; full-history fallback" line, removes the
# partial-clone registration (remote.origin.promisor, remote.origin.partialclonefilter,
# extensions.partialclone) so index-pack does not run in promisor mode, and fetches the same refs
# once without --filter. It then restores those keys to their values from before the attempt: in
# a repo that already was a partial clone the base history's trees exist only on the promisor
# remote, and later diffs must still lazy-fetch them. A fresh checkout ends with none of the keys.
# After a fallback the merge base's tree must read (lazily if need be), or the step fails: a merge
# base whose trees cannot be read makes a later three-dot diff fail with no output. Only a failure
# of the fallback fetch or of that check fails the step.
shallow_args() {
  if [[ $(git rev-parse --is-shallow-repository) == true ]]; then
    printf '%s\n' --unshallow
  fi
}
fetch_args=(--quiet --no-tags --no-recurse-submodules)
mapfile -t depth_args < <(shallow_args)
start=$SECONDS
err=$(mktemp)
trap 'rm -f "$err"' EXIT
partial_keys=(remote.origin.promisor remote.origin.partialclonefilter extensions.partialclone)
declare -A saved=()
for key in "${partial_keys[@]}"; do
  if value=$(git config --get "$key"); then
    saved[$key]=$value
  fi
done
rc=0
git "${git_auth[@]}" fetch "${fetch_args[@]}" --filter=tree:0 \
  "${depth_args[@]}" origin "$head_sha" "$base_spec" 2>"$err" || rc=$?
cat "$err" >&2
fell_back=0
if (( rc != 0 )); then
  fell_back=1
  last=$(grep -v '^[[:space:]]*$' "$err" | tail -n 1 || true)
  echo "fetch-merge-base: filtered fetch failed (rc=${rc}): ${last:-<no stderr>}; full-history fallback" >&2
  for key in "${partial_keys[@]}"; do
    git config --unset-all "$key" 2>/dev/null || true
  done
  mapfile -t depth_args < <(shallow_args)
  git "${git_auth[@]}" fetch "${fetch_args[@]}" "${depth_args[@]}" origin "$head_sha" "$base_spec"
  for key in "${partial_keys[@]}"; do
    if [[ -v saved[$key] ]]; then
      git config "$key" "${saved[$key]}"
    fi
  done
fi
echo "fetch-merge-base: fetched ${base_rev} and ${head_sha} history in $((SECONDS - start))s" >&2

# Exit 1 with no output is "no common ancestor"; anything else (a missing object) is an error,
# never a silent "unrelated".
rc=0
merge_base=$(git merge-base "$base_rev" "$head_sha") || rc=$?
if (( rc > 1 )); then
  exit "$rc"
fi
if (( fell_back )) && [[ -n $merge_base ]] \
   && ! git "${git_auth[@]}" ls-tree "$merge_base" >/dev/null; then
  echo "fetch-merge-base: after the fallback the tree of merge base ${merge_base} cannot be read" >&2
  exit 1
fi
if [[ -n $merge_base ]]; then
  printf '%s\n' "$merge_base"
fi
