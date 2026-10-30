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
instead.
