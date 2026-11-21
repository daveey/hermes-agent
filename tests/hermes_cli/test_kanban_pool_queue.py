"""One worker queue: portable cards get ``runs_on: [pool:worker]``, not a machine pin. [daveey]

11-12 05:40Z: ten ready cards were pinned ``machine:worker-2`` while worker-2 was drain-held, and worker-1 and
worker-4 had free slots; the oldest waited 261 min. Every route that put portable work on a worker wrote one machine
name (the Board reservation, ``heavy_route``, agent moves). Here, one test per entry path: the agent tool create,
the CLI create, the heavy route, ``hermes kanban move --pool``, a machine move without ``host_reason`` (refused), and a
park of a pushed workspace (stays on the pool; only unpushed state pins it, with the reason in the event).
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_move as km
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli._placement_helpers import seen_now
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import HUB, MACHINES, WORKER, Gateway

POOL = "pool:worker"
CFG = {"worker_queue": {HUB: POOL}}
TAILNET = "capability:tailnet-exec"
HUB_WRITE = "capability:hub-write"
# The live pool workers' labels (Board registry, 11-13): every one reaches the hub, the Macs and token-broker.
LIVE = ["linux", POOL, HUB_WRITE, TAILNET, "capability:token-broker"]
W1 = {**MACHINES[WORKER], "labels": LIVE}
W2 = {**MACHINES[WORKER], "name": "worker-2", "labels": LIVE}


def _machines(*, held=()):
    return {m["name"]: {**m, "hold": m["name"] in held} for m in (MACHINES[HUB], W1, W2, MACHINES["macbook"])}


def _registry(*, held=()):
    machines = list(_machines(held=held).values())
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": seen_now(machines)})


@pytest.fixture
def svc():
    return FakeService(lease_runs=True, machines=MACHINES)


@pytest.fixture
def hub(tmp_path, monkeypatch, svc):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    monkeypatch.setattr(kp, "config", lambda: CFG)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # card worktrees are looked up under ~/code/_wt
    sync.consume_dispatcher_wake()
    gw = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    gw.enter()
    yield gw
    gw.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _runs_on(conn, tid):
    raw = conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    return json.loads(raw) if raw else None


def _moved(conn, tid):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'moved' ORDER BY id", (tid,))]


# --- create -----------------------------------------------------------------------------------------------------

def test_tool_create_queues_a_portable_card_for_the_pool_while_one_worker_is_held(hub):
    from tools import kanban_tools
    _registry(held=("worker-1",))
    out = json.loads(kanban_tools._handle_create({"title": "summarise the failing CI jobs", "assignee": "default",
                                                  "requires": []}))
    assert out.get("ok") and out["runs_on"] == [POOL], out
    assert _runs_on(hub.conn, out["task_id"]) == [POOL]
    assert "worker_queue" in out["placement"]["why"], out
    # No machine is named, so the held worker cannot strand it: unheld worker-2 is eligible right now.
    card = dict(hub.conn.execute("SELECT * FROM tasks WHERE id = ?", (out["task_id"],)).fetchone())
    assert wn.eligible(card, "worker-2", _machines(held=("worker-1",))) is None
    assert wn.eligible(card, "worker-1", _machines(held=("worker-1",))) == "held"


def test_tool_create_keeps_host_work_and_an_empty_pool_off_the_queue(hub):
    from tools import kanban_tools
    _registry()
    out = json.loads(kanban_tools._handle_create({"title": "restart the gateway service on the hub",
                                                  "assignee": "default", "requires": []}))
    assert out.get("ok") and _runs_on(hub.conn, out["task_id"]) is None, out
    _registry(held=("worker-1", "worker-2"))
    out = json.loads(kanban_tools._handle_create({"title": "summarise CI", "assignee": "default", "requires": []}))
    assert out.get("ok") and _runs_on(hub.conn, out["task_id"]) is None, out


def test_cli_create_queues_a_portable_card_for_the_pool(hub):
    from hermes_cli import kanban as kcli
    _registry(held=("worker-1",))
    out = kcli.run_slash('create "summarise the failing CI jobs" --assignee default --json')
    tid = json.loads(out)["id"]
    assert _runs_on(hub.conn, tid) == [POOL], out
    out = kcli.run_slash('create "pinned" --assignee default --runs-on machine:worker-2 --json')
    assert _runs_on(hub.conn, json.loads(out)["id"]) == ["machine:worker-2"], out  # an explicit pin wins


def test_explicit_auto_beats_a_skill_local_marker_and_the_remedy_names_it(hub, monkeypatch):
    """11-12: the marker remedy said re-create with placement='hub'; hub admission refuses portable work, so the
    caller was left with an archived card and no replacement. An explicit 'auto' now queues it for the pool;
    an omitted placement still keeps it local, and the remedy names 'auto'. [daveey]"""
    from hermes_cli import kanban_skill_check
    from tools import kanban_tools
    monkeypatch.setattr(kanban_skill_check, "check_task_skills", lambda *a, **k: None)  # no skills dir here
    _registry()
    card = {"title": "Fix the parser and add unit tests", "assignee": "default", "requires": [],
            "skills": ["babybear-development"]}
    out = json.loads(kanban_tools._handle_create({**card, "placement": "auto"}))
    assert out.get("ok") and out["runs_on"] == [POOL] and "remedy" not in out, out
    assert _runs_on(hub.conn, out["task_id"]) == [POOL]
    out = json.loads(kanban_tools._handle_create({**card, "title": "Fix the lexer and add unit tests"}))
    assert out.get("ok") and _runs_on(hub.conn, out["task_id"]) is None, out
    placement = out["placement"]
    assert (placement["where"], placement["why"]) == ("local", "local marker: skill babybear-development"), out


def test_explicit_auto_keeps_a_device_skill_local(hub, monkeypatch):
    """Review of #1441: 'auto' is also the routine default callers spell out, so it must not move a card whose
    skill drives this machine (apple-*, computer-use, macos-, ios-, a local Chrome). [daveey]"""
    from hermes_cli import kanban_skill_check
    from tools import kanban_tools
    monkeypatch.setattr(kanban_skill_check, "check_task_skills", lambda *a, **k: None)
    _registry()
    titles = ("Click through the Settings pane", "Tag the reading list notes", "Reorder the dock icons",
              "Sign the shortcut bundle")
    for title, skill in zip(titles, ("computer-use", "apple-notes", "macos-dock-management", "ios-shortcuts-build")):
        out = json.loads(kanban_tools._handle_create({
            "title": title, "assignee": "default", "requires": [], "skills": [skill],
            "placement": "auto"}))
        assert out.get("ok") and _runs_on(hub.conn, out["task_id"]) is None, out
        assert out["placement"]["why"] == f"local marker: skill {skill}", out
    # A device skill beside a context skill still pins: 'auto' drops only the context one.
    assert kp.marker_skills({"skills": ["babybear-development", "computer-use"], "placement": "auto"},
                            kp._Cfg(CFG)) == ["computer-use"]
    assert kp.marker_skills({"skills": ["bb-self-fix"]}, kp._Cfg(CFG)) == ["bb-self-fix"]  # omitted: unchanged


def test_marker_remedies_name_the_override_that_works():
    skill = kp.marker_remedy("local marker: skill babybear-development", {})
    assert skill == "if the work doesn't need this machine, archive and re-create with placement='auto'"
    assert kp.marker_remedy("local marker: skill bb-self-fix", {}) == skill
    assert kp.marker_remedy("local marker: skill computer-use", {}) == kp.DEVICE_SKILL_MARKER_REMEDY
    text = kp.marker_remedy("local marker: mentions 'macbook'", {})
    assert text == kp.TEXT_MARKER_REMEDY and "placement='hub'" not in text, text
    # A replaced overridable list is honoured.
    assert kp.marker_remedy("local marker: skill computer-use",
                            {"auto_overridable_skill_markers": ["^computer-use$"]}) == skill
    cfg, live = {"route": {"macbook": HUB}}, (lambda _t: None)
    card = {"title": "Fix the parser", "skills": ["babybear-development"]}
    assert kp.decide(card, cfg=cfg, self_gw="macbook", live=live)["why"] == "local marker: skill babybear-development"
    assert kp.decide({**card, "placement": "auto"}, cfg=cfg, self_gw="macbook", live=live)["where"] == HUB
    apple = {"title": "Fix the parser", "skills": ["apple-notes"], "placement": "auto"}
    assert kp.decide(apple, cfg=cfg, self_gw="macbook", live=live)["why"] == "local marker: skill apple-notes"
    title = {"title": "Verify 57094b7a60 live on the MacBook", "placement": "auto"}
    assert kp.decide(title, cfg=cfg, self_gw="macbook", live=live)["why"].startswith("local marker: mentions")


def test_a_local_pin_note_simulates_an_omitted_placement_not_an_explicit_auto(hub):
    """A context-skill card pinned local stays local without the pin too (the skill marker holds when placement is
    omitted), so it gets no "would run elsewhere" note; the same card without the skill does. [daveey]"""
    from tools.kanban_tools_placement import _local_placement
    _registry()
    pinned = {"where": "local", "why": kp.PINNED_LOCAL_WHY}
    card = {"title": "summarise the failing CI jobs", "assignee": "default", "requires": [], "placement": "local"}
    plain = _local_placement(kp, card, pinned, machine_ok=None, note_local_pin=True)
    assert plain.get("note"), plain  # control: portable work pinned local is told where it would go
    skilled = _local_placement(kp, {**card, "skills": ["babybear-development"]}, pinned, machine_ok=None,
                               note_local_pin=True)
    assert "note" not in skilled, skilled


def test_heavy_route_accepts_a_pool_label_and_checks_it_is_live(hub):
    cfg = {"heavy_route": {HUB: POOL}}
    _registry(held=("worker-1",))
    d = kp.decide({"title": "Run the regression tests for the parser"}, cfg=cfg, self_gw=HUB)
    assert d["where"] == "local" and d["runs_on"] == [POOL], d
    wn.set_registry_fetch(None)
    _registry(held=("worker-1", "worker-2"))
    d = kp.decide({"title": "Run the regression tests for the parser"}, cfg=cfg, self_gw=HUB)
    assert "runs_on" not in d, d  # no unheld machine carries the label: the card stays on the hub


# --- move -------------------------------------------------------------------------------------------------------

def test_cli_move_pool_requeues_a_worker_pin_for_any_worker(hub):
    from hermes_cli import kanban as kcli
    _registry()
    tid = kb.create_task(hub.conn, title="stuck on worker-1", assignee="default", runs_on=["machine:worker-1"])
    out = kcli.run_slash(f'move {tid} --pool --reason "worker-1 is held; any worker can run it" --json')
    res = json.loads(out)
    assert res["ok"] and res["runs_on"] == [POOL] and res["matches"] == ["worker-1", "worker-2"], res
    assert _runs_on(hub.conn, tid) == [POOL]


def test_machine_move_to_one_pool_worker_needs_a_host_reason(hub):
    from hermes_cli import kanban as kcli
    _registry()
    tid = kb.create_task(hub.conn, title="work", assignee="default", runs_on=[POOL])
    res = km.move_queued(hub.conn, tid, runs_on=["machine:worker-2"], reason="worker-2 has free slots")
    assert not res["ok"] and res["reason_code"] == "host_reason_required" and "--pool" in res["error"], res
    out = kcli.run_slash(f'move {tid} --runs-on machine:worker-2 --reason "free slots" --json')
    assert json.loads(out)["reason_code"] == "host_reason_required", out
    assert _runs_on(hub.conn, tid) == [POOL]  # nothing was written
    res = km.move_queued(hub.conn, tid, runs_on=["machine:worker-2"], reason="repro the worker-2 disk alert",
                         host_reason="the card fixes worker-2's own disk")
    assert res["ok"], res
    assert _moved(hub.conn, tid)[-1]["host_reason"] == "the card fixes worker-2's own disk"


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


def _commit(tree, name):
    (tree / name).write_text(name + "\n")
    _git(tree, "add", name)
    _git(tree, "commit", "-qm", name)


def _pushed_clone(tmp_path, where):
    """A clone at ``where`` whose one commit is on its remote."""
    remote = tmp_path / f"{where.name}-remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    _git(tmp_path, "clone", "-q", str(remote), str(where))
    _commit(where, "a.txt")
    _git(where, "push", "-q", "origin", "HEAD:refs/heads/wt/x")
    return where


# --- park -------------------------------------------------------------------------------------------------------

@pytest.fixture
def w2(tmp_path, monkeypatch, svc):
    """The gateway of pool worker ``worker-2``, running its own card's run as a dispatched worker would."""
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sync.consume_dispatcher_wake()
    gw = Gateway(tmp_path, "worker-2", svc, monkeypatch, pickup="true")
    gw.enter()
    yield gw
    gw.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _run(gw, monkeypatch, tid):
    with kb.write_txn(gw.conn):
        gw.conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert kb.claim_task(gw.conn, tid) is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(gw.conn, tid)))


def _placement_events(conn, tid):
    return [(k, json.loads(p)) for k, p in conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN ('pool_pinned', 'pool_unpinned') "
        "ORDER BY id", (tid,))]


def test_park_keeps_a_pushed_card_on_the_pool_and_pins_only_unpushed_state(w2, tmp_path, monkeypatch):
    """Through the real park entry points (kanban_continue, kanban_block kind=time, kanban_schedule)."""
    from tools import kanban_tools as kt
    conn = w2.conn
    tid = kb.create_task(conn, title="pool card with a worktree", assignee="default", runs_on=[POOL])
    ws = tmp_path / "ws"
    ws.mkdir()
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(ws), tid))
    # The card's tree lives outside its scratch workspace, where card worktrees usually are (~/code/_wt/<slug>).
    tree = _pushed_clone(tmp_path, tmp_path / "code" / "_wt" / "pool-card")

    # 1. Clean and pushed: kanban_continue leaves the card on the pool, so any worker can resume it.
    _run(w2, monkeypatch, tid)
    out = json.loads(kt._handle_continue({"checkpoint": f"Done: a.txt. Worktree {tree}, pushed as wt/x."}))
    assert out.get("ok"), out
    assert _runs_on(conn, tid) == [POOL] and _placement_events(conn, tid) == []

    # 2. An unpushed commit: the time park pins the card to this worker and says why.
    _run(w2, monkeypatch, tid)
    _commit(tree, "b.txt")
    out = json.loads(kt._handle_block({"kind": "time", "clock_only": True, "awaiting": WINDOW, "reason": (
        f"SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on CI; b.txt is committed in {tree}")}))
    assert out.get("ok"), out
    assert _runs_on(conn, tid) == ["machine:worker-2"]
    kind, ev = _placement_events(conn, tid)[-1]
    assert kind == "pool_pinned" and ev["unpushed_state"] == f"unpushed commits in {tree.resolve()}", ev
    assert ev["previous_runs_on"] == [POOL] and str(tree.resolve()) in ev["trees"], ev

    # 3. Pushed again: the next park (kanban_schedule) puts the card back on the pool.
    _run(w2, monkeypatch, tid)
    _git(tree, "push", "-q", "origin", "HEAD:refs/heads/wt/x")
    out = json.loads(kt._handle_schedule({"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on CI",
                                          "clock_only": True, "awaiting": WINDOW}))
    assert out.get("ok"), out
    assert _runs_on(conn, tid) == [POOL]
    assert [k for k, _ in _placement_events(conn, tid)] == ["pool_pinned", "pool_unpinned"]


def test_park_keeps_work_pushed_by_url_on_the_pool_even_after_the_remote_branch_moved(w2, tmp_path, monkeypatch):
    """11-12: t_01a1d0a1eaa00371 pushed its fix with ``git push https://...git HEAD:wt/...`` (a URL, so origin/*
    stayed at the old tip) and a server-side update-branch then moved the branch past it. The park called that
    "unpushed commits", pinned the card to machine:worker-2, and it sat ready while worker-2 was on a Board hold."""
    from tools import kanban_tools as kt
    conn = w2.conn
    tid = kb.create_task(conn, title="pool card pushed by url", assignee="default", runs_on=[POOL])
    tree = _pushed_clone(tmp_path, tmp_path / "code" / "_wt" / "url-push")
    remote = str(tmp_path / "url-push-remote.git")
    _git(tree, "fetch", "-q", "origin")  # origin/wt/x tracks the pushed tip
    _commit(tree, "b.txt")
    _git(tree, "push", "-q", remote, "HEAD:refs/heads/wt/x")  # by URL: origin/wt/x stays behind HEAD
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", "-b", "wt/x", remote, str(other))
    _commit(other, "merge-from-base.txt")  # GitHub's update-branch: the remote branch moves past HEAD
    _git(other, "push", "-q", "origin", "HEAD:refs/heads/wt/x")

    _run(w2, monkeypatch, tid)
    out = json.loads(kt._handle_block({"kind": "time", "clock_only": True, "awaiting": WINDOW, "reason": (
        f"SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on review; fix pushed from {tree}")}))
    assert out.get("ok"), out
    assert _runs_on(conn, tid) == [POOL] and _placement_events(conn, tid) == []

    # A commit no remote branch holds still pins, even with a stale tracking ref around.
    _run(w2, monkeypatch, tid)
    _commit(tree, "c.txt")
    out = json.loads(kt._handle_block({"kind": "time", "clock_only": True, "awaiting": WINDOW, "reason": (
        f"SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on review; c.txt only in {tree}")}))
    assert out.get("ok"), out
    assert _runs_on(conn, tid) == ["machine:worker-2"]
    assert _placement_events(conn, tid)[-1][1]["unpushed_state"] == f"unpushed commits in {tree.resolve()}"


@pytest.mark.parametrize("rebased", [False, True])
def test_url_push_moved_tip_without_ancestral_tracking(w2, tmp_path, monkeypatch, rebased):
    from tools import kanban_tools as kt
    tree = _pushed_clone(tmp_path, tmp_path / "url-tip")
    remote = str(tmp_path / "url-tip-remote.git")
    if rebased:
        _git(tree, "commit", "--amend", "-qm", "rewritten root")
    else:
        _commit(tree, "new.txt")
    branch = "wt/x" if rebased else "wt/new"
    _git(tree, "push", "-q", "--force", remote, f"HEAD:refs/heads/{branch}")
    other = tmp_path / "other-tip"
    _git(tmp_path, "clone", "-q", "-b", branch, remote, str(other))
    _commit(other, "advanced.txt")
    _git(other, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    tid = kb.create_task(w2.conn, title="URL pushed tip", assignee="default", runs_on=[POOL])
    _run(w2, monkeypatch, tid)
    out = json.loads(kt._handle_continue({"checkpoint": f"Pushed work in {tree}"}))
    assert out.get("ok"), out
    assert _runs_on(w2.conn, tid) == [POOL]


@pytest.mark.parametrize("destination", ["same-name", "upstream", "push"])
def test_url_push_refresh_prioritizes_branch_over_earlier_heads(tmp_path, destination):
    from hermes_cli import kanban_move_pool as kmp
    tree = _pushed_clone(tmp_path, tmp_path / "priority")
    remote = str(tmp_path / "priority-remote.git")
    _git(tree, "branch", "-m", "wt/x")
    # Six earlier heads must not spend the entire five-fetch budget.
    for i in range(6):
        _git(tree, "push", "-q", remote, f"HEAD:refs/heads/aaa/{i}")
    _git(tree, "commit", "--amend", "-qm", "rewritten root")
    branch = "wt/x" if destination == "same-name" else "zzz/destination"
    if destination == "upstream":
        _git(tree, "config", "branch.wt/x.remote", "origin")
        _git(tree, "config", "branch.wt/x.merge", f"refs/heads/{branch}")
    elif destination == "push":
        _git(tree, "config", "remote.origin.push", f"refs/heads/wt/x:refs/heads/{branch}")
    _git(tree, "push", "-q", "--force", remote, f"HEAD:refs/heads/{branch}")
    other = tmp_path / "priority-other"
    _git(tmp_path, "clone", "-q", "-b", branch, remote, str(other))
    _commit(other, "advanced.txt")
    _git(other, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    assert kmp.pushed_on_remote(str(tree))
    _commit(tree, "unpublished.txt")
    assert not kmp.pushed_on_remote(str(tree))


@pytest.mark.parametrize("failure", ["bad-url", "timeout"])
def test_remote_failure_pins_without_reading_stdin(w2, tmp_path, monkeypatch, failure):
    from hermes_cli import worktree_ops
    from tools import kanban_tools as kt
    (tmp_path / "code" / "_wt").mkdir(parents=True, exist_ok=True)
    tree = _pushed_clone(tmp_path, tmp_path / "code" / "_wt" / "unreachable")  # a per-card tree location
    _commit(tree, "local.txt")
    _git(tree, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    original = worktree_ops._git_out
    calls = []

    def observe(args, cwd, **kwargs):
        if args[0] == "ls-remote":
            calls.append(kwargs)
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 20)
        return original(args, cwd, **kwargs)

    monkeypatch.setattr(worktree_ops, "_git_out", observe)
    tid = kb.create_task(w2.conn, title="unreachable remote", assignee="default", runs_on=[POOL])
    _run(w2, monkeypatch, tid)
    out = json.loads(kt._handle_continue({"checkpoint": f"Local work in {tree}"}))
    assert out.get("ok"), out
    assert _runs_on(w2.conn, tid) == ["machine:worker-2"]
    assert calls and all(call.get("stdin") == subprocess.DEVNULL for call in calls)
    assert all("env" not in call for call in calls)  # retain private HTTPS credential helpers


def test_remote_refresh_caps_and_stdin(monkeypatch):
    from hermes_cli import kanban_move_pool as kmp, worktree_ops
    calls = []

    def git_out(args, cwd, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "rev-parse":
            return "head"
        if args[0] == "remote":
            return "origin"
        if args[0] == "ls-remote":
            return "\n".join(f"new{i}\trefs/heads/b{i}" for i in range(60))
        if args[0] == "for-each-ref":
            return "\n".join(f"old{i} refs/remotes/origin/b{i}" for i in range(60))
        return None  # unrelated old tips and failed fetches must not prove publication

    monkeypatch.setattr(worktree_ops, "_git_out", git_out)
    monkeypatch.setattr(worktree_ops, "_worktree_has_unpushed_commits", lambda tree, **kw: True)
    assert not kmp.pushed_on_remote("unused")
    assert sum(args[0] == "merge-base" for args, _ in calls) == 50
    assert sum(args[0] == "fetch" for args, _ in calls) == 5
    assert all(kw.get("stdin") == subprocess.DEVNULL and "env" not in kw for _, kw in calls)


def test_a_head_that_is_a_remote_branch_tip_counts_as_pushed(tmp_path):
    from hermes_cli import kanban_move_pool as kmp
    tree = _pushed_clone(tmp_path, tmp_path / "tip")
    _commit(tree, "b.txt")
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"
    _git(tree, "push", "-q", str(tmp_path / "tip-remote.git"), "HEAD:refs/heads/wt/new")  # new branch, by URL
    assert kmp.tree_state([str(tree)]) is None


def test_park_pins_a_dirty_tree_found_by_its_owner_marker(w2, tmp_path, monkeypatch):
    from tools import kanban_tools as kt
    conn = w2.conn
    tid = kb.create_task(conn, title="pool card", assignee="default", runs_on=[POOL])
    main = _pushed_clone(tmp_path, tmp_path / "main")
    tree = tmp_path / "code" / "_wt" / "marked"
    _git(main, "worktree", "add", "-q", "-b", "wt/marked", str(tree))
    gitdir = (tree / ".git").read_text().split(":", 1)[1].strip()
    (Path(gitdir) / "hermes-owner.json").write_text(json.dumps({"task_id": tid}))
    (tree / "wip.txt").write_text("not committed\n")
    _run(w2, monkeypatch, tid)
    out = json.loads(kt._handle_continue({"checkpoint": "half done", "resume_after_seconds": 600,
                                          "clock_only": True}))
    assert out.get("ok"), out
    assert _runs_on(conn, tid) == ["machine:worker-2"]
    assert _placement_events(conn, tid)[-1][1]["unpushed_state"] == f"dirty worktree {tree.resolve()}"


# --- false park pins (t_01a1d145c441f2bd) -----------------------------------------------------------------------
# 11-12 18:45Z: 7 of 9 park pins were false. 5 core cards were pinned to worker-2 over "dirty worktree
# ~/.hermes/hermes-agent" (one stray untracked file in the gateway's live core checkout, which every core card names),
# and 2 to worker-6 over "unpushed commits" in clones that track only origin/state while HEAD was on another branch.

def _owned_worktree(main, tree, tid, branch):
    """A linked worktree of ``main`` at ``tree``, marked for ``tid`` by the worktree-owner plugin, pushed."""
    _git(main, "worktree", "add", "-q", "-b", branch, str(tree))
    gitdir = (tree / ".git").read_text().split(":", 1)[1].strip()
    (Path(gitdir) / "hermes-owner.json").write_text(json.dumps({"task_id": tid}))
    _commit(tree, "fix.txt")
    _git(tree, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    return tree


# A scheduled wait needs a wake reason (kanban_awaiting no_wake_reason).
WINDOW = {"kind": "clock", "why": "the CI / review window"}


def _park(kt, tree, extra=""):
    return json.loads(kt._handle_block({"kind": "time", "clock_only": True, "awaiting": WINDOW, "reason": (
        f"SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on CI for {tree}{extra}")}))


def test_park_ignores_a_dirty_shared_core_checkout_the_card_text_names(w2, tmp_path, monkeypatch):
    from tools import kanban_tools as kt
    conn = w2.conn
    tid = kb.create_task(conn, title="core card", assignee="default", runs_on=[POOL],
                         body="Build in a hermes-core-wt worktree, never in the live ~/.hermes/hermes-agent.")
    live = _pushed_clone(tmp_path, tmp_path / ".hermes" / "hermes-agent")  # the gateway's shared main checkout
    (live / "cache-sizes.txt").write_text("a stray diagnostic listing\n")
    tree = _owned_worktree(live, tmp_path / "code" / "_wt" / "core-fix", tid, "wt/core-fix")
    _run(w2, monkeypatch, tid)
    out = json.loads(kt._handle_block({"kind": "time", "clock_only": True, "awaiting": WINDOW, "reason": (
        f"SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting on CI for {tree}; deploys to {live} via hermes update")}))
    assert out.get("ok"), out
    assert _runs_on(conn, tid) == [POOL] and _placement_events(conn, tid) == []


def test_a_false_pin_on_a_shared_checkout_is_lifted_at_the_next_park(w2, tmp_path, monkeypatch):
    """The 11-12 pins recorded the live checkout in ``trees``: the next park of such a card puts it back."""
    from tools import kanban_tools as kt
    conn = w2.conn
    tid = kb.create_task(conn, title="core card", assignee="default", runs_on=[POOL])
    live = _pushed_clone(tmp_path, tmp_path / ".hermes" / "hermes-agent")
    (live / "cache-sizes.txt").write_text("a stray diagnostic listing\n")
    tree = _owned_worktree(live, tmp_path / "code" / "_wt" / "core-fix", tid, "wt/core-fix")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET runs_on = ? WHERE id = ?", (json.dumps(["machine:worker-2"]), tid))
        kb._append_event(conn, tid, "pool_pinned", {
            "from_machine": "worker-2", "runs_on": ["machine:worker-2"], "previous_runs_on": [POOL],
            "unpushed_state": f"dirty worktree {live.resolve()}", "trees": [str(live.resolve()), str(tree.resolve())]})
    _run(w2, monkeypatch, tid)
    assert _park(kt, tree).get("ok")
    assert _runs_on(conn, tid) == [POOL]
    assert [k for k, _ in _placement_events(conn, tid)] == ["pool_pinned", "pool_unpinned"]


def test_a_dirty_linked_worktree_the_text_names_still_pins_and_the_event_names_the_check(w2, tmp_path, monkeypatch):
    from tools import kanban_tools as kt
    conn = w2.conn
    tid = kb.create_task(conn, title="core card", assignee="default", runs_on=[POOL])
    main = _pushed_clone(tmp_path, tmp_path / "main")
    tree = tmp_path / "code" / "_wt" / "named"
    _git(main, "worktree", "add", "-q", "-b", "wt/named", str(tree))  # no owner marker: only the text names it
    (tree / "wip.txt").write_text("not committed\n")
    _run(w2, monkeypatch, tid)
    assert _park(kt, tree, "\nUNPUBLISHED_WORK_REASON=half-written wip.txt").get("ok")
    assert _runs_on(conn, tid) == ["machine:worker-2"]
    ev = _placement_events(conn, tid)[-1][1]
    assert (ev["check"], ev["tree"]) == ("dirty", str(tree.resolve())), ev


def _single_branch_clone(tmp_path, name):
    """worker-6's workspace clone: tracks only ``origin/state``; HEAD, one commit ahead, pushed as ``wt/x``."""
    remote = tmp_path / f"{name}-remote.git"
    seed = tmp_path / f"{name}-seed"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    _commit(seed, "base.txt")
    _git(seed, "push", "-q", "origin", "HEAD:refs/heads/state")
    tree = tmp_path / "ws" / name
    _git(tmp_path, "clone", "-q", "--single-branch", "-b", "state", str(remote), str(tree))
    _commit(tree, "fix.txt")
    _git(tree, "push", "-q", "origin", "HEAD:refs/heads/wt/x")
    return tree, remote


def test_a_single_branch_clone_whose_head_is_on_another_remote_branch_is_pushed(tmp_path):
    from hermes_cli import kanban_move_pool as kmp
    tree, remote = _single_branch_clone(tmp_path, "tip")
    assert kmp.tree_state([str(tree)]) is None  # HEAD equals refs/heads/wt/x
    # The branch then moves past HEAD on the remote (GitHub update-branch) and no tracking ref exists for it; a
    # fetch of that branch alone (FETCH_HEAD) brings its tip here.
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", "-b", "wt/x", str(remote), str(other))
    _commit(other, "merge-from-base.txt")
    _git(other, "push", "-q", "origin", "HEAD:refs/heads/wt/x")
    _git(tree, "fetch", "-q", "origin", "wt/x")
    assert kmp.tree_state([str(tree)]) is None


def test_park_and_move_keep_a_pushed_single_branch_clone_on_the_pool(w2, tmp_path, monkeypatch):
    from tools import kanban_tools as kt
    conn = w2.conn
    tid = kb.create_task(conn, title="state card", assignee="default", runs_on=[POOL])
    tree, remote = _single_branch_clone(tmp_path, "state-clone")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", "-b", "wt/x", str(remote), str(other))
    _commit(other, "merge-from-base.txt")
    _git(other, "push", "-q", "origin", "HEAD:refs/heads/wt/x")
    _git(tree, "fetch", "-q", "origin", "wt/x")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(tree.parent), tid))
    _run(w2, monkeypatch, tid)
    assert _park(kt, tree).get("ok")
    assert _runs_on(conn, tid) == [POOL] and _placement_events(conn, tid) == []
    _registry()
    res = km.move_queued(conn, tid, runs_on=["machine:worker-2"], reason="resume here", host_reason="my tree is here",
                         me="worker-2", machines=_machines())
    assert not res["ok"] and res["reason_code"] == "pool_keeps_pushed", res


def test_a_head_no_remote_holds_and_an_unreachable_remote_still_pin(tmp_path, monkeypatch):
    from hermes_cli import kanban_move_pool as kmp
    from hermes_cli import worktree_ops
    tree, remote = _single_branch_clone(tmp_path, "fail-safe")
    _commit(tree, "only-here.txt")
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"
    _git(tree, "reset", "-q", "--hard", "HEAD~1")  # back to the pushed wt/x tip
    assert kmp.tree_state([str(tree)]) is None
    # That check may refresh wt/x into a tracking ref; drop it so the failure cases below still need the remote.
    _git(tree, "update-ref", "-d", "refs/remotes/origin/wt/x")
    _git(tree, "remote", "set-url", "origin", str(tmp_path / "gone.git"))  # ls-remote fails
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"
    _git(tree, "remote", "set-url", "origin", str(remote))
    real = worktree_ops._git_out
    timeouts = []

    def slow_remote(args, cwd, timeout=10, **kw):
        if args[:1] == ["ls-remote"]:
            timeouts.append(timeout)
            raise subprocess.TimeoutExpired(["git", *args], timeout)
        return real(args, cwd, timeout=timeout, **kw)

    monkeypatch.setattr(worktree_ops, "_git_out", slow_remote)
    assert kmp.tree_state([str(tree)]) == f"unpushed commits in {tree}"
    assert timeouts and max(timeouts) <= 10, timeouts


# --- worker-origin cards ----------------------------------------------------------------------------------------

def test_a_worker_created_card_joins_the_pool_and_a_pin_to_the_ship_host_stays(w2, monkeypatch):
    """A card a worker run creates used to be a local row only that worker could claim (11-12 06:40: worker-4 held
    5 waiting cards, one it created itself, while worker-5 ran 3 of 10)."""
    from tools import kanban_tools as kt
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: "worker-2")
    monkeypatch.setattr(kp, "config", lambda: {"worker_queue": {"worker-2": POOL}})
    machines = list(_machines().values())
    wn.set_registry_fetch(lambda: {"me": "worker-2", "machines": seen_now(machines)})
    out = json.loads(kt._handle_create({"title": "summarise the failing CI jobs", "assignee": "default",
                                        "requires": []}))
    assert out.get("ok") and _runs_on(w2.conn, out["task_id"]) == [POOL], out
    out = json.loads(kt._handle_create({"title": "re-review PR 1 exact head", "assignee": "default", "requires": [],
                                        "placement": "worker-2"}))  # a review the land card on worker-2 must read
    assert out.get("ok") and _runs_on(w2.conn, out["task_id"]) is None, out
    out = json.loads(kt._handle_create({"title": "pinned", "assignee": "default", "requires": [],
                                        "runs_on": ["machine:worker-1"]}))
    assert out.get("ok") and _runs_on(w2.conn, out["task_id"]) == ["machine:worker-1"], out


def test_move_to_self_refuses_only_on_a_found_clean_tree(hub, tmp_path):
    _registry()
    ws = tmp_path / "ws"
    ws.mkdir()
    tid = kb.create_task(hub.conn, title="parked pool card", assignee="default", runs_on=[POOL])
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(ws), tid))
    move = dict(runs_on=["machine:worker-2"], reason="resume here", host_reason="my worktree is here",
                me="worker-2", machines=_machines())
    # No tree of the card on this host (it may sit elsewhere): the host reason stands.
    res = km.move_queued(hub.conn, tid, **move)
    assert res["ok"], res
    tid = kb.create_task(hub.conn, title="parked pool card 2", assignee="default", runs_on=[POOL])
    _pushed_clone(tmp_path, ws / "repo")
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(ws), tid))
    res = km.move_queued(hub.conn, tid, **move)
    assert not res["ok"] and res["reason_code"] == "pool_keeps_pushed", res  # found, clean and pushed
    _commit(ws / "repo", "b.txt")
    res = km.move_queued(hub.conn, tid, **move)
    assert res["ok"] and _moved(hub.conn, tid)[-1]["unpushed_state"].startswith("unpushed commits in "), res


# --- direct create_task producers (swarm, triage sweep, alert cards) -------------------------------------------

def test_direct_create_task_queues_a_portable_card_like_the_front_doors(hub, monkeypatch):
    """11-12 11:10Z: with ``worker_queue`` set, the old ``db_policy`` judged the queue decision but kept only its
    (absent) ``placement_policy``, so a direct ``kb.create_task`` card was neither queued nor pool-enrolled: it ran on
    the hub."""
    monkeypatch.setattr(kp, "config", lambda: {**CFG, "worker_pools": {HUB: ["worker-1", "worker-2"]}})
    _registry(held=("worker-1",))
    tid = kb.create_task(hub.conn, title="summarise the failing CI jobs", assignee="default", requires=[])
    assert _runs_on(hub.conn, tid) == [POOL]
    assert kb.get_task(hub.conn, tid).placement_policy is None  # queued, not also enrolled
    # Host work stays local and unqueued; an explicit pin and a checked front-door decision are kept as given.
    tid = kb.create_task(hub.conn, title="restart the gateway service on the hub", assignee="default", requires=[])
    assert _runs_on(hub.conn, tid) is None and kb.get_task(hub.conn, tid).placement_policy is None
    tid = kb.create_task(hub.conn, title="pinned", assignee="default", requires=[], runs_on=["machine:worker-2"])
    assert _runs_on(hub.conn, tid) == ["machine:worker-2"]
    tid = kb.create_task(hub.conn, title="summarise CI", assignee="default", requires=[], placement_checked=True)
    assert _runs_on(hub.conn, tid) is None


def test_direct_create_task_keeps_a_queue_miss_home_instead_of_pool_enrollment(hub, monkeypatch):
    """creation_decision on a worker_queue gateway never falls back to worker-first (MacBook t_01a1d5386cb55abf,
    11-13: the fallback reserved one machine at a time from a stale worker_pools list); a queue miss stays home."""
    monkeypatch.setattr(kp, "config", lambda: {**CFG, "worker_pools": {HUB: ["worker-1", "worker-2"]}})
    _registry(held=("worker-1", "worker-2"))  # no unheld machine carries the label
    tid = kb.create_task(hub.conn, title="summarise the failing CI jobs", assignee="default", requires=[])
    assert _runs_on(hub.conn, tid) is None and kb.get_task(hub.conn, tid).placement_policy is None
    _registry()
    sync.set_mode(hub.conn, "off")  # runs_on is a Board placement field: off a primary nothing queues
    tid = kb.create_task(hub.conn, title="summarise the failing CI jobs", assignee="default", requires=[])
    assert _runs_on(hub.conn, tid) is None and kb.get_task(hub.conn, tid).placement_policy is None
    monkeypatch.setattr(kp, "config", lambda: {"worker_pools": {HUB: ["worker-1", "worker-2"]}})  # no queue: enroll
    tid = kb.create_task(hub.conn, title="summarise the failing CI jobs", assignee="default", requires=[])
    assert kb.get_task(hub.conn, tid).placement_policy == "worker-first-v1"


# --- courier and origin-bound creates (t_01a1d0571c2f07d6) ------------------------------------------------------
# 11-12 12:40Z-15:16Z: after every gateway flipped to the queue, 7 of 8 new worker-first cards on the hub were
# routed (courier) creates the hub admission judge denied, and the 8th was origin-bound: both entries still took
# worker-first, which reserves one worker and writes runs_on [machine:worker-N].

def _intent(request_id):
    return {"request_id": request_id, "owner": {"gateway": HUB, "profile": "default", "board": "default"},
            "origin": {"gateway": "macbook", "profile": "default", "session": "s_mac"}}


def _created(conn, tid):
    return next(json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created'", (tid,)))


@pytest.fixture
def judge(monkeypatch):
    from tests.hermes_cli.hub_judge_support import FakeJudge
    from hermes_cli import kanban_hub_admission as admission
    fake = FakeJudge()
    monkeypatch.setattr(admission, "admit", fake)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_TASK", raising=False)
    return fake


@pytest.mark.parametrize("pools", [False, True])
def test_courier_create_denied_by_hub_admission_joins_the_pool_queue(hub, judge, monkeypatch, pools):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**CFG, **({"worker_pools": {HUB: ["worker-1"]}} if pools else {})})
    machines = [{**m, "labels": [*m["labels"], "capability:hub-write"]} if m["name"] == "worker-2" else m
                for m in _machines(held=("worker-1",)).values()]
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": seen_now(machines)})  # worker-2: hub-write
    judge.admit_next = False
    args = {"title": "Read ~/.hermes/logs/agent.log on the hub", "requires": [], "assignee": "default"}
    out = json.loads(kt._handle_create(dict(args), origin_intent=_intent(f"request-courier-{pools}")))
    tid = out.get("task_id")
    assert tid and len(judge.calls) == 1, out
    assert sorted(_runs_on(hub.conn, tid) or []) == ["capability:hub-write", POOL], out  # never machine:worker-N
    task = kb.get_task(hub.conn, tid)
    assert task.placement_policy is None and "hub:write" in (task.requires or [])
    created = _created(hub.conn, tid)
    assert created["placement_policy"] is None and created["hub_admission"]["entry"] == "courier", created
    # A re-applied request (lost reply) rebuilds the identical envelope: same card, no judge call, no conflict.
    again = json.loads(kt._handle_create(dict(args), origin_intent=_intent(f"request-courier-{pools}")))
    assert again.get("task_id") == tid and again.get("code") != "origin_creation_conflict", again
    assert len(judge.calls) == 1


def test_courier_create_keeps_worker_first_only_without_a_live_queue_label(hub, judge, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**CFG, "worker_pools": {HUB: ["worker-1"]}})
    _registry(held=("worker-1", "worker-2"))  # no unheld machine carries the label
    judge.admit_next = False
    out = json.loads(kt._handle_create({"title": "Read ~/.hermes/logs/agent.log on the hub", "requires": [],
                                        "assignee": "default"}, origin_intent=_intent("request-courier-held")))
    assert _runs_on(hub.conn, out["task_id"]) is None, out
    assert kb.get_task(hub.conn, out["task_id"]).placement_policy == "worker-first-v1"


def test_courier_create_refuses_with_neither_a_live_queue_label_nor_a_worker_pool(hub, judge, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: dict(CFG))  # no worker_pools entry for the hub
    _registry(held=("worker-1", "worker-2"))  # and no unheld machine carries the queue label
    judge.admit_next = False
    out = json.loads(kt._handle_create({"title": "Read ~/.hermes/logs/agent.log on the hub", "requires": [],
                                        "assignee": "default"}, origin_intent=_intent("request-courier-nowhere")))
    assert out.get("code") == "placement_unavailable" and not out.get("created"), out


def test_courier_explicit_machine_pin_still_wins(hub, judge, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**CFG, "worker_pools": {HUB: ["worker-1"]}})
    _registry()
    judge.admit_next = False
    out = json.loads(kt._handle_create({"title": "summarise CI", "requires": [], "assignee": "default",
                                        "runs_on": ["machine:worker-2"]}, origin_intent=_intent("request-pinned")))
    assert _runs_on(hub.conn, out["task_id"]) == ["machine:worker-2"], out
    assert judge.calls == [] and kb.get_task(hub.conn, out["task_id"]).placement_policy is None


@pytest.mark.parametrize("extra", [{"runs_on": [f"machine:{HUB}"]}, {"workspace_kind": "dir", "workspace_path": "/srv/x"}])
def test_courier_create_pinned_to_the_hub_is_refused_when_denied(hub, judge, monkeypatch, extra):
    # A denied hub pin (or a host workspace) cannot move to the queue: the creator re-places it; no card is made.
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**CFG, "worker_pools": {HUB: ["worker-1"]}})
    _registry()
    judge.admit_next = False
    before = hub.conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    out = json.loads(kt._handle_create({"title": "Read ~/.hermes/logs/agent.log on the hub", "requires": [],
                                        "assignee": "default", **extra},
                                       origin_intent=_intent(f"request-pinned-deny-{sorted(extra)[0]}")))
    assert out.get("code") == "placement_unavailable" and not out.get("created"), out
    assert len(judge.calls) == 1
    assert hub.conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before


@pytest.fixture
def origin_ok(monkeypatch):
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: (HUB, {HUB, "macbook"}))
    monkeypatch.setattr(kanban_origin, "bind", lambda conn, tid, origin, board=None: {"origin": origin.ref})


@pytest.mark.parametrize("pools", [False, True])
def test_origin_bound_tool_create_joins_the_pool_queue(hub, origin_ok, monkeypatch, pools):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**CFG, **({"worker_pools": {HUB: ["worker-1"]}} if pools else {})})
    _registry(held=("worker-1",))
    out = json.loads(kt._handle_create({"title": "summarise the failing CI jobs", "assignee": "default",
                                        "requires": [], "origin_session": "macbook:abc123"}))
    assert out.get("ok") and _runs_on(hub.conn, out["task_id"]) == [POOL], out
    assert kb.get_task(hub.conn, out["task_id"]).placement_policy is None
    assert _created(hub.conn, out["task_id"])["placement_policy"] is None


def test_origin_bound_explicit_pins_still_win(hub, origin_ok, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**CFG, "worker_pools": {HUB: ["worker-1"]}})
    _registry()
    base = {"title": "summarise the failing CI jobs", "assignee": "default", "requires": [],
            "origin_session": "macbook:abc123"}
    out = json.loads(kt._handle_create({**base, "runs_on": ["machine:worker-2"]}))
    assert out.get("ok") and _runs_on(hub.conn, out["task_id"]) == ["machine:worker-2"], out
    out = json.loads(kt._handle_create({**base, "placement": "local", "title": "summarise CI here"}))
    assert out.get("ok") and _runs_on(hub.conn, out["task_id"]) is None, out
    assert kb.get_task(hub.conn, out["task_id"]).placement_policy is None


def test_queue_decision_does_not_exclude_courier_creation():
    from hermes_cli import kanban_worker_placement as wp
    out = wp.creation_decision({"title": "summarise CI"}, CFG, HUB, "auto", {"request_id": "r"}, lambda _label: True)
    assert out is not None and out["runs_on"] == [POOL], out


def test_a_callers_runs_on_beats_a_queued_landing(monkeypatch):
    from tools import kanban_tools_placement as ktp
    queued = {"where": "local", "runs_on": [POOL]}
    monkeypatch.setattr(ktp, "_origin_enrollment", lambda *a, **kw: queued)
    monkeypatch.setattr(ktp, "_courier_landing", lambda conn, args, intent, marker, runs_on, **kw: marker)
    assert ktp._landing_placement(None, {}, object(), None, None, ["machine:worker-2"], title="t") == (
        queued, ["machine:worker-2"])
    assert ktp._landing_placement(None, {}, object(), None, None, None, title="t") == (queued, [POOL])


# --- local markers a pool worker cannot meet (t_01a1d2662553185d) ----------------------------------------------
# 11-13: on a pool worker a built-in local-marker hit (Keychain, Chrome, a Desktop chat, a ship review's CI line
# naming the macOS check) kept the card as a local row only its creating worker could claim (workers pick up only
# runs_on cards). No Board machine a card can lease runs macOS, so the creator was never better than the pool.
# Observed: t_01a1d2434cf3676c, and t_01a1d2662553185d itself (its spec names Chrome).

SHIP_TITLE = "Independent exact-head review PR #1789 @370a13d80c (core-wt ship review required)"
SHIP_BODY = (
    "Independent exact-head review of https://github.com/softmax-universe/daveey-hermes/pull/1789 at "
    "**370a13d80c0000000000000000000000000000ab**.\n"
    "CI at filing: green: All required checks pass (v2): success; macOS state required (v1): success.\n"
    "Required CI passed before filing.\n\n"
    "An independent exact-head review was explicitly requested before landing.\n"
    "The ship gate lands only a head approved at its exact sha. This card was filed by ship; the landing session "
    "must not be the one reviewing.\n\n"
    "Finish with kanban_complete metadata exactly:\n"
    '  {"verdict": "approve" | "request_changes", "reviewed_sha": "370a13d80c0000000000000000000000000000ab", '
    '"reviewed_pr": "https://github.com/softmax-universe/daveey-hermes/pull/1789"}\n')


def _pool_machines(*, held=()):
    return {m["name"]: {**m, "hold": m["name"] in held} for m in (MACHINES[HUB], W1, W2, MACHINES["macbook"])}


def _on(monkeypatch, gateway, cfg, machines):
    from hermes_cli import kanban_skill_check
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: gateway)
    monkeypatch.setattr(kp, "config", lambda: cfg)
    monkeypatch.setattr(kanban_skill_check, "check_task_skills", lambda *a, **k: None)  # no skills dir here
    wn.set_registry_fetch(lambda: {"me": gateway, "machines": seen_now(machines.values())})


@pytest.fixture
def on_w2(w2, monkeypatch):
    _on(monkeypatch, "worker-2", {"worker_queue": {"worker-2": POOL}}, _pool_machines())
    return w2


def _create(**card):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"assignee": "default", "requires": [], **card}))
    assert out.get("ok"), out
    return out


def test_a_keychain_card_from_a_pool_worker_joins_the_pool_with_the_mac_reach_label(on_w2):
    out = _create(title="rotate the CI token in the Keychain")
    # The Board stores labels normalized (sorted): the label set is the contract; the reply keeps the queue first.
    assert _runs_on(on_w2.conn, out["task_id"]) == sorted([POOL, TAILNET]), out
    assert out["placement"]["runs_on"] == [POOL, TAILNET], out
    assert "tailnet_exec" in out["note"] and out["placement"]["note"] == out["note"], out
    assert "remedy" not in out, out
    why = out["placement"]["why"]
    assert why.startswith("portable: queued for any machine labelled pool:worker"), why
    assert why.endswith("(local marker 'keychain' not met on worker-2)"), why
    # Any pool worker may claim it, not only the creator.
    card = dict(on_w2.conn.execute("SELECT * FROM tasks WHERE id = ?", (out["task_id"],)).fetchone())
    assert wn.eligible(card, "worker-1", _pool_machines()) is None
    # The audit stays truthful: the card did hit a local marker.
    assert _created(on_w2.conn, out["task_id"])["portable_reason"] == "local marker"


def test_a_chrome_card_from_a_pool_worker_joins_the_pool_and_names_the_hub_route(on_w2):
    out = _create(title="read the dashboard in Chrome")
    assert _runs_on(on_w2.conn, out["task_id"]) == [POOL], out
    assert "machine:hub" in out["note"] and "hub_reason" in out["note"], out


def test_a_desktop_chat_card_from_a_pool_worker_carries_the_mac_reach_label(on_w2):
    out = _create(title="Fix Desktop chat sidebar ordering")
    assert TAILNET in (_runs_on(on_w2.conn, out["task_id"]) or []), out


def test_a_device_skill_card_from_a_pool_worker_carries_the_mac_reach_label(on_w2):
    out = _create(title="Click through the Settings pane", skills=["computer-use"])
    assert TAILNET in (_runs_on(on_w2.conn, out["task_id"]) or []), out


def test_a_ship_review_card_naming_the_macos_check_joins_the_pool(on_w2):
    out = _create(title=SHIP_TITLE, body=SHIP_BODY)
    assert POOL in (_runs_on(on_w2.conn, out["task_id"]) or []), out


def test_cli_create_of_a_keychain_card_on_a_pool_worker_joins_the_pool(on_w2):
    from hermes_cli import kanban as kcli
    out = kcli.run_slash('create "rotate the Keychain token" --assignee default --json')
    assert _runs_on(on_w2.conn, json.loads(out)["id"]) == sorted([POOL, TAILNET]), out


def test_direct_create_task_of_a_keychain_card_on_a_pool_worker_joins_the_pool(on_w2):
    tid = kb.create_task(on_w2.conn, title="rotate the CI token in the Keychain", assignee="default", requires=[])
    assert _runs_on(on_w2.conn, tid) == sorted([POOL, TAILNET])


def _no_live_tailnet_pool_worker(kind):
    # worker-1/2: linux + pool:worker, no tailnet attestation (the live-set fixture labels minus TAILNET)
    machines = {name: {**m, "labels": [x for x in m["labels"] if x != TAILNET]} for name, m in _machines().items()}
    if kind == "held":
        machines["worker-1"] = {**machines["worker-1"], "labels": ["linux", POOL, TAILNET], "hold": True}
    elif kind == "split":  # a live machine has the tailnet label but not the queue label
        machines["worker-5"] = {**W1, "name": "worker-5", "labels": ["linux", TAILNET]}
    return machines


# 11-13 review nit (t_01a1d28879d22917): queue_decision checked only that the queue label is live, so a Mac-group
# card queued as [pool:worker, capability:tailnet-exec] even when no live machine holds both: nobody could claim it.
@pytest.mark.parametrize("kind", ["none", "held", "split"])
def test_pool_worker_keeps_a_mac_marker_card_when_no_live_machine_holds_every_label(w2, monkeypatch, kind):
    _on(monkeypatch, "worker-2", {"worker_queue": {"worker-2": POOL}}, _no_live_tailnet_pool_worker(kind))
    out = _create(title="rotate the CI token in the Keychain")
    assert _runs_on(w2.conn, out["task_id"]) is None and out["placement"]["why"].startswith("local marker"), out
    # A group that adds no label still queues: only the unmet label set falls back.
    out = _create(title="read the dashboard in Chrome")
    assert _runs_on(w2.conn, out["task_id"]) == [POOL], out


HUB_WRITE = "capability:hub-write"
# worker_pools names worker-5: portability attests coordination labels on those members, not on the queue's machines.
POOLS_CFG = {**CFG, "worker_pools": {HUB: ["worker-5"]}}


def _hub_write_registry(kind):
    # worker-1/2: linux + pool:worker, no hub-write attestation (the live-set fixture labels minus HUB_WRITE)
    machines = {name: {**m, "labels": [x for x in m["labels"] if x != HUB_WRITE]} for name, m in _machines().items()}
    machines["worker-5"] = {**W1, "name": "worker-5", "labels": ["linux", HUB_WRITE]}  # no queue label
    if kind == "held":
        machines["worker-1"] = {**machines["worker-1"], "labels": ["linux", POOL, HUB_WRITE], "hold": True}
    elif kind == "joint":
        machines["worker-1"] = {**machines["worker-1"], "labels": ["linux", POOL, HUB_WRITE]}
    seen = int(time.time() * 1000)  # coordination attestation only counts fresh members
    listed = [{**m, "last_seen": seen} for m in machines.values()]
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": listed})


# 11-13 review nit 3 (t_01a1d38bd571fd44): with no local-marker hit, queue_decision checked only the queue label, so
# a card whose coordination label the attested pool members hold, but no live queue machine also holds, queued where
# nobody could claim it.
@pytest.mark.parametrize("kind", ["none", "held", "joint"])
def test_no_marker_card_queues_only_when_one_live_machine_holds_queue_and_coordination_labels(hub, kind):
    from hermes_cli import kanban_worker_placement as wp
    _hub_write_registry(kind)
    args = {"title": "summarise CI", "requires": ["hub:write"], "assignee": "default"}
    assert kp.portable_reason(args, POOLS_CFG) is None  # worker-5 attests hub-write: portable
    out = wp.queue_decision(args, POOLS_CFG, HUB, "")
    if kind == "joint":
        assert out is not None and out["runs_on"] == [POOL, HUB_WRITE], out
    else:
        assert out is None, out  # today's local keep (worker-first enrollment) instead of an unclaimable queue
    # A card with no coordination label still queues on the queue label alone.
    plain = wp.queue_decision({"title": "summarise CI", "assignee": "default"}, POOLS_CFG, HUB, "")
    assert plain is not None and plain["runs_on"] == [POOL], plain


# Review nit 1: the injected machine_ok seam is the one liveness source for the whole label set, on both paths.
def test_machine_ok_seam_judges_the_whole_label_set_on_both_paths(w2, monkeypatch):
    from hermes_cli import kanban_worker_placement as wp
    seen = []

    def ok(labels):
        seen.append(labels)
        return True
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    _hub_write_registry("none")  # the real registry has no joint holder; the seam says there is one
    out = wp.queue_decision({"title": "summarise CI", "requires": ["hub:write"]}, POOLS_CFG, HUB, "", machine_ok=ok)
    assert out is not None and out["runs_on"] == [POOL, HUB_WRITE] and seen == [(POOL, HUB_WRITE)], (out, seen)
    seen.clear()
    w2_cfg = {"worker_queue": {"worker-2": POOL}}
    _on(monkeypatch, "worker-2", w2_cfg, _no_live_tailnet_pool_worker("none"))
    keychain = {"title": "rotate the CI token in the Keychain"}
    out = wp.queue_decision(keychain, w2_cfg, "worker-2", "", machine_ok=ok)
    assert out is not None and out["runs_on"] == [POOL, TAILNET] and seen == [(POOL, TAILNET)], (out, seen)
    assert wp.queue_decision(keychain, w2_cfg, "worker-2", "", machine_ok=lambda _labels: False) is None


# The seam stays the one liveness source on the home-marker path too: with every registry pool member held, an
# injected machine_ok that sees a joint holder still queues the card (no separate bare-label registry check).
def test_machine_ok_seam_alone_judges_liveness_for_a_home_marker_card(w2, monkeypatch):
    from hermes_cli import kanban_worker_placement as wp
    seen = []

    def ok(labels):
        seen.append(labels)
        return True
    w2_cfg = {"worker_queue": {"worker-2": POOL}}
    _on(monkeypatch, "worker-2", w2_cfg, _machines(held=("worker-1", "worker-2")))
    card = {"title": "Fix the hermes-sync drain window parse"}
    out = wp.queue_decision(card, w2_cfg, "worker-2", "", machine_ok=ok)
    assert out is not None and out["runs_on"] == [POOL, HUB_WRITE] and seen == [(POOL, HUB_WRITE)], (out, seen)
    assert wp.queue_decision(card, w2_cfg, "worker-2", "", machine_ok=lambda _labels: False) is None


def test_every_built_in_local_marker_has_a_group():
    assert set(kp.DEFAULT_LOCAL_MARKERS) | set(kp.DEFAULT_LOCAL_SKILL_MARKERS) <= set(kp.LOCAL_MARKER_GROUPS)
    assert set(kp.LOCAL_MARKER_GROUPS.values()) <= {"mac", "desktop", "browser", "sso", "context"}


def test_markers_are_unmeetable_only_on_a_linux_non_hub_registry_entry():
    machines = _pool_machines()
    assert kp.markers_unmeetable_here("worker-2", {}, machines) is True
    assert kp.markers_unmeetable_here(HUB, {}, machines) is False  # linux, but the hub (admission judge path)
    assert kp.markers_unmeetable_here("macbook", {}, machines) is False
    assert kp.markers_unmeetable_here("worker-9", {}, machines) is False  # no registry entry
    assert kp.markers_unmeetable_here(None, {}, machines) is False


# Controls: green before and after.

def test_hub_keychain_card_still_asks_the_admission_judge(hub, judge):
    from tools import kanban_tools as kt
    _registry()
    out = json.loads(kt._handle_create({"title": "rotate the CI token in the Keychain", "assignee": "default",
                                        "requires": []}))
    assert out.get("task_id") and len(judge.calls) == 1, out


def test_macbook_keychain_card_stays_local(w2, monkeypatch):
    _on(monkeypatch, "macbook", {"route": {"macbook": HUB}, "worker_queue": {"macbook": POOL}}, _pool_machines())
    d = kp.decide({"title": "rotate the CI token in the Keychain"}, live=lambda _t: None, carries=True)
    assert d["where"] == "local" and "runs_on" not in d and d["why"].startswith("local marker"), d


def test_pool_worker_keeps_a_marker_card_when_the_registry_is_unreadable(w2, monkeypatch):
    _on(monkeypatch, "worker-2", {"worker_queue": {"worker-2": POOL}}, _pool_machines())

    def down():
        raise OSError("board unreachable")
    wn.set_registry_fetch(down)
    out = _create(title="rotate the CI token in the Keychain")
    assert _runs_on(w2.conn, out["task_id"]) is None and out["placement"]["why"].startswith("local marker"), out


@pytest.mark.parametrize("cfg,title", [
    ({"extra_local_markers": [r"\bgrafana\b"]}, "fix the grafana panel"),
    ({"extra_local_markers": [r"\bgrafana\b"]}, "rotate the Keychain token the grafana panel reads"),  # + built-in
    ({"local_markers": [r"\bkeychain\b"]}, "rotate the CI token in the Keychain"),  # a replaced list
])
def test_pool_worker_keeps_an_operator_marker(w2, monkeypatch, cfg, title):
    _on(monkeypatch, "worker-2", {"worker_queue": {"worker-2": POOL}, **cfg}, _pool_machines())
    out = _create(title=title)
    assert _runs_on(w2.conn, out["task_id"]) is None and out["placement"]["why"].startswith("local marker"), out


@pytest.mark.parametrize("card", [
    # "systemctl restart x" was a keep here until t_01a1d2856e5fec44: a home-gateway marker on a pool worker now
    # joins the pool with capability:hub-write (test_systemctl_on_a_pool_worker_joins_the_pool_with_hub_write).
    {"title": "rotate the CI token in the Keychain", "placement": "local"},
    {"title": "rotate the CI token in the Keychain", "workspace_kind": "dir", "workspace_path": "/srv/x"},
    {"title": "fix the hermes-sync drain", "placement": "local"},
    {"title": "fix the hermes-sync drain", "workspace_kind": "dir", "workspace_path": "/srv/x"},
])
def test_pool_worker_leaves_other_keeps_unchanged(on_w2, card):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"assignee": "default", "requires": [], **card}))
    assert out.get("task_id") and _runs_on(on_w2.conn, out["task_id"]) is None, out


# --- home-gateway markers on a pool worker (t_01a1d2856e5fec44) -------------------------------------------------
# 11-13: in 24 h, 473 worker-created cards hit a home-gateway marker (a ~/.hermes path, a _wt path, hermes-sync,
# "on the hub", cron, ssh, ...) and stayed local rows only their creating worker could claim. Observed:
# t_01a1d264e2d198d5 ('on the hub', worker-4), t_01a1ce17ce25fbc7 (_wt path, worker-2), t_01a1d259636f336f
# (~/.hermes/kanban path, worker-5). Every pool worker reaches hub state with hub-admin (capability:hub-write), so
# only this worker's own unpushed tree or files, 'this host' and operator markers keep a card on its creator.

def _home_marker(conn, tid):
    return _created(conn, tid).get("home_marker")


def _queued(conn, out, labels):
    assert out.get("ok"), out
    assert _runs_on(conn, out["task_id"]) == sorted([POOL, *labels]), out
    assert out["placement"]["runs_on"] == [POOL, *sorted(labels)], out
    assert out["placement"]["why"].startswith("portable: queued for any machine labelled pool:worker"), out
    assert "(home gateway marker " in out["placement"]["why"], out
    assert out["placement"].get("note") == out.get("note") and out.get("note"), out
    card = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (out["task_id"],)).fetchone())
    assert wn.eligible(card, "worker-1", _pool_machines()) is None  # any pool worker may claim it
    assert _created(conn, out["task_id"])["portable_reason"] == "home gateway marker"  # the audit stays truthful
    return out


def _kept(conn, out):
    assert out.get("ok") and _runs_on(conn, out["task_id"]) is None, out
    assert out["placement"]["why"].startswith("home gateway marker: "), out
    assert out.get("note", "").startswith("kept on worker-2: "), out
    assert out["placement"].get("note") == out["note"], out
    assert _home_marker(conn, out["task_id"])["decision"] == "keep", out
    return out


def test_a_hermes_sync_card_from_a_pool_worker_joins_the_pool_with_hub_write(on_w2):  # (a)
    out = _queued(on_w2.conn, _create(title="Fix the hermes-sync drain window parse"), [HUB_WRITE])
    assert "hub-admin" in out["note"] and "worker-2" in out["note"], out
    assert _home_marker(on_w2.conn, out["task_id"]) == {"decision": "queue", "class": "b", "hit": "hermes-sync"}


def test_an_on_the_hub_kanban_db_card_from_a_pool_worker_joins_the_pool_with_hub_write(on_w2):  # (b)
    _queued(on_w2.conn, _create(title="Investigate the stall on the hub; read the hub kanban.db"), [HUB_WRITE])


def test_systemctl_on_a_pool_worker_joins_the_pool_with_hub_write(on_w2):  # (c), reverses the sibling's control
    _queued(on_w2.conn, _create(title="systemctl " + "restart x"), [HUB_WRITE])


def test_an_ssh_card_from_a_pool_worker_joins_the_pool_with_no_extra_label(on_w2):  # (d)
    out = _queued(on_w2.conn, _create(title="use ssh to pull the CI box log"), [])
    assert "every pool worker carries" in out["note"], out
    assert _home_marker(on_w2.conn, out["task_id"])["class"] == "d"


def test_a_review_naming_a_clean_pushed_wt_tree_joins_the_pool(on_w2, tmp_path):  # (e)
    tree = _pushed_clone(tmp_path, tmp_path / "code" / "_wt" / "pool-card")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree, capture_output=True, text=True,
                          check=True).stdout.strip()
    out = _queued(on_w2.conn, _create(
        title="Independent exact-head review PR #1900",
        body=(f"Review https://github.com/softmax-universe/daveey-hermes/pull/1900 at {head}.\n"
              "The implementer's tree is ~/code/_wt/pool-card (read-only; fetch the PR head into your own tree).")),
        [])
    assert "~/code/_wt/pool-card is not unpublished state on worker-2" in out["note"], out
    assert _home_marker(on_w2.conn, out["task_id"])["class"] == "worktree-not-here"


def test_a_wt_path_absent_on_this_worker_joins_the_pool(on_w2):  # (f)
    _queued(on_w2.conn, _create(title="Re-run the tests in ~/code/_wt/gone"), [])


def test_an_evidence_path_absent_on_this_worker_joins_the_pool_with_hub_write(on_w2):  # (g)
    out = _queued(on_w2.conn, _create(title="read ~/.hermes/kanban/evidence/t_0000000000000001/report.md"),
                  [HUB_WRITE])
    assert _home_marker(on_w2.conn, out["task_id"])["class"] == "artifact-not-here"


def test_cli_create_of_a_hermes_sync_card_on_a_pool_worker_joins_the_pool(on_w2):  # (h)
    from hermes_cli import kanban as kcli
    out = kcli.run_slash('create "fix the hermes-sync drain" --assignee default --json')
    assert _runs_on(on_w2.conn, json.loads(out)["id"]) == sorted([POOL, HUB_WRITE]), out


def test_direct_create_task_of_a_hermes_sync_card_on_a_pool_worker_joins_the_pool(on_w2):
    tid = kb.create_task(on_w2.conn, title="fix the hermes-sync drain", assignee="default", requires=[])
    assert _runs_on(on_w2.conn, tid) == sorted([POOL, HUB_WRITE])


def test_a_keychain_and_systemctl_card_carries_both_reach_labels(on_w2):  # (i)
    out = _create(title="rotate the Keychain token, then restart it via systemctl")
    assert _runs_on(on_w2.conn, out["task_id"]) == sorted([POOL, HUB_WRITE, TAILNET]), out
    assert "tailnet_exec" in out["note"] and "hub-admin" in out["note"], out


def test_every_home_gateway_marker_has_a_class():
    assert set(kp.HOME_MARKER_CLASSES) == set(kp.HOME_GATEWAY_MARKERS)
    assert set(kp.HOME_MARKER_CLASSES.values()) <= {"worktree", "state-path", "a", "b", "c", "d"}
    assert kp.HOME_MARKER_CLASSES[r"\bthis host\b"] == "a"
    assert kp.HOME_CLASS_LABELS == {"b": HUB_WRITE, "c": TAILNET}


def test_home_marker_hits_lists_every_hit_and_tags_operator_patterns():
    from hermes_cli.kanban_placement_diagnostics import home_marker_hits
    hits = home_marker_hits({"title": "on the hub, fix the grafana panel via ssh"},
                            {"extra_home_markers": [r"\bgrafana\b"]})
    assert [(p, m.group(0)) for p, m in hits] == [
        (r"\bon the hub\b", "on the hub"), (r"\bssh\b", "ssh"), ("operator", "grafana")]


# Controls: unchanged before and after.

# What a MacBook (routed to the hub) decides for a home-gateway marker at base 141bcec62a: home markers bind only a
# routeless gateway, so it routes to the hub. This card changes no routed gateway.
MACBOOK_HOME_DECISION = {"where": "hub", "why": "no local-only need"}


def test_hub_hermes_sync_card_still_asks_the_admission_judge(hub, judge):
    from tools import kanban_tools as kt
    _registry()
    out = json.loads(kt._handle_create({"title": "fix hermes-sync on the hub", "assignee": "default",
                                        "requires": []}))
    assert out.get("task_id") and len(judge.calls) == 1, out
    assert "home_marker" not in _created(hub.conn, out["task_id"])  # the hub is not a worker gateway


def test_macbook_hermes_sync_card_is_unchanged(w2, monkeypatch):
    _on(monkeypatch, "macbook", {"route": {"macbook": HUB}, "worker_queue": {"macbook": POOL}}, _pool_machines())
    d = kp.decide({"title": "fix hermes-sync on the hub"}, live=lambda _t: None, carries=True)
    assert d == MACBOOK_HOME_DECISION, d


def test_pool_worker_keeps_a_home_marker_card_when_the_registry_is_unreadable(w2, monkeypatch):
    _on(monkeypatch, "worker-2", {"worker_queue": {"worker-2": POOL}}, _pool_machines())

    def down():
        raise OSError("board unreachable")
    wn.set_registry_fetch(down)
    out = _create(title="fix the hermes-sync drain")
    assert _runs_on(w2.conn, out["task_id"]) is None, out
    assert out["placement"]["why"].startswith("home gateway marker: "), out
    # The live check lists these: a worker kept a home-marker card only because it could not test its gateway.
    assert _home_marker(w2.conn, out["task_id"]) == {
        "decision": "keep", "class": "gateway-test-failed", "hit": "hermes-sync"}


def test_pool_worker_keeps_a_home_marker_card_when_every_pool_member_is_held(w2, monkeypatch):
    _on(monkeypatch, "worker-2", {"worker_queue": {"worker-2": POOL}},
        _pool_machines(held=("worker-1", "worker-2")))
    out = _create(title="fix the hermes-sync drain")
    assert _runs_on(w2.conn, out["task_id"]) is None, out
    assert out["placement"]["why"].startswith("home gateway marker: "), out
    assert _home_marker(w2.conn, out["task_id"])["class"] == "gateway-test-failed"


def test_pool_worker_keeps_a_home_marker_card_no_live_machine_can_claim(w2, monkeypatch):
    """No live pool member attests capability:hub-write: the (b) card keeps the legacy local keep (cfc3d681ce), and
    its audit says the gateway could not queue it, not that it queued."""
    machines = {name: {**m, "labels": [x for x in m["labels"] if x != HUB_WRITE]} for name, m in _machines().items()}
    _on(monkeypatch, "worker-2", {"worker_queue": {"worker-2": POOL}}, machines)
    out = _create(title="fix the hermes-sync drain")
    assert _runs_on(w2.conn, out["task_id"]) is None, out
    assert out["placement"]["why"].startswith("home gateway marker: "), out
    assert _home_marker(w2.conn, out["task_id"]) == {
        "decision": "keep", "class": "gateway-test-failed", "hit": "hermes-sync"}


def _refused_mandatory_create(conn):
    from hermes_cli.kanban_model_neutral import ERROR
    from tools import kanban_tools as kt
    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    out = json.loads(kt._handle_create({"assignee": "default", "requires": [],
                                        "title": "rotate the CI token in the Keychain",
                                        "model": "claude-opus-5-5", "provider": "anthropic",
                                        "pin_mandatory": True}))
    assert out.get("error") == ERROR and not out.get("ok"), out
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before  # refused before any card or placement


def test_a_marker_card_mandatory_pin_is_refused_not_kept(on_w2):
    """Mandatory model pins are no longer a portability reason (nor a creatable one): ingress refuses the card
    before placement, so no pool-member label attestation can matter."""
    _refused_mandatory_create(on_w2.conn)


def test_a_marker_card_mandatory_pin_is_refused_whatever_the_pool_attests(w2, monkeypatch):
    """worker-1 alone attests the legacy pin label; ingress still refuses the pin, so the pool never decides."""
    from hermes_cli.kanban_mandatory_pin import LABEL
    machines = _pool_machines()
    machines["worker-1"] = {**machines["worker-1"], "labels": [*LIVE, LABEL]}
    _on(monkeypatch, "worker-2", {"worker_queue": {"worker-2": POOL}}, machines)
    _refused_mandatory_create(w2.conn)


def test_a_worker_keeps_a_card_naming_a_non_git_directory_under_wt(on_w2, tmp_path):
    """A named ``_wt`` root that exists here but is not a git tree fails toward keep."""
    (tmp_path / "code" / "_wt" / "plain").mkdir(parents=True)
    out = _kept(on_w2.conn, _create(title="copy the logs out of ~/code/_wt/plain"))
    assert f"{tmp_path / 'code' / '_wt' / 'plain'} exists only on this worker" in out["note"], out


def test_a_worker_keeps_a_card_naming_more_trees_than_the_probe_budget(on_w2, tmp_path):
    """Only MAX_TREES trees are probed per create; a further existing tree is kept, never assumed pushed."""
    from hermes_cli import kanban_home_marker_placement as khm
    names = [f"t{i}" for i in range(khm.MAX_TREES + 1)]
    for name in names:
        _pushed_clone(tmp_path, tmp_path / "code" / "_wt" / name)
    out = _kept(on_w2.conn, _create(title="compare " + ", ".join(f"~/code/_wt/{n}" for n in names)))
    assert "was not checked in the probe budget" in out["note"], out


def test_a_create_probe_bounds_every_git_call_by_its_budget(on_w2, tmp_path, monkeypatch):
    """The create's tree probe hands its remaining PROBE_BUDGET_S to ``tree_state``: every git call it makes
    (dirty, unpushed, ls-remote) times out within that budget, not the move guard's longer default."""
    from hermes_cli import kanban_home_marker_placement as khm, worktree_ops
    tree = _pushed_clone(tmp_path, tmp_path / "code" / "_wt" / "bounded")
    _commit(tree, "b.txt")
    original, timeouts = worktree_ops._git_out, []

    def observe(args, cwd, **kwargs):
        timeouts.append((args[0], kwargs.get("timeout")))
        return original(args, cwd, **kwargs)
    monkeypatch.setattr(worktree_ops, "_git_out", observe)
    out = _kept(on_w2.conn, _create(title="finish the fix in ~/code/_wt/bounded"))
    assert f"unpushed commits in {tree}" in out["note"], out
    assert "ls-remote" in [c for c, _ in timeouts], timeouts
    assert all(t is not None and 0 < t <= khm.PROBE_BUDGET_S for _, t in timeouts), timeouts


def test_a_worker_resolves_its_own_home_spelled_out(on_w2, tmp_path):
    """``/home/<this user>/...`` names this worker's home just like ``~``."""
    tree = _pushed_clone(tmp_path, tmp_path / "code" / "_wt" / "spelled")
    _commit(tree, "b.txt")
    out = _kept(on_w2.conn, _create(title=f"finish the fix in /home/{tmp_path.name}/code/_wt/spelled"))
    assert f"unpushed commits in {tree}" in out["note"], out


@pytest.mark.parametrize("state", ["unpushed", "dirty"])
def test_a_worker_keeps_a_card_naming_its_own_unpublished_tree(on_w2, tmp_path, state):
    tree = _pushed_clone(tmp_path, tmp_path / "code" / "_wt" / "held-card")
    if state == "unpushed":
        _commit(tree, "b.txt")
    else:
        (tree / "wip.txt").write_text("not committed\n")
    out = _kept(on_w2.conn, _create(title="finish the parser fix in ~/code/_wt/held-card"))
    word = "unpushed commits in" if state == "unpushed" else "dirty worktree"
    assert f"{word} {tree}" in out["note"], out
    assert _home_marker(on_w2.conn, out["task_id"])["class"] == "a-worktree"


@pytest.mark.parametrize("home", ["~", "$HOME", "${HOME}"])
def test_a_worker_keeps_a_card_naming_its_own_artifact(on_w2, tmp_path, home):
    log = tmp_path / ".hermes" / "kanban" / "workspaces" / "t_0000000000000002" / "log.txt"
    log.parent.mkdir(parents=True)
    log.write_text("x\n")
    out = _kept(on_w2.conn, _create(title=f"read {home}/.hermes/kanban/workspaces/t_0000000000000002/log.txt"))
    assert "exists only on this worker" in out["note"], out
    assert _home_marker(on_w2.conn, out["task_id"])["class"] == "a-artifact"


@pytest.mark.parametrize("title", [
    "read $HOME/.hermes/kanban/workspaces/t_0000000000000003/log.txt",
    'read "${HOME}"/.hermes/kanban/workspaces/t_0000000000000003/log.txt',
    "cd ~ && cat .hermes/kanban/workspaces/t_0000000000000003/log.txt",
])
def test_a_worker_keeps_a_card_naming_its_own_artifact_by_a_home_spelling(on_w2, tmp_path, title):
    """``$HOME`` and a home-relative path after a cd home probe the same artifact (review 5894942332 nit)."""
    log = tmp_path / ".hermes" / "kanban" / "workspaces" / "t_0000000000000003" / "log.txt"
    log.parent.mkdir(parents=True)
    log.write_text("x\n")
    out = _kept(on_w2.conn, _create(title=title))
    assert "exists only on this worker" in out["note"], out
    assert _home_marker(on_w2.conn, out["task_id"])["class"] == "a-artifact"


def test_a_worker_reads_every_home_relative_hit_as_its_home_path(on_w2, tmp_path):
    """Every relative ``.hermes/<p>`` after a cd home reads as ``~/.hermes/<p>``, not only the first one (post-merge
    mutants M8/M8b on PR #2585): the first span names hub state, the second this worker's own artifact, which keeps."""
    log = tmp_path / ".hermes" / "kanban" / "workspaces" / "t_0000000000000005" / "log.txt"
    log.parent.mkdir(parents=True)
    log.write_text("x\n")
    out = _kept(on_w2.conn, _create(
        title="cd ~ && cat .hermes/.env; cd ~ && cat .hermes/kanban/workspaces/t_0000000000000005/log.txt"))
    assert "exists only on this worker" in out["note"], out
    marker = _home_marker(on_w2.conn, out["task_id"])
    assert marker["class"] == "a-artifact", marker
    assert marker["hit"] == "~/.hermes/kanban/workspaces/t_0000000000000005/log.txt", marker


def test_a_repo_relative_hermes_after_a_later_cd_is_no_home_marker(on_w2, tmp_path):
    """A later cd elsewhere ends the home-relative span (PR #2463 review 5895947424 nit): the repo's own
    ``.hermes/kanban/...`` is not read as this worker's ``~/.hermes/kanban/...`` artifact."""
    log = tmp_path / ".hermes" / "kanban" / "workspaces" / "t_0000000000000004" / "log.txt"
    log.parent.mkdir(parents=True)
    log.write_text("x\n")
    out = _create(title="cd ~ && git clone x repo && cd repo && cat .hermes/kanban/workspaces/t_0000000000000004/log")
    assert out.get("ok"), out
    assert _home_marker(on_w2.conn, out["task_id"]) is None, out


@pytest.mark.parametrize("title", ['pushd "$HOME/.hermes" && cat .env', "cd $HOME/.hermes && cat .env"])
def test_the_state_directory_itself_joins_the_pool_with_hub_write(on_w2, title):
    """A cd into ``~/.hermes`` names hub state (PR #2463 review 5895947424 nit)."""
    out = _queued(on_w2.conn, _create(title=title), [HUB_WRITE])
    assert _home_marker(on_w2.conn, out["task_id"])["class"] == "b", out


def test_a_worker_keeps_a_this_host_card(on_w2):
    out = _kept(on_w2.conn, _create(title="apply the next hermes update on this host"))
    assert "'this host' names this worker" in out["note"], out
    assert _home_marker(on_w2.conn, out["task_id"])["class"] == "a-this-host"


def test_a_worker_keeps_an_operator_home_marker(w2, monkeypatch):
    _on(monkeypatch, "worker-2", {"worker_queue": {"worker-2": POOL}, "extra_home_markers": [r"\bgrafana\b"]},
        _pool_machines())
    out = _kept(w2.conn, _create(title="fix the grafana panel"))
    assert "operator marker" in out["note"], out


def test_a_hub_hit_with_an_unpushed_tree_stays_kept(on_w2, tmp_path):
    """Every hit decides, not the first: 'on the hub' alone would queue."""
    tree = _pushed_clone(tmp_path, tmp_path / "code" / "_wt" / "mixed")
    _commit(tree, "b.txt")
    out = _kept(on_w2.conn, _create(title="on the hub, finish the fix in ~/code/_wt/mixed"))
    assert _home_marker(on_w2.conn, out["task_id"])["class"] == "a-worktree"


def test_an_explicit_runs_on_beats_a_home_marker(on_w2):
    out = _create(title="fix the hermes-sync drain", runs_on=["machine:worker-1"])
    assert _runs_on(on_w2.conn, out["task_id"]) == ["machine:worker-1"], out


def test_tree_state_without_refresh_never_fetches(tmp_path, monkeypatch):
    """Creation probes a named tree with ``refresh=False``: only ``ls-remote``, no fetch. Park and move keep the
    default (``refresh=True``), which refreshes a stale tracking ref (test_url_push_moved_tip_...)."""
    from hermes_cli import kanban_move_pool as kmp, worktree_ops
    tree = _pushed_clone(tmp_path, tmp_path / "norefresh")
    _git(tree, "fetch", "-q", "origin")
    _commit(tree, "b.txt")
    other = tmp_path / "norefresh-other"
    _git(tmp_path, "clone", "-q", "-b", "wt/x", str(tmp_path / "norefresh-remote.git"), str(other))
    _git(tree, "push", "-q", str(tmp_path / "norefresh-remote.git"), "HEAD:refs/heads/wt/x")  # by URL
    _commit(other, "advanced.txt")
    _git(other, "pull", "-q", "--rebase", "origin", "wt/x")
    _git(other, "push", "-q", "origin", "HEAD:refs/heads/wt/x")  # the remote moved past HEAD
    original, calls = worktree_ops._git_out, []

    def observe(args, cwd, **kwargs):
        calls.append((args[0], kwargs.get("timeout")))
        return original(args, cwd, **kwargs)
    monkeypatch.setattr(worktree_ops, "_git_out", observe)
    assert kmp.tree_state([str(tree)], refresh=False, budget=5) == f"unpushed commits in {tree}"
    # every git call, ls-remote included, ends inside the create's budget (main's deadline shrinks the timeouts)
    assert "fetch" not in [c for c, _ in calls] and "ls-remote" in [c for c, _ in calls], calls
    assert all(t is not None and 0 < t <= 5 for _, t in calls), calls
    calls.clear()
    assert kmp.tree_state([str(tree)]) is None  # the default refresh finds HEAD on the moved branch
    assert "fetch" in [c for c, _ in calls], calls


# --- the home_marker audit matches the stored placement (t_01a1db073fff0642) --------------------------------------
# The 24 h live check of t_01a1d2856e5fec44 found worker-created cards whose created event said ``decision: queue``
# while the card stayed local (runs_on NULL), or that a home hit kept with no key at all. One test per shape.
PASS_KEEPS = {"a-this-host", "a-worktree", "a-artifact", "operator", "gateway-test-failed"}


def _audit_matches(conn, tid):
    """The created event's ``home_marker`` says what the store did: queue exactly when ``runs_on`` is set, and a keep
    only with a class the live check accepts."""
    marker, runs_on = _home_marker(conn, tid), _runs_on(conn, tid)
    if marker is not None:
        assert (marker["decision"] == "queue") == (runs_on is not None), (marker, runs_on)
        assert marker["decision"] == "queue" or marker["class"] in PASS_KEEPS, marker
    return marker


def test_a_home_hit_whose_text_every_pickup_gate_refuses_is_recorded_as_kept(on_w2):
    """t_01a1d73ba3627290 (worker-2): an ssh hit queues, but a backticked ``hermes-hub tasks`` action (portability
    drops backtick spans; the pickup gate's text keeps them) makes ``required_labels`` None, so the card stays."""
    out = _create(title="Capability owner: ssh:mini for mini/t_01a1d73b8cf9634f",
                  body="1. Read the waiting card: `hermes-hub tasks --gw mini --json`.\n2. Use ssh to pull the log.")
    assert _runs_on(on_w2.conn, out["task_id"]) is None, out
    assert _audit_matches(on_w2.conn, out["task_id"]) == {
        "decision": "keep", "class": "gateway-test-failed", "hit": "ssh"}


@pytest.mark.parametrize("placement", ["local", "worker-2"])
def test_an_explicit_local_pin_carries_no_home_marker(on_w2, placement):
    """t_01a1d773774908a6 (worker-7, placement_intent explicit): the local-pin note simulated the auto placement
    (a queue record), then the pin kept the card here. The rule leaves explicit placement alone: no key."""
    out = _create(title="Activate the fix in ~/.hermes/plugins/ship-only-guard/__init__.py", placement=placement)
    assert _runs_on(on_w2.conn, out["task_id"]) is None, out
    created = _created(on_w2.conn, out["task_id"])
    assert created["placement_intent"] == "explicit" and "home_marker" not in created, created


def test_a_pool_worker_profile_without_a_worker_queue_records_its_home_keep(w2, monkeypatch):
    """t_01a1d8146b9ef790 (worker-5, created_by bb-tuner): that profile's config has no ``kanban.placement``, so
    ``queue_decision`` had no queue label and returned before any record: the home hit kept the card with no key."""
    _on(monkeypatch, "worker-2", {}, _pool_machines())
    out = _create(title="fix the hermes-sync drain")
    assert _runs_on(w2.conn, out["task_id"]) is None, out
    assert _created(w2.conn, out["task_id"])["portable_reason"] == "home gateway marker"
    assert _audit_matches(w2.conn, out["task_id"]) == {
        "decision": "keep", "class": "gateway-test-failed", "hit": "hermes-sync"}


@pytest.mark.parametrize("gateway, title", [
    ("macbook", "fix the hermes-sync drain"),  # not a Linux pool worker: no gateway test to fail
    ("worker-2", "rotate the CI token in the Keychain, then fix the hermes-sync drain"),  # a local marker keeps it
])
def test_a_queueless_keep_is_recorded_only_for_a_pool_workers_home_hit(w2, monkeypatch, gateway, title):
    _on(monkeypatch, gateway, {}, _pool_machines())
    out = _create(title=title)
    assert _runs_on(w2.conn, out["task_id"]) is None, out
    assert "home_marker" not in _created(w2.conn, out["task_id"]), out


def test_a_queueless_placement_outside_a_create_reads_no_registry(monkeypatch):
    """Off a create there is nothing to record, so a gateway without a queue label pays no registry read."""
    from hermes_cli import kanban_worker_placement as kwp
    calls = []

    def fetch():
        calls.append(1)
        return {"me": "worker-2", "machines": seen_now(_pool_machines().values())}
    wn.set_registry_fetch(fetch)
    try:
        assert kwp.queue_decision({"title": "fix the hermes-sync drain"}, {}, "worker-2", "auto") is None
    finally:
        wn.set_registry_fetch(None)
    assert calls == []
