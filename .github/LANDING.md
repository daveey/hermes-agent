# Landing commits on the fork's `main`

`main` on daveey/hermes requires one status check, **All required checks pass (v2)**
(the `all-checks-pass` job in `workflows/ci.yaml`), and branch protection has
`enforce_admins` on. A direct `git push … :main` of a commit without that check
green is refused with `GH006: Protected branch update failed`, admins included.

So every change lands through CI on its exact sha:

1. Push a topic branch and open a PR (`gh pr create --fill`).
2. Wait until the check is `success` on the PR's head sha. Queued, in progress,
   missing or cancelled does not count.
3. Land that sha: fast-forward `main` to it when `main` has not moved, else
   `gh pr merge <n> --rebase --match-head-commit <sha>`. Never `--admin`.

`hermes-core-wt ship <slug>` does all three after its local lint, affected-test
and pre-push gates. Upstream syncs go through `scripts/ci/upstream_sync.py`
instead. A purely mechanical sync (every merge has an empty
`git show --remerge-diff` and nothing else sits on top of upstream) lands on
green CI alone. A sync that carries fork-authored content (a conflict
resolution or fork edit in a merge, or a `[daveey]` fixup commit) is code the
fork wrote: the gate parks it as NOT YET until an independent exact-head review
approves it, through the same review card and `review_gate` that
`hermes-core-wt ship` uses. `upstream_sync.py --classify <sha>` prints which
kind a head is. Only commits of `upstream/main` re-fetched from
`github.com/NousResearch/hermes-agent` count as upstream's own: an `upstream`
remote configured with any other URL is REFUSED, never classified. The card
that authored a fork-authored sync is passed as `--impl-task <card>` (its
sessions cannot be its reviewer); there is no environment variable for it.
