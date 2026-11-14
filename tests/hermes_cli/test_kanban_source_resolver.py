"""workspace_source v1 resolver + refusals (plan v3 §6/§6b/§9, v4 S7, Q3; t_01a1d0ec4ab93f32).

Real git: a bare origin, an author clone that publishes the task branch, and two HERMES_HOMEs (``hub`` holds custody,
``worker-2`` materializes) each with its own dedicated registered clone. The GitHub identity layer (URL canonicalizer
and the API id lookup) is the only double: the bare origin is a local path standing in for ``acme/widget``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_clones as kcl
from hermes_cli import kanban_source_resolver as ksr
from hermes_cli import kanban_workspace_source as kws
from tests.hermes_cli import kanban_sync_support as support

REPO_ID = 42
BRANCH = "wt/t_src"
TASK = "t_src"


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(cwd, name, text="x") -> str:
    (Path(cwd) / name).parent.mkdir(parents=True, exist_ok=True)
    (Path(cwd) / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", name)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def make_world(tmp_path, monkeypatch):
    """The git world (shared with the core PR-2 publish tests)."""
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
                 "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n[protocol \"file\"]\n\tallow = always\n")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    author = tmp_path / "author"
    subprocess.run(["git", "clone", "-q", str(origin), str(author)], check=True, capture_output=True)
    base = commit(author, "README")
    git(author, "push", "-q", "origin", "main")
    git(author, "checkout", "-q", "-b", BRANCH)
    tip = commit(author, "work.txt", "published")
    git(author, "push", "-q", "origin", BRANCH)
    homes = {}
    for name in ("hub", "worker-2"):
        home = tmp_path / name
        clone = home / "code" / "_src" / "widget"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
        homes[name] = SimpleNamespace(home=home, clone=clone)
    calls = []

    def lookup(full_name):
        calls.append(full_name)
        return {"id": REPO_ID, "full_name": "acme/widget"}
    monkeypatch.setattr(kcl, "canonical_github", lambda url: "acme/widget" if url.strip() == str(origin) else None)
    monkeypatch.setattr(kcl, "github_repo", lookup)
    return SimpleNamespace(tmp=tmp_path, origin=origin, author=author, base=base, tip=tip, homes=homes,
                           lookup_calls=calls, monkeypatch=monkeypatch, open_conns=[])


def use_home(world, name):
    """Switch the process to ``name``'s HERMES_HOME and kanban DB; returns (conn generator, settings)."""
    h = world.homes[name]
    hermes_home = support.kanban_home(h.home, world.monkeypatch) if not (h.home / ".hermes").exists() else h.home / ".hermes"
    world.monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    h.hermes = hermes_home
    conn_gen = support.synced_conn(hermes_home)
    conn = next(conn_gen)
    world.open_conns.append(conn_gen)  # keep the generator alive: GC would close the connection
    cfg = kws.Settings(enabled=True, requested=True, clones={"acme/widget": str(h.clone)}, publish_on_park=True,
                       push_timeout_s=30, preflight_timeout_s=60, materialize_timeout_s=60)
    real = kws.settings
    world.monkeypatch.setattr(kws, "settings", lambda config=None: cfg if config is None else real(config))
    return conn, cfg


def manifest(world, **over) -> dict:
    m = {"version": 1, "repository": {"id": REPO_ID, "full_name": "acme/widget",
                                      "url": "https://github.com/acme/widget"},
         "branch": f"refs/heads/{BRANCH}", "commit": world.tip, "base_commit": world.base, "published_at": 1,
         "manifest_revision": 1, "published_by": {"run_uid": "r1", "machine": "hub"}, "source_kind": "task_worktree",
         "artifacts": []}
    m.update(over)
    return m


def snap(world, *, holder="hub", state="published", src=True, **mover):
    return kws.SourceSnapshot(manifest_value=json.dumps(manifest(world, **mover)) if src else None,
                              custody=kws.Custody(holder, state, 1))


def task(**over):
    return SimpleNamespace(**{"id": TASK, "workspace_path": None, "branch_name": None, **over})


def resolve(world, conn, cfg, snapshot, me="worker-2", **kw):
    return ksr.resolve_source_workspace(conn, task(**kw), snapshot, me=me, cfg=cfg, lookup=kcl.github_repo)


def refusal(fn) -> kws.WorkspaceSourceRefusal:
    with pytest.raises(kws.WorkspaceSourceRefusal) as err:
        fn()
    return err.value


# --- materialize ----------------------------------------------------------------------------------------------------

def test_materialize_exact_branch_and_sha(world):
    conn, cfg = use_home(world, "worker-2")
    git(world.homes["worker-2"].clone, "update-ref", "-d", f"refs/remotes/origin/{BRANCH}")
    out = resolve(world, conn, cfg, snap(world))
    clone = world.homes["worker-2"].clone
    assert (out.mode, out.branch, out.commit, out.manifest_revision) == ("materialized", BRANCH, world.tip, 1)
    assert out.path == str(clone.resolve().parent / ".worktrees" / str(REPO_ID) / TASK)
    assert git(out.path, "rev-parse", "HEAD") == world.tip
    assert git(out.path, "symbolic-ref", "HEAD") == f"refs/heads/{BRANCH}"
    assert git(clone, "rev-parse", f"refs/hermes/source/{TASK}") == world.tip
    assert subprocess.run(["git", "-C", str(clone), "rev-parse", "--verify", "-q", f"refs/remotes/origin/{BRANCH}"],
                          capture_output=True, check=False).returncode != 0  # S4: tracking refs never written
    payload = out.event_payload(holder="hub", run_uid="r2")
    assert {k: payload[k] for k in ("repository", "branch", "commit", "manifest_revision", "path", "source_kind",
                                    "mode", "holder", "run_uid")} == {
        "repository": "acme/widget", "branch": BRANCH, "commit": world.tip, "manifest_revision": 1, "path": out.path,
        "source_kind": "task_worktree", "mode": "materialized", "holder": "hub", "run_uid": "r2"}
    assert payload["hostname"]


def test_materialized_worktree_cannot_inherit_clone_build_config(world):
    conn, cfg = use_home(world, "worker-2")
    clone = world.homes["worker-2"].clone
    clone_head = commit(clone, "config.nims", 'switch("path", "src")')
    out = resolve(world, conn, cfg, snap(world))
    path = Path(out.path).resolve()
    # Tools such as Nim discover config by walking ancestors, independent of Git's worktree boundary.
    inherited = [parent / "config.nims" for parent in path.parents if (parent / "config.nims").exists()]
    assert inherited == []
    assert not path.is_relative_to(clone.resolve())
    assert git(path, "rev-parse", "HEAD") == world.tip
    assert git(clone, "rev-parse", "HEAD") == clone_head


def test_stale_tracking_ref_is_never_read(world):
    conn, cfg = use_home(world, "worker-2")
    clone = world.homes["worker-2"].clone
    git(clone, "update-ref", f"refs/remotes/origin/{BRANCH}", world.base)  # stale tracking ref
    out = resolve(world, conn, cfg, snap(world))
    assert git(out.path, "rev-parse", "HEAD") == world.tip


def test_return_home_fast_forwards_existing_worktree(world):
    """The destination already has B checked out (it is where the card ran before): clean + behind -> ff."""
    conn, cfg = use_home(world, "worker-2")
    clone = world.homes["worker-2"].clone
    home = world.tmp / "wt-home"
    git(clone, "worktree", "add", "-q", "-b", BRANCH, str(home), world.base)
    out = resolve(world, conn, cfg, snap(world))
    assert (out.mode, out.path) == ("return_home", str(home))
    assert git(home, "rev-parse", "HEAD") == world.tip


def test_local_branch_behind_is_moved_not_forced(world):
    conn, cfg = use_home(world, "worker-2")
    clone = world.homes["worker-2"].clone
    git(clone, "branch", BRANCH, world.base)
    out = resolve(world, conn, cfg, snap(world))
    assert out.mode == "materialized" and git(clone, "rev-parse", BRANCH) == world.tip


# --- holder path (v3 §6, v4 S7) -------------------------------------------------------------------------------------

def test_holder_ahead_of_manifest_reuses_as_is(world):
    conn, cfg = use_home(world, "hub")
    clone = world.homes["hub"].clone
    wt = world.tmp / "hub-wt"
    git(clone, "worktree", "add", "-q", "-b", BRANCH, str(wt), world.tip)
    ahead = commit(wt, "more.txt")
    (wt / "dirt.txt").write_text("its own work")
    out = resolve(world, conn, cfg, snap(world, state="retained"), me="hub")
    assert (out.mode, out.path) == ("holder_reuse", str(wt))
    assert git(wt, "rev-parse", "HEAD") == ahead and (wt / "dirt.txt").exists()


def test_holder_diverged_refused(world):
    conn, cfg = use_home(world, "hub")
    clone = world.homes["hub"].clone
    wt = world.tmp / "hub-wt"
    git(clone, "worktree", "add", "-q", "-b", BRANCH, str(wt), world.base)
    commit(wt, "other.txt")
    err = refusal(lambda: resolve(world, conn, cfg, snap(world), me="hub"))
    assert (err.code, err.scope) == ("diverged_local_branch", "host")


def test_holder_no_manifest_reuses_local_worktree(world):
    conn, cfg = use_home(world, "hub")
    wt = world.tmp / "legacy-wt"
    wt.mkdir()
    out = resolve(world, conn, cfg, snap(world, src=False, state="retained"), me="hub", workspace_path=str(wt),
                  branch_name="wt/legacy")
    assert (out.mode, out.path, out.branch, out.commit) == ("holder_reuse", str(wt), "wt/legacy", None)


def test_holder_no_manifest_missing_blocks(world):
    conn, cfg = use_home(world, "hub")
    err = refusal(lambda: resolve(world, conn, cfg, snap(world, src=False, state="retained"), me="hub",
                                  workspace_path=str(world.tmp / "gone")))
    assert (err.code, err.scope) == ("custody_source_missing", "card")


def test_holder_copy_gone_but_published_materializes(world):
    conn, cfg = use_home(world, "hub")
    out = resolve(world, conn, cfg, snap(world), me="hub")
    assert out.mode == "materialized"


# --- refusals (v3 §6b, §9) ------------------------------------------------------------------------------------------

def test_remote_drift_is_card_scope(world):
    conn, cfg = use_home(world, "worker-2")
    commit(world.author, "after.txt")
    git(world.author, "push", "-q", "origin", BRANCH)
    err = refusal(lambda: resolve(world, conn, cfg, snap(world)))
    assert (err.code, err.scope) == ("remote_drift", "card")
    assert not (world.homes["worker-2"].clone.parent / ".worktrees" / str(REPO_ID) / TASK).exists()


def test_remote_branch_missing_is_card_scope(world):
    conn, cfg = use_home(world, "worker-2")
    git(world.author, "push", "-q", "origin", f":{BRANCH}")
    err = refusal(lambda: resolve(world, conn, cfg, snap(world)))
    assert (err.code, err.scope) == ("remote_branch_missing", "card")


def test_manifest_invalid_is_card_scope(world):
    conn, cfg = use_home(world, "worker-2")
    err = refusal(lambda: resolve(world, conn, cfg, snap(world, artifacts=[{"x": 1}])))
    assert (err.code, err.scope) == ("manifest_invalid", "card")


@pytest.mark.parametrize("rewrite", ["insteadOf", "pushurl", "url_any"])
def test_origin_rewrite_refused(world, rewrite):
    conn, cfg = use_home(world, "worker-2")
    clone = world.homes["worker-2"].clone
    if rewrite == "insteadOf":
        git(clone, "config", f"url.{world.origin}.insteadOf", "elsewhere:")
        git(clone, "remote", "set-url", "origin", "elsewhere:")
    elif rewrite == "pushurl":
        git(clone, "config", "remote.origin.pushurl", str(world.tmp / "fork.git"))
    else:
        git(clone, "config", "url.https://mirror.example/.pushInsteadOf", "https://github.com/")
    err = refusal(lambda: resolve(world, conn, cfg, snap(world)))
    assert err.code in ("origin_rewritten", "wrong_repo") and err.scope == "host"
    if rewrite != "insteadOf":
        assert err.code == "origin_rewritten"


def test_global_push_rewrite_that_misses_origin_passes(world):
    """Every pool worker has install-push-route's global ``url.https://github.com/.pushInsteadOf git@github.com:``.
    It cannot touch this origin, so the clone proves (it refused as ``origin_rewritten`` on every worker)."""
    conn, cfg = use_home(world, "worker-2")
    git(world.tmp, "config", "--global", "url.https://github.com/.pushInsteadOf", "git@github.com:")
    out = resolve(world, conn, cfg, snap(world))
    assert out.mode == "materialized"


@pytest.mark.parametrize("key", ["pushInsteadOf", "insteadOf"])
def test_global_rewrite_of_origin_refused(world, key):
    """A global rule that DOES change this origin's push or fetch URL still refuses."""
    conn, cfg = use_home(world, "worker-2")
    git(world.tmp, "config", "--global", f"url.{world.tmp / 'fork.git'}.{key}", str(world.origin))
    err = refusal(lambda: resolve(world, conn, cfg, snap(world)))
    assert (err.code, err.scope) == ("origin_rewritten", "host")


def test_global_fetch_only_rewrite_of_origin_refused(world):
    """A global insteadOf to a fork plus a pushInsteadOf back to origin moves only the FETCH URL: push still
    equals origin and the clone has no local url.* rule, so only the fetch-URL compare refuses it. The fork is a
    real, reachable repository: without that compare the clone would prove while fetching from it."""
    conn, cfg = use_home(world, "worker-2")
    clone = world.homes["worker-2"].clone
    fork = str(world.tmp / "fork.git")
    git(world.tmp, "clone", "-q", "--bare", str(world.origin), fork)
    git(world.tmp, "config", "--global", f"url.{fork}.insteadOf", str(world.origin))
    git(world.tmp, "config", "--global", f"url.{world.origin}.pushInsteadOf", str(world.origin))
    assert git(clone, "ls-remote", "--get-url", "origin") == fork
    assert git(clone, "remote", "get-url", "--push", "origin") == str(world.origin)
    err = refusal(lambda: resolve(world, conn, cfg, snap(world)))
    assert (err.code, err.scope) == ("origin_rewritten", "host")


def test_github_repo_uses_dispatcher_gh_argv(monkeypatch):
    """The id lookup runs under the home's gh_command_prefix (the hub's gh has no login or GH_TOKEN)."""
    from hermes_cli import kanban_gh_argv
    seen = []
    monkeypatch.setattr(kanban_gh_argv, "dispatcher_gh_argv", lambda: ["/opt/broker", "exec", "--", "/bin/gh"])

    def run(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(stdout='{"id": 42, "full_name": "acme/widget"}')
    monkeypatch.setattr(kcl.subprocess, "run", run)
    assert kcl.github_repo("acme/widget") == {"id": 42, "full_name": "acme/widget"}
    assert seen == [["/opt/broker", "exec", "--", "/bin/gh", "api", "repos/acme/widget", "--jq",
                     "{id: .id, full_name: .full_name}"]]


def test_github_repo_without_gh_argv_fails_closed(monkeypatch):
    from hermes_cli import kanban_gh_argv

    def unavailable():
        raise kanban_gh_argv.GhArgvUnavailable("kanban.pr_acceptance.gh_command_prefix is unreadable or invalid")
    monkeypatch.setattr(kanban_gh_argv, "dispatcher_gh_argv", unavailable)
    with pytest.raises(kanban_gh_argv.GhArgvUnavailable):
        kcl.github_repo("acme/widget")


def test_wrong_repo_clone_refused(world):
    conn, cfg = use_home(world, "worker-2")
    world.monkeypatch.setattr(kcl, "github_repo", lambda full_name: {"id": 7, "full_name": full_name})
    err = refusal(lambda: resolve(world, conn, cfg, snap(world)))
    assert (err.code, err.scope) == ("clone_missing", "host") or err.code == "wrong_repo"


def test_renamed_repo_same_id_passes(world):
    """The clone is registered under the OLD name; GitHub resolves both names to the same id."""
    conn, cfg = use_home(world, "worker-2")
    cfg = kws.Settings(**{**cfg.__dict__, "clones": {"acme/old-widget": str(world.homes["worker-2"].clone)}})
    out = resolve(world, conn, cfg, snap(world))
    assert out.mode == "materialized"


def test_live_checkout_refused_as_clone(world):
    live = Path(kcl.__file__).resolve().parents[1]
    err = refusal(lambda: kcl.prove_clone("acme/widget", str(live)))
    assert err.code == "clone_missing" and "live checkout" in err.detail


def test_occupied_path_refused(world):
    conn, cfg = use_home(world, "worker-2")
    (world.homes["worker-2"].clone.parent / ".worktrees" / str(REPO_ID) / TASK).mkdir(parents=True)
    err = refusal(lambda: resolve(world, conn, cfg, snap(world)))
    assert (err.code, err.scope) == ("occupied_path", "host")


def test_uncreated_destination_reserved_by_other_task_refused(world):
    from hermes_cli import kanban_db_workspace as kbw
    conn, cfg = use_home(world, "worker-2")
    target = world.homes["worker-2"].clone.parent / ".worktrees" / str(REPO_ID) / TASK
    other = kb.create_task(conn, title="reserved", assignee="worker")
    kbw.set_workspace_path(conn, other, str(target))
    err = refusal(lambda: resolve(world, conn, cfg, snap(world)))
    assert (err.code, err.scope) == ("occupied_path", "host")
    assert not target.exists()


def test_destination_claimed_by_other_task_refused(world):
    conn, cfg = use_home(world, "worker-2")
    clone = world.homes["worker-2"].clone
    home = world.tmp / "wt-home"
    git(clone, "worktree", "add", "-q", "-b", BRANCH, str(home), world.base)
    other = kb.create_task(conn, title="other", assignee="worker")
    from hermes_cli import kanban_db_workspace as kbw
    kbw.set_workspace_path(conn, other, str(home))
    err = refusal(lambda: resolve(world, conn, cfg, snap(world)))
    assert (err.code, err.scope) == ("occupied_path", "host")


def test_dirty_destination_refused(world):
    conn, cfg = use_home(world, "worker-2")
    clone = world.homes["worker-2"].clone
    home = world.tmp / "wt-home"
    git(clone, "worktree", "add", "-q", "-b", BRANCH, str(home), world.base)
    (home / "untracked.txt").write_text("local")
    err = refusal(lambda: resolve(world, conn, cfg, snap(world)))
    assert (err.code, err.scope) == ("dirty_destination", "host")
    assert (home / "untracked.txt").exists() and git(home, "rev-parse", "HEAD") == world.base  # nothing lost


def test_diverged_local_branch_refused(world):
    conn, cfg = use_home(world, "worker-2")
    clone = world.homes["worker-2"].clone
    git(clone, "branch", BRANCH, world.base)
    side = world.tmp / "side"
    git(clone, "worktree", "add", "-q", str(side), BRANCH)
    local = commit(side, "local-only.txt")
    git(clone, "worktree", "remove", str(side))
    err = refusal(lambda: resolve(world, conn, cfg, snap(world)))
    assert (err.code, err.scope) == ("diverged_local_branch", "host")
    assert git(clone, "rev-parse", BRANCH) == local  # never reset


def test_materialize_timeout_is_host_scope(world):
    """Q3: past ``materialize_timeout_s`` the host refuses (host scope), so the dispatcher memoizes the revision."""
    conn, cfg = use_home(world, "worker-2")
    ticks = iter([0.0] + [1000.0] * 50)
    err = refusal(lambda: ksr.resolve_source_workspace(conn, task(), snap(world), me="worker-2", cfg=cfg,
                                                       clock=lambda: next(ticks)))
    assert (err.code, err.scope) == ("materialize_timeout", "host")


@pytest.mark.parametrize("replica_revision", [None, 1])
def test_materialize_timeout_uses_grant_revision_then_defers_next_claim(world, replica_revision):
    from hermes_cli import kanban_source_dispatch as ksd
    from hermes_cli import kanban_db_sync as sync
    conn, cfg = use_home(world, "worker-2")
    claimed = _claimed(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_source=? WHERE id=?", (
            json.dumps(manifest(world, manifest_revision=replica_revision)) if replica_revision else None, claimed.id))
    original = ksr.resolve_source_workspace

    def timed(conn_, task_, snapshot, **kw):
        ticks = iter([0.0] + [1000.0] * 50)
        return original(conn_, task_, snapshot, cfg=cfg, clock=lambda: next(ticks), **kw)
    world.monkeypatch.setattr(ksr, "resolve_source_workspace", timed)
    snapshot = kws.SourceSnapshot(json.dumps(manifest(world, manifest_revision=7)), kws.Custody("hub", "published", 7))
    kws.stash_grant_snapshot(claimed.id, snapshot)
    err = refusal(lambda: ksd.resolve(conn, claimed, board=None, kbw=None))
    assert err.code == "materialize_timeout"
    assert ksd.refused(conn, claimed, err, SimpleNamespace(auto_blocked=[]))
    assert kws.skipped(conn, claimed.id, 7) == "materialize_timeout"
    assert kws.skipped(conn, claimed.id, replica_revision) is None
    # Once the feed catches up to the grant revision, claim_gate must defer before any new lease/run.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_source=? WHERE id=?", (json.dumps(manifest(world, manifest_revision=7)), claimed.id))
    runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
    assert sync.claim_gate(conn, claimed.id, 900).deferred
    deferral = sync.pop_claim_deferral(claimed.id)
    assert deferral == "workspace_source_refused_here:materialize_timeout"
    assert deferral.origin == sync.ORIGIN_LOCAL  # this host's skip memo, not a Board refusal
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == runs


def test_non_holder_with_retained_custody_refused(world):
    conn, cfg = use_home(world, "worker-2")
    err = refusal(lambda: resolve(world, conn, cfg, snap(world, state="retained")))
    assert (err.code, err.scope) == ("custody_not_portable", "host")


def test_disabled_host_refuses(world):
    conn, cfg = use_home(world, "worker-2")
    cfg = kws.Settings(**{**cfg.__dict__, "enabled": False})
    assert refusal(lambda: resolve(world, conn, cfg, snap(world))).code == "source_disabled"


# --- proof cache + attestation (v2 §3, v3 S5) ------------------------------------------------------------------------

def test_clone_id_cached_per_home_for_6h(world):
    use_home(world, "worker-2")
    path = str(world.homes["worker-2"].clone)
    kcl.prove_clone("acme/widget", path, now=1000.0)
    kcl.prove_clone("acme/widget", path, now=1000.0 + 3600)
    assert len(world.lookup_calls) == 1
    kcl.prove_clone("acme/widget", path, now=1000.0 + kcl.ID_CACHE_SECONDS + 1)
    assert len(world.lookup_calls) == 2
    assert (world.homes["worker-2"].hermes / "kanban" / "source-proof.json").exists()
    assert not (world.homes["hub"].home / ".hermes" / "kanban" / "source-proof.json").exists()


def test_attestation_body_and_old_board_404(world, monkeypatch):
    use_home(world, "worker-2")
    monkeypatch.setattr(kcl, "_BOARD_LACKS_ROUTE", False)
    clone = kcl.prove_clone("acme/widget", str(world.homes["worker-2"].clone))
    sent = []
    assert kcl.attest(lambda path, body: sent.append((path, body)), [clone])
    assert sent == [("/v2/machines/me/sources", {"version": 1, "repos": [{"id": REPO_ID, "full_name": "acme/widget"}]})]

    class NotFound(Exception):
        status = 404
    calls = []

    def old_board(path, body):
        calls.append(path)
        raise NotFound("no route")
    assert not kcl.attest(old_board, [clone])
    assert kcl.board_lacks_source_route()
    assert not kcl.attest(old_board, [clone]) and len(calls) == 1  # logged once, then disabled locally


def test_attest_heartbeat_inert_when_disabled(world):
    sent = []
    assert not kcl.attest_heartbeat(lambda p, b: sent.append(p), kws.settings({}))
    assert sent == []


def test_attest_if_due_from_sync_round(world, monkeypatch):
    use_home(world, "worker-2")
    monkeypatch.setattr(kcl, "_BOARD_LACKS_ROUTE", False)
    monkeypatch.setattr(kcl, "_ATTEST_STATE", {"next_at": 0.0})
    sent = []
    hub = SimpleNamespace(attest_sources=sent.append)
    assert not kcl.attest_if_due(hub, now=1.0, cfg=kws.settings({}))  # disabled (S9): nothing sent
    cfg = kws.Settings(enabled=True, requested=True, clones={"acme/widget": str(world.homes["worker-2"].clone)},
                       publish_on_park=True, push_timeout_s=30, preflight_timeout_s=60, materialize_timeout_s=None)
    assert kcl.attest_if_due(hub, now=2.0, cfg=cfg)
    assert not kcl.attest_if_due(hub, now=3.0, cfg=cfg)  # rate-limited
    assert kcl.attest_if_due(hub, now=3.0 + kcl.ATTEST_EVERY_S, cfg=cfg)
    assert sent == [{"version": 1, "repos": [{"id": REPO_ID, "full_name": "acme/widget"}]}] * 2


# --- dispatcher refusal handling (v3 §6b) ----------------------------------------------------------------------------

def _claimed(conn):
    tid = kb.create_task(conn, title="src", assignee="worker")
    assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    return kb.get_task(conn, tid)


def test_host_refusal_releases_without_failure_and_memoizes(world):
    from hermes_cli import kanban_source_dispatch as ksd
    conn, _cfg = use_home(world, "worker-2")
    claimed = _claimed(conn)
    exc = kws.WorkspaceSourceRefusal("materialize_timeout", "slow", evidence={"manifest_revision": 5})
    result = SimpleNamespace(auto_blocked=[])
    assert ksd.refused(conn, claimed, exc, result)
    row = conn.execute("SELECT status, consecutive_failures FROM tasks WHERE id = ?", (claimed.id,)).fetchone()
    assert row[0] == "ready" and not row[1] and result.auto_blocked == []
    assert kws.skipped(conn, claimed.id, 5) == "materialize_timeout"
    kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (claimed.id,))]
    assert "workspace_source_refused" in kinds


def test_card_refusal_blocks_needs_input(world):
    from hermes_cli import kanban_source_dispatch as ksd
    conn, _cfg = use_home(world, "worker-2")
    claimed = _claimed(conn)
    result = SimpleNamespace(auto_blocked=[])
    assert ksd.refused(conn, claimed, kws.WorkspaceSourceRefusal("remote_drift", "moved"), result)
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (claimed.id,)).fetchone()[0] == "blocked"
    assert result.auto_blocked == [claimed.id]


def test_other_exceptions_left_to_legacy_path(world):
    from hermes_cli import kanban_source_dispatch as ksd
    conn, _cfg = use_home(world, "worker-2")
    assert not ksd.refused(conn, SimpleNamespace(id="t"), RuntimeError("x"), SimpleNamespace(auto_blocked=[]))
