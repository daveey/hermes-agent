"""kanban_* tool fence for a worker's subprocesses follows the board, like the DB fence. [daveey]

A Kanban worker's terminal/execute_code subprocesses inherit ``HERMES_DELEGATED_CHILD_CONTEXT=<board
root>``. The DB fence (``kanban_path_is_fenced``) refuses writes only to the pinned board and boards under
that root, so a throwaway ``HERMES_HOME`` outside it is writable. The tool layer used to refuse every
mutation once the marker was set, so a worker could never show a reviewer a live ``kanban_link`` /
``kanban_block`` refusal on a scratch board and handed "run this command" back to the user
(defect agent-cbb2687c75, t_01a194ea280f2a75).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

FOREIGN = "t_01a1937c927e0342"
REFUSAL = "delegate_task child agents are not Kanban run owners"


def _board_with_claimed_task(home: Path) -> tuple[str, int | None]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="scratch probe", assignee="scratch-worker")
        kb.claim_task(conn, tid)
        return tid, kb._current_run_id(conn, tid)


def _snap(tid: str) -> dict:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        return {"status": task.status, "run": kb._current_run_id(conn, tid), "parents": kb.parent_ids(conn, tid)}


def _layout(monkeypatch, real_root: Path, scratch: Path) -> tuple[Path, Path, str]:
    real_root.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_OWNER", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(scratch))
    monkeypatch.setenv("HERMES_PROFILE", "scratch-worker")
    tid, run_id = _board_with_claimed_task(scratch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return real_root, scratch, tid


@pytest.fixture
def fenced_layout(monkeypatch, tmp_path):
    """A fenced real root (the marker value) and a scratch home outside it."""
    return _layout(monkeypatch, tmp_path / "real-hermes", tmp_path / "kanban-probe")


@pytest.fixture
def scratch_dir_layout(monkeypatch, tmp_path):
    """A probe home in the fenced root's own auto-pruned scratch dir (``$TMPDIR/kanban-probe.X``), which the
    24 h idle pruner removes: the route that leaves nothing behind in ``~``."""
    real_root = tmp_path / "real-hermes"
    return _layout(monkeypatch, real_root, real_root / "cache" / "scratch" / "kanban-probe.AbCd")


def _probe(tid: str) -> tuple[dict, dict, dict, dict]:
    """(a) link a foreign parent, (b) dependency-block on a raw dangling edge; card before/after each."""
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    before = _snap(tid)
    link = json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))
    after_link = _snap(tid)
    assert after_link == before
    with kbc.connect() as conn, kbc.write_txn(conn):
        conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (FOREIGN, tid))
    before_block = _snap(tid)
    block = json.loads(kt._handle_block({"task_id": tid, "kind": "dependency", "reason": "wait for remote owner"}))
    return link, block, before_block, _snap(tid)


def test_env_fenced_subprocess_reaches_real_logic_on_scratch_home(monkeypatch, fenced_layout):
    """(a) Marker set, HERMES_HOME outside its root: the handlers run their real validation."""
    real_root, _scratch, tid = fenced_layout
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))

    link, block, before, after = _probe(tid)

    assert REFUSAL not in json.dumps(link)
    assert "unknown task(s)" in link["error"] and FOREIGN in link["error"]
    assert REFUSAL not in json.dumps(block)
    assert "unknown task(s)" in block["error"] and "awaiting={kind:'card'" in block["error"]
    assert after == before and after["status"] == "running"


def test_env_fenced_subprocess_still_refused_on_board_under_root(monkeypatch, fenced_layout):
    """(b) Control: the same subprocess whose board lies under the fenced root is refused."""
    _real_root, scratch, tid = fenced_layout
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(scratch))  # scratch IS the fenced root here
    from tools import kanban_tools as kt

    before = _snap(tid)
    link = json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))
    block = json.loads(kt._handle_block({"task_id": tid, "kind": "dependency", "reason": "x"}))
    comment = json.loads(kt._handle_comment({"task_id": tid, "body": "x"}))
    for out in (link, block, comment):
        assert REFUSAL in out["error"]
        assert 'mktemp -d "$TMPDIR/kanban-probe.' in out["error"]  # the refusal names the scratch-home route
    assert _snap(tid) == before


def test_env_fenced_subprocess_refused_when_home_is_fenced_but_board_is_not(monkeypatch, fenced_layout):
    """Control: board moved out via HERMES_KANBAN_HOME while HERMES_HOME (gateway config, credentials) is
    still the real, fenced home: refused, so a scratch board never runs with the real home's routes."""
    real_root, scratch, tid = fenced_layout
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(scratch))
    monkeypatch.setenv("HERMES_HOME", str(real_root))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    from tools import kanban_tools as kt

    before = _snap(tid)
    link = json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))
    assert REFUSAL in link["error"]
    assert _snap(tid) == before


@pytest.mark.parametrize("layout", ["fenced_layout", "scratch_dir_layout"])
def test_scratch_home_scope_covers_local_writes_only(monkeypatch, request, layout):
    """On a scratch home (outside the root, or in its scratch dir): comment and an explicitly local create
    work; a create with any other placement, archive (may relay home) and origin adoption stay refused."""
    real_root, _scratch, tid = request.getfixturevalue(layout)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    monkeypatch.delenv("HERMES_KANBAN_TASK")  # act as an orchestrator on the scratch board
    from tools import kanban_tools as kt

    assert json.loads(kt._handle_comment({"task_id": tid, "body": "probe"}))["ok"] is True
    created = json.loads(kt._handle_create({"title": "probe child", "assignee": "x", "placement": "local",
                                            "requires": []}))
    assert created.get("ok") is True, created
    for out in (kt._handle_create({"title": "t", "assignee": "x", "placement": "hub", "requires": []}),
                kt._handle_create({"title": "t", "assignee": "x", "requires": []}),
                kt._handle_archive({"task_id": created["task_id"]})):
        assert REFUSAL in json.loads(out)["error"], out


def test_env_fenced_subprocess_still_refused_on_pinned_db(monkeypatch, fenced_layout):
    """(b) Control: the dispatcher-pinned HERMES_KANBAN_DB is refused even when it sits outside the root."""
    real_root, _scratch, tid = fenced_layout
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    from tools import kanban_tools as kt

    before = _snap(tid)
    link = json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))
    block = json.loads(kt._handle_block({"task_id": tid, "kind": "dependency", "reason": "x", "board": "other"}))
    assert REFUSAL in link["error"] and REFUSAL in block["error"]
    assert _snap(tid) == before


def test_legacy_marker_still_refuses_everywhere(monkeypatch, fenced_layout):
    _real_root, _scratch, tid = fenced_layout
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    from tools import kanban_tools as kt

    assert REFUSAL in json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))["error"]


def test_in_process_delegate_child_still_refused_on_scratch_home(fenced_layout):
    """(c) Control: an in-process delegate_task child is refused on any board, scratch included."""
    _real_root, _scratch, tid = fenced_layout
    from agent.delegation_context import delegated_child_context
    from tools import kanban_tools as kt

    before = _snap(tid)
    with delegated_child_context("child-session"):
        link = json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))
        comment = json.loads(kt._handle_comment({"task_id": tid, "body": "x"}))
    assert REFUSAL in link["error"] and REFUSAL in comment["error"]
    assert _snap(tid) == before


def test_real_worker_subprocess_drives_tools_on_scratch_home(monkeypatch, tmp_path):
    """End to end: a real subprocess with the env a worker's terminal gets (``delegated_child_subprocess_env``,
    owner-bound marker) and a scratch HERMES_HOME outside the fenced root gets the real refusal text, while the
    same subprocess on a board under the root is still refused by the tool fence."""
    from agent.delegation_context import delegated_child_subprocess_env

    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_OWNER", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    real_root = tmp_path / "real-hermes"
    homes = {"scratch": tmp_path / "kanban-probe", "fenced": real_root / "sub"}
    tasks = {}
    for name, home in homes.items():  # boards created by the (unfenced) owner, as the dispatcher would
        home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        tasks[name] = _board_with_claimed_task(home)
    base = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_KANBAN", "HERMES_DELEGATED"))}
    base["HERMES_DELEGATED_CHILD_CONTEXT"] = str(real_root)
    env = delegated_child_subprocess_env(base)
    assert env["HERMES_DELEGATED_CHILD_CONTEXT"] == str(real_root) and env.get("HERMES_DELEGATED_CHILD_OWNER")
    script = textwrap.dedent(f"""
        import json, os, sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
        from tools import kanban_tools as kt
        tid = os.environ["HERMES_KANBAN_TASK"]
        print(json.dumps(json.loads(kt._handle_link({{"parent_id": {FOREIGN!r}, "child_id": tid}}))))
    """)
    outputs = {}
    for name, home in homes.items():
        tid, run = tasks[name]
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False,
            env={**env, "HERMES_HOME": str(home), "HERMES_KANBAN_TASK": tid, "HERMES_KANBAN_RUN_ID": str(run)})
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        outputs[name] = lines[-1] if proc.returncode == 0 and lines else proc.stderr[-2000:]
    assert "unknown task(s)" in outputs["scratch"] and REFUSAL not in outputs["scratch"], outputs["scratch"]
    assert REFUSAL in outputs["fenced"], outputs["fenced"]
    for name, home in homes.items():
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert _snap(tasks[name][0])["parents"] == []


def test_descendant_refusal_text_names_the_scratch_home_route(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(tmp_path))
    from hermes_cli.kanban_db_fence import descendant_refusal_text

    text = descendant_refusal_text()
    # The route makes the home in the auto-pruned scratch dir: an unattended run cannot `rm -rf`, and a home
    # in ~ leaked 0.1-2.3 GB per probe on pool workers (t_01a1d3b5bfc898e7).
    assert 'H=$(mktemp -d "$TMPDIR/kanban-probe.XXXX")' in text and "~/kanban-probe" not in text
    assert "unset HERMES_KANBAN_DB HERMES_KANBAN_BOARD HERMES_KANBAN_HOME" in text and "HERMES_HOME=$H" in text
    assert "24 h idle" in text and "rm -rf" in text and "optional" in text


# ---- Probe homes in the fenced root's scratch dir (t_01a1d3b5bfc898e7) ------------------------------------
# A probe home in ~ needed an `rm -rf` that unattended runs may not do, so 0.1-2.3 GB homes piled up on pool
# workers. The scratch dir (<marker root>/cache/scratch, TMPDIR) is auto-pruned after 24 h idle; an entry in
# it is the only exemption under the fenced root. Everything else under the root, the pinned DB, the legacy
# marker and in-process delegate children stay fenced.


def test_scratch_dir_home_reaches_real_logic(monkeypatch, scratch_dir_layout):
    """Allowed: marker = real root, HERMES_HOME = <real root>/cache/scratch/kanban-probe.X."""
    real_root, _scratch, tid = scratch_dir_layout
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    link, block, before, after = _probe(tid)

    assert REFUSAL not in json.dumps(link)
    assert "unknown task(s)" in link["error"] and FOREIGN in link["error"]
    assert REFUSAL not in json.dumps(block)
    assert "unknown task(s)" in block["error"]
    assert after == before and after["status"] == "running"
    with kbc.connect() as conn:  # the DB fence agrees: a real write lands on the scratch-dir board
        assert kb.create_task(conn, title="db write", assignee="x")


def test_exemption_is_the_scratch_dir_get_scratch_dir_gives_for_the_marker_root(monkeypatch, tmp_path):
    from agent.delegation_context import kanban_path_is_fenced
    from hermes_constants import get_scratch_dir

    real_root = tmp_path / "real-hermes"
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    scratch = get_scratch_dir(real_root, prune=False)
    assert not kanban_path_is_fenced(scratch / "kanban-probe.X")
    assert not kanban_path_is_fenced(scratch / "kanban-probe.X" / "kanban.db")
    assert kanban_path_is_fenced(scratch)  # the scratch dir itself is not a probe home
    assert kanban_path_is_fenced(real_root / "cache" / "scratchy" / "kanban-probe.X")
    assert kanban_path_is_fenced(scratch / ".." / "other" / "kanban.db")


@pytest.mark.parametrize("absolute", [False, True])
def test_core_noindex_scratch_descendants_and_fences(monkeypatch, tmp_path, absolute):
    from agent.delegation_context import delegated_child_context, kanban_path_is_fenced
    from hermes_constants import SPOTLIGHT_NOINDEX_SUFFIX, ensure_spotlight_noindex_dir

    root = tmp_path / "real-hermes"
    scratch = root / "cache" / "scratch"
    sibling = scratch.with_name(scratch.name + SPOTLIGHT_NOINDEX_SUFFIX)
    monkeypatch.delenv("HERMES_SPOTLIGHT_INDEX_CACHE", raising=False)
    ensure_spotlight_noindex_dir(scratch, platform="darwin")
    if absolute:
        scratch.unlink()
        scratch.symlink_to(sibling, target_is_directory=True)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(root))
    for spelling in (scratch, sibling):
        assert not kanban_path_is_fenced(spelling / "probe")
        assert not kanban_path_is_fenced(spelling / "probe" / "kanban.db")
        assert kanban_path_is_fenced(spelling)
    escaped = sibling / "escape"
    escaped.symlink_to(root / "sub", target_is_directory=True)
    assert kanban_path_is_fenced(escaped / "kanban.db")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(scratch / "probe" / "kanban.db"))
    assert kanban_path_is_fenced(sibling / "probe" / "kanban.db")
    monkeypatch.delenv("HERMES_KANBAN_DB")
    with delegated_child_context("child-session"):
        assert kanban_path_is_fenced(sibling / "probe")
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    assert kanban_path_is_fenced(sibling / "probe")


@pytest.mark.parametrize("link", [None, "other", "./scratch.noindex", "../cache/scratch.noindex"])
def test_noindex_sibling_requires_exact_core_link(monkeypatch, tmp_path, link):
    from agent.delegation_context import kanban_path_is_fenced

    root = tmp_path / "real-hermes"
    scratch = root / "cache" / "scratch"
    sibling = scratch.with_name("scratch.noindex")
    sibling.mkdir(parents=True)
    if link is None:
        scratch.mkdir()
    else:
        scratch.symlink_to(link, target_is_directory=True)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(root))
    assert kanban_path_is_fenced(sibling / "probe")
    if link is None:
        assert not kanban_path_is_fenced(scratch / "probe")
    else:
        assert kanban_path_is_fenced(scratch / "probe")


def test_noindex_sibling_requires_symlink_even_with_matching_readlink(monkeypatch, tmp_path):
    from agent.delegation_context import kanban_path_is_fenced

    root = tmp_path / "real-hermes"
    scratch = root / "cache" / "scratch"
    scratch.mkdir(parents=True)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(root))
    # Isolate the is_symlink condition from the readlink OSError fallback.
    monkeypatch.setattr(os, "readlink", lambda path: "scratch.noindex")
    assert kanban_path_is_fenced(scratch.with_name("scratch.noindex") / "probe")


def test_noindex_scratch_readlink_failure_stays_fenced(monkeypatch, tmp_path):
    from agent.delegation_context import kanban_path_is_fenced

    root = tmp_path / "real-hermes"
    scratch = root / "cache" / "scratch"
    scratch.parent.mkdir(parents=True)
    scratch.symlink_to("scratch.noindex", target_is_directory=True)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(root))

    def failed_readlink(path, *args, **kwargs):
        raise OSError("link changed during inspection")

    # Probe the physical spelling so target.resolve does not itself read this link.
    monkeypatch.setattr(os, "readlink", failed_readlink)
    assert kanban_path_is_fenced(scratch.with_name("scratch.noindex") / "probe")


@pytest.mark.parametrize("rel", ["sub", "cache/other", "cache"])
def test_homes_elsewhere_under_the_root_stay_refused(monkeypatch, tmp_path, rel):
    real_root = tmp_path / "real-hermes"
    _root, _home, tid = _layout(monkeypatch, real_root, real_root / rel / "kanban-probe.AbCd")
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    from tools import kanban_tools as kt

    before = _snap(tid)
    assert REFUSAL in json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))["error"]
    assert _snap(tid) == before


def test_scratch_dir_home_with_board_elsewhere_under_root_stays_refused(monkeypatch, tmp_path):
    """HERMES_HOME in the scratch dir but HERMES_KANBAN_HOME (the board) under the root: refused."""
    real_root = tmp_path / "real-hermes"
    board_home = real_root / "sub"
    _root, _home, tid = _layout(monkeypatch, real_root, board_home)
    from hermes_cli import kanban_db as kb

    home = real_root / "cache" / "scratch" / "kanban-probe.AbCd"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(board_home))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    assert Path(kb.kanban_db_path()).resolve().is_relative_to(board_home.resolve())
    from tools import kanban_tools as kt

    before = _snap(tid)
    assert REFUSAL in json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))["error"]
    assert _snap(tid) == before


def test_symlink_in_scratch_dir_to_a_board_under_root_stays_refused(monkeypatch, tmp_path):
    """Symlinks resolve first: a scratch-dir entry pointing at a home under the root is that home."""
    real_root = tmp_path / "real-hermes"
    _root, target, tid = _layout(monkeypatch, real_root, real_root / "sub")
    link_home = real_root / "cache" / "scratch" / "kanban-probe.Link"
    link_home.parent.mkdir(parents=True)
    link_home.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(link_home))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    from agent.delegation_context import kanban_path_is_fenced
    from tools import kanban_tools as kt

    assert kanban_path_is_fenced(link_home) and kanban_path_is_fenced(link_home / "kanban.db")
    before = _snap(tid)
    assert REFUSAL in json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))["error"]
    assert _snap(tid) == before


def test_scratch_dir_symlinked_into_the_root_exempts_nothing(monkeypatch, tmp_path):
    """<root>/cache/scratch as a symlink to <root>/sub: entries resolve under sub, which stays fenced."""
    from agent.delegation_context import kanban_path_is_fenced

    real_root = tmp_path / "real-hermes"
    (real_root / "sub" / "kanban-probe.X").mkdir(parents=True)
    (real_root / "cache").mkdir()
    (real_root / "cache" / "scratch").symlink_to(real_root / "sub", target_is_directory=True)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    assert kanban_path_is_fenced(real_root / "cache" / "scratch" / "kanban-probe.X" / "kanban.db")
    assert kanban_path_is_fenced(real_root / "sub" / "kanban-probe.X" / "kanban.db")


def test_pinned_db_under_scratch_dir_stays_refused(monkeypatch, scratch_dir_layout):
    real_root, _scratch, tid = scratch_dir_layout
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    from agent.delegation_context import kanban_path_is_fenced
    from tools import kanban_tools as kt

    assert kanban_path_is_fenced(kb.kanban_db_path())
    before = _snap(tid)
    assert REFUSAL in json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))["error"]
    assert _snap(tid) == before


def test_legacy_marker_still_refuses_a_scratch_dir_home(monkeypatch, scratch_dir_layout):
    _real_root, _scratch, tid = scratch_dir_layout
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    from tools import kanban_tools as kt

    assert REFUSAL in json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))["error"]


def test_in_process_delegate_child_still_refused_on_a_scratch_dir_home(monkeypatch, scratch_dir_layout):
    real_root, scratch, tid = scratch_dir_layout
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(real_root))
    from agent.delegation_context import delegated_child_context, kanban_path_is_fenced
    from tools import kanban_tools as kt

    before = _snap(tid)
    with delegated_child_context("child-session"):
        assert kanban_path_is_fenced(scratch / "kanban.db")
        link = json.loads(kt._handle_link({"parent_id": FOREIGN, "child_id": tid}))
        comment = json.loads(kt._handle_comment({"task_id": tid, "body": "x"}))
    assert REFUSAL in link["error"] and REFUSAL in comment["error"]
    assert _snap(tid) == before


def test_real_worker_subprocess_drives_tools_on_scratch_dir_home(monkeypatch, tmp_path):
    """End to end, like test_real_worker_subprocess_drives_tools_on_scratch_home: a real owner-bound worker
    subprocess with HERMES_HOME=<root>/cache/scratch/kanban-probe.X reaches the real refusal text; homes
    elsewhere under the root (sub, cache/other) stay refused by the fence."""
    from agent.delegation_context import delegated_child_subprocess_env

    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_OWNER", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    real_root = tmp_path / "real-hermes"
    homes = {"scratch_dir": real_root / "cache" / "scratch" / "kanban-probe.AbCd",
             "sub": real_root / "sub", "cache_other": real_root / "cache" / "other"}
    tasks = {}
    for name, home in homes.items():
        home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        tasks[name] = _board_with_claimed_task(home)
    base = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_KANBAN", "HERMES_DELEGATED"))}
    base["HERMES_DELEGATED_CHILD_CONTEXT"] = str(real_root)
    env = delegated_child_subprocess_env(base)
    assert env["HERMES_DELEGATED_CHILD_CONTEXT"] == str(real_root) and env.get("HERMES_DELEGATED_CHILD_OWNER")
    script = textwrap.dedent(f"""
        import json, os, sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
        from tools import kanban_tools as kt
        tid = os.environ["HERMES_KANBAN_TASK"]
        print(json.dumps(json.loads(kt._handle_link({{"parent_id": {FOREIGN!r}, "child_id": tid}}))))
    """)
    outputs = {}
    for name, home in homes.items():
        tid, run = tasks[name]
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False,
            env={**env, "HERMES_HOME": str(home), "HERMES_KANBAN_TASK": tid, "HERMES_KANBAN_RUN_ID": str(run)})
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        outputs[name] = lines[-1] if proc.returncode == 0 and lines else proc.stderr[-2000:]
    assert "unknown task(s)" in outputs["scratch_dir"] and REFUSAL not in outputs["scratch_dir"], outputs
    assert REFUSAL in outputs["sub"] and REFUSAL in outputs["cache_other"], outputs
    for name, home in homes.items():
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert _snap(tasks[name][0])["parents"] == []


def test_scratch_pruner_removes_an_idle_probe_home(monkeypatch, tmp_path):
    """The cleanup the route relies on: the scratch pruner removes an idle kanban-probe.* home (board and
    all) under the scratch dir, with no `rm -rf` by the run that made it."""
    from hermes_constants import get_scratch_dir, prune_scratch_dir
    from tests import test_scratch_dir as scratch_fixtures

    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(key, raising=False)
    scratch = get_scratch_dir(tmp_path / "real-hermes", prune=False)
    home = scratch_fixtures._idle_entry(scratch, "kanban-probe.AbCd")
    monkeypatch.setenv("HERMES_HOME", str(home))
    _board_with_claimed_task(home)
    assert any(home.rglob("kanban.db"))

    assert prune_scratch_dir(scratch, max_idle_hours=0) == 1
    assert not home.exists()
