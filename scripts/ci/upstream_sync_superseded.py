"""Clean superseded sync PRs and orphan staging refs (upstream_sync.py housekeeping).

An open PR main <- sync/upstream-<X> from this repo is superseded once a freshly fetched origin/main
contains every parent of X (X is a merge). This is the gate's supersession policy, not tree equality.
Such a PR is
commented (naming the main sha that holds the parents), closed, and its branch deleted only if it
still points at X. Anything else (forks, other branches, single-parent heads, a parent main lacks,
a moved branch) stays open. Every fault is printed and skipped: this never changes the gate's verdict.
Orphan sync refs need no open PR (on any base) and all head parents on main. Delete with a lease
on the listed SHA; exclude this invocation's staged/landing candidates and refs handled as PRs.
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


def _delete_orphans(repo, gh_repo, request, main, *, run, env, prefix, protected, excluded):
    # Any open PR, including one targeting a release branch, vetoes orphan cleanup.
    pages = request(f"repos/{gh_repo}/pulls?state=open&per_page=100")
    open_refs = {str((pr.get("head") or {}).get("ref") or "")
                 for page in pages for pr in page if isinstance(pr, dict)}
    open_refs.update(excluded)  # Do not retry a failed PR deletion against a newly moved head.
    open_refs.update(prefix + sha for sha in protected)  # Protect the ref even if its head moved before listing.
    listed = run(repo, "ls-remote", "--heads", "origin", f"refs/heads/{prefix}*")
    if listed.returncode:
        raise RuntimeError(f"could not list origin sync branches: {listed.stderr}")
    deleted = failed = checked = 0
    for line in listed.stdout.splitlines():
        sha, ref = line.split()
        if not SHA.fullmatch(sha) or not ref.startswith(f"refs/heads/{prefix}"):
            continue
        if ref.removeprefix("refs/heads/") in open_refs or sha in protected:
            continue
        checked += 1
        try:
            if run(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode:
                run(repo, "fetch", "-q", "origin", ref)
            ancestry = run(repo, "rev-list", "--parents", "-n1", sha)
            if ancestry.returncode:
                raise RuntimeError(f"could not read listed head {sha}: {ancestry.stderr}")
            parents = ancestry.stdout.split()[1:]
            if not parents or any(run(repo, "merge-base", "--is-ancestor", p, main).returncode for p in parents):
                continue
            result = run(repo, "push", "-q", f"--force-with-lease={ref}:{sha}", "origin", f":{ref}", env=env)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip()[-300:])
            deleted += 1
            print(f"DELETED superseded sync branch {ref} at {sha}: main {main} contains every parent")
        except Exception as exc:  # health: allow BLE001 -- one branch cannot stop housekeeping or change the verdict
            failed += 1
            print(f"  superseded sync branch {ref} at {sha}: branch kept: {exc}")
    print(f"superseded sync branches: {deleted} deleted, {failed} failed ({checked} orphan(s) checked)")


def close_superseded(repo, gh_repo, request, gh, *, run, env, prefix, protected=()) -> None:
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
            if (pr.get("head") or {}).get("sha") in protected:
                continue
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
    _delete_orphans(repo, gh_repo, request, main, run=run, env=env, prefix=prefix, protected=protected,
                    excluded={str((pr.get("head") or {}).get("ref") or "") for pr in pulls})
