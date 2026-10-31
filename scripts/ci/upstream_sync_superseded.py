"""Close open sync PRs that a landed sync made redundant (scripts/ci/upstream_sync.py housekeeping).

An open PR main <- sync/upstream-<X> from this repo is superseded once a freshly fetched origin/main
contains every parent of X (X is a merge). This is the gate's supersession policy, not tree equality.
Such a PR is
commented (naming the main sha that holds the parents), closed, and its branch deleted only if it
still points at X. Anything else (forks, other branches, single-parent heads, a parent main lacks,
a moved branch) stays open. Every fault is printed and skipped: this never changes the gate's verdict.
"""
from __future__ import annotations

import re

SHA = re.compile(r"[0-9a-f]{40}")


def _open_sync_prs(gh_repo, request, prefix) -> list[dict]:
    pages = request(f"repos/{gh_repo}/pulls?state=open&base=main&per_page=100")
    return [pr for page in pages for pr in page if isinstance(pr, dict)
            and (pr.get("base") or {}).get("ref") == "main"
            and str((pr.get("head") or {}).get("ref") or "").startswith(prefix)]


def _kept_because(repo, pr, gh_repo, main, run, prefix) -> tuple[str, list[str]]:
    """("", parents) when ``pr`` is superseded, else (why it stays open, [])."""
    head = pr.get("head") or {}
    sha, ref = str(head.get("sha") or ""), str(head.get("ref") or "")
    if str((head.get("repo") or {}).get("full_name") or "").lower() != gh_repo.lower():
        return "its head is not on this repository", []
    if not SHA.fullmatch(sha) or ref != prefix + sha:
        return f"branch {ref} does not name its head {sha[:10]}", []
    listed = run(repo, "ls-remote", "origin", f"refs/heads/{ref}").stdout.split()
    remote = listed[0] if listed[1:2] == [f"refs/heads/{ref}"] else ""
    if remote != sha:
        return f"{ref} on origin is {remote[:10] or 'gone'}, not the PR head {sha[:10]}", []
    if run(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode:
        run(repo, "fetch", "-q", "origin", f"refs/heads/{ref}")
    listed = run(repo, "rev-list", "--parents", "-n1", sha)
    if listed.returncode:
        return f"its head {sha[:10]} could not be fetched", []
    parents = listed.stdout.split()[1:]
    if len(parents) < 2:
        return f"its head {sha[:10]} is not a merge", []
    missing = [p for p in parents if run(repo, "merge-base", "--is-ancestor", p, main).returncode]
    if missing:
        return f"main {main[:10]} lacks parent {missing[0][:10]}", []
    return "", parents


def _close(repo, pr, parents, gh_repo, main, gh, run, env) -> bool:
    number, sha, ref = str(pr["number"]), pr["head"]["sha"], pr["head"]["ref"]
    body = (f"Superseded: main {main} already contains every parent of this sync's head {sha} "
            f"({', '.join(parents)}). Closed by scripts/ci/upstream_sync.py under the all-parent supersession policy.")
    for verb, extra in (("comment", ("--body", body)), ("close", ())):
        done = gh("pr", verb, number, "-R", gh_repo, *extra, reason=f"upstream sync gate: {verb} a superseded sync PR")
        if done.returncode:
            message = ((done.stderr or "") + (done.stdout or "")).strip()[-300:]
            print(f"  superseded sync PR #{number}: gh pr {verb} failed, left open: {message}")
            return False
    # The lease refuses the deletion if anyone moved the branch off the head checked above.
    deleted = run(repo, "push", "-q", f"--force-with-lease=refs/heads/{ref}:{sha}", "origin", f":refs/heads/{ref}",
                  env=env)
    note = "branch deleted" if deleted.returncode == 0 else \
        f"branch kept: {(deleted.stderr or deleted.stdout).strip()[-200:]}"
    print(f"CLOSED superseded sync PR #{number} ({ref}): main {main} contains its parents; {note}")
    return True


def close_superseded(repo, gh_repo, request, gh, *, run, env, prefix) -> None:
    try:
        pulls = _open_sync_prs(gh_repo, request, prefix)
    except Exception as exc:  # health: allow BLE001 -- housekeeping only: any fault is printed, the gate goes on
        print(f"  superseded sync PRs: could not list open PRs: {exc}")
        return
    if run(repo, "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main").returncode:
        print("  superseded sync PRs: could not fetch origin/main; none checked")
        return
    main = run(repo, "rev-parse", "refs/remotes/origin/main").stdout.strip()
    closed = 0
    failed = 0
    for pr in pulls:
        try:
            why, parents = _kept_because(repo, pr, gh_repo, main, run, prefix)
            if why:
                print(f"  sync PR #{pr.get('number')} stays open: {why}")
                continue
            if _close(repo, pr, parents, gh_repo, main, gh, run, env):
                closed += 1
            else:
                failed += 1
        except Exception as exc:  # health: allow BLE001 -- one PR's fault never stops the others or the gate
            failed += 1
            print(f"  superseded sync PR #{pr.get('number')}: check failed, left open: {exc!r}")
    if failed:
        print(f"superseded sync PRs: cleanup incomplete ({closed} closed, {failed} failed)")
    elif not closed:
        print(f"superseded sync PRs: none superseded ({len(pulls)} open sync PR(s) checked against main {main[:10]})")
