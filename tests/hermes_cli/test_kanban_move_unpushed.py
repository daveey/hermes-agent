"""A move never strands a card's unpushed work on the host it leaves (self-heal agent-e575f77d7d). [daveey]

2026-11-11 21:25Z: ``hermes kanban move t_01a1cc2e2445efac --runs-on machine:worker-2`` moved a hub card whose
``~/code/_wt/*`` trees held unpushed commits. Worker-2 could not reach them and moved it straight back; it recurred on
11-12 (hub -> worker-3). ``kanban_move.check`` now reads the card's trees on this host when the card leaves it and
refuses ``unpushed_state_here`` (``force`` leaves it, recorded as ``left_unpushed_state``). Every entry path goes
through ``check``: the queued move, the running card's own move, the CLI (which prints the code) and the
pickup-stall alarm's re-place (which leaves the card where it is).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_move as km
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import HUB, MACHINES, WORKER, Gateway

# kanban_move_pool: a machine:<x> target needs a host_reason naming state on x (t_01a1d95c41620e6c).
HR = "the card's worktree is on that host"

TARGET = [f"machine:{WORKER}"]


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # card worktrees are looked up under ~/code/_wt
    sync.consume_dispatcher_wake()
    gw = Gateway(tmp_path, HUB, FakeService(lease_runs=True, machines=MACHINES), monkeypatch, pickup=False)
    gw.enter()
    yield gw
    gw.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


def _commit(tree, name):
    (tree / name).write_text(name + "\n")
    _git(tree, "add", name)
    _git(tree, "commit", "-qm", name)


def _worktree(tmp_path, slug, owner=None):
    """``~/code/_wt/<slug>``: a linked worktree of a clone whose commits are on its remote, marked for ``owner``."""
    remote, main = tmp_path / f"{slug}-remote.git", tmp_path / f"{slug}-main"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    _git(tmp_path, "clone", "-q", str(remote), str(main))
    _commit(main, "a.txt")
    _git(main, "push", "-q", "origin", "HEAD:refs/heads/main")
    tree = tmp_path / "code" / "_wt" / slug
    _git(main, "worktree", "add", "-q", "-b", f"wt/{slug}", str(tree))
    if owner:
        gitdir = (tree / ".git").read_text().split(":", 1)[1].strip()
        (Path(gitdir) / "hermes-owner.json").write_text(json.dumps({"task_id": owner}))
    return tree


def _card(hub, title="scratch card", **kw):
    return kb.create_task(hub.conn, title=title, assignee="default", **kw)


def _moved(conn, tid):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'moved' ORDER BY id", (tid,))]


def _runs_on(conn, tid):
    raw = conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    return json.loads(raw) if raw else None


def _refused(res, tree):
    assert not res["ok"] and res["reason_code"] == "unpushed_state_here", res
    assert str(tree.resolve()) in res["error"] and HUB in res["error"], res


def test_an_owner_marked_tree_with_an_unpushed_commit_keeps_the_card_here(hub, tmp_path):
    tid = _card(hub)
    tree = _worktree(tmp_path, "impl", owner=tid)
    _commit(tree, "b.txt")  # on wt/impl only: no remote has it
    res = km.move_queued(hub.conn, tid, runs_on=TARGET, host_reason=HR, reason="worker-1 has slots")
    _refused(res, tree)
    assert "unpushed commits" in res["error"]
    assert _moved(hub.conn, tid) == [] and _runs_on(hub.conn, tid) != TARGET


def test_a_dirty_owner_marked_tree_keeps_the_card_here(hub, tmp_path):
    tid = _card(hub)
    tree = _worktree(tmp_path, "dirty", owner=tid)
    _git(tree, "push", "-q", "origin", "HEAD:refs/heads/wt/dirty")
    (tree / "wip.txt").write_text("not committed\n")
    res = km.move_queued(hub.conn, tid, runs_on=TARGET, host_reason=HR, reason="worker-1 has slots")
    _refused(res, tree)
    assert "dirty worktree" in res["error"]


def test_a_clean_tree_whose_commits_are_pushed_moves(hub, tmp_path):
    tid = _card(hub)
    tree = _worktree(tmp_path, "pushed", owner=tid)
    _commit(tree, "b.txt")
    _git(tree, "push", "-q", "origin", "HEAD:refs/heads/wt/pushed")
    res = km.move_queued(hub.conn, tid, runs_on=TARGET, host_reason=HR, reason="worker-1 has slots")
    assert res["ok"], res
    assert "left_unpushed_state" not in _moved(hub.conn, tid)[-1]


def test_a_tree_named_only_in_the_newest_comment_keeps_the_card_here(hub, tmp_path):
    tid = _card(hub)
    tree = _worktree(tmp_path, "named")  # no owner marker: only the comment ties it to the card
    _commit(tree, "b.txt")
    kb.add_comment(hub.conn, tid, "default", "older note, no path")
    kb.add_comment(hub.conn, tid, "default", f"Implementation is in {tree}, commit b not pushed yet.")
    _refused(km.move_queued(hub.conn, tid, runs_on=TARGET, host_reason=HR, reason="worker-1 has slots"), tree)


def test_a_shared_main_checkout_the_body_mentions_is_not_the_cards_work(hub, tmp_path):
    main = tmp_path / "code" / "babybear"
    main.mkdir(parents=True)
    _git(main, "init", "-q")
    (main / "untracked.txt").write_text("someone else's\n")
    tid = _card(hub, body=f"Fix the parser in {main}.")
    assert km.move_queued(hub.conn, tid, runs_on=TARGET, host_reason=HR, reason="worker-1 has slots")["ok"]


def test_force_leaves_the_state_and_the_event_records_it(hub, tmp_path):
    tid = _card(hub)
    tree = _worktree(tmp_path, "forced", owner=tid)
    _commit(tree, "b.txt")
    res = km.move_queued(hub.conn, tid, runs_on=TARGET, host_reason=HR, reason="abandon the hub copy", force=True)
    assert res["ok"], res
    ev = _moved(hub.conn, tid)[-1]
    assert ev["left_unpushed_state"] == f"unpushed commits in {tree.resolve()}", ev


def test_a_queued_move_whose_matches_include_this_host_is_not_refused(hub, tmp_path):
    tid = _card(hub)
    tree = _worktree(tmp_path, "stays", owner=tid)
    _commit(tree, "b.txt")
    res = km.move_queued(hub.conn, tid, runs_on=["linux"], reason="any linux host, this one included")
    assert res["ok"] and HUB in res["matches"], res
    assert "left_unpushed_state" not in _moved(hub.conn, tid)[-1]


def test_a_running_cards_own_move_is_refused_and_its_run_goes_on(hub, tmp_path):
    tid = _card(hub, runs_on=[f"machine:{HUB}"])
    hub.round()
    hub.tick()
    run_id = hub.conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert kb.get_task(hub.conn, tid).status == "running"
    tree = _worktree(tmp_path, "running", owner=tid)
    _commit(tree, "b.txt")
    res = km.move_task(hub.conn, tid, runs_on=TARGET, host_reason=HR, reason="r", checkpoint="c", expected_run_id=run_id)
    _refused(res, tree)
    assert kb.get_task(hub.conn, tid).status == "running"


def test_the_cli_prints_the_refusal_code(hub, tmp_path):
    from hermes_cli import kanban as kcli
    tid = _card(hub)
    tree = _worktree(tmp_path, "cli", owner=tid)
    _commit(tree, "b.txt")
    out = json.loads(kcli.run_slash(f'move {tid} --runs-on machine:{WORKER} --host-reason worktree --reason "slots" --json'))
    assert out["reason_code"] == "unpushed_state_here", out
    text = kcli.run_slash(f'move {tid} --runs-on machine:{WORKER} --host-reason worktree --reason "slots"')
    assert "[unpushed_state_here]" in text and "pass force to leave it" in text, text
