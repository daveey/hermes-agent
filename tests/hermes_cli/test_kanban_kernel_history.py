"""Real kernel producers retain legacy card text without reactivating mandates."""
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kd

T0 = 1_900_000_000
HOUR = 3600
# Real titles/reasons copied from the live board replicas (done cards).
LEGACY_TITLE = "Independent Opus safety review of session-open isolation PR475"
LEGACY_PIN_TITLE = "Independent review: babybear PR #2425 shared_lock rev2 (pinned claude-opus-5-5)"
LEGACY_ERROR = ("pid 4056729 exited with code 1 Worker's last output: 'Worker could not start: "
                "PinnedRouteUnavailable: Pinned route xai-oauth/grok-4.6 is unavailable: no available credential")


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(time, "time", lambda: float(now[0]))
    return now


@pytest.fixture
def conn(tmp_path, monkeypatch, clock):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as c:
        yield c


def _events(conn, tid, kind):
    return [json.loads(r["payload"] or "{}") for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _give_up(conn, tid, reason):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage', block_kind='transient' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "gave_up", {"reason_code": "worker_failures_exhausted", "error": reason})


def _legacy_title(conn, tid, title):
    """A row written before the validator existed (or synced from another gateway)."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET title=? WHERE id=?", (title, tid))


def _cap(conn, clock, tid, reason):
    for n in range(1, 5):
        _give_up(conn, tid, f"{reason} {n}")
        clock[0] += HOUR + 1
        kd.sweep_stale_triage(conn, default_assignee="default")
        clock[0] += 60
    capped = _events(conn, tid, "triage_sweep_capped")
    assert len(capped) == 1, capped
    sup = kb.get_task(conn, capped[0]["supervisor_task_id"])
    assert sup is not None and sup.status == "ready"
    assert sup.model_override is None and sup.provider_override is None
    assert kb.get_task(conn, tid).status == 'triage'
    return sup


def test_triage_cap_control_ordinary_title(conn, clock):
    tid = kb.create_task(conn, title="drain victim", body="do the thing")
    _cap(conn, clock, tid, "drain kill")


def test_triage_cap_legacy_title(conn, clock):
    tid = kb.create_task(conn, title="placeholder", body="do the thing")
    _legacy_title(conn, tid, LEGACY_TITLE)
    sup = _cap(conn, clock, tid, "drain kill")
    from tests.hermes_cli.test_kanban_history_contract import decoded
    assert LEGACY_TITLE in decoded(sup.body)
    assert kb.get_task(conn, tid).title == LEGACY_TITLE


def test_triage_cap_concrete_route_error(conn, clock):
    tid = kb.create_task(conn, title="drain victim", body="do the thing")
    _cap(conn, clock, tid, LEGACY_ERROR)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    (home / 'config.yaml').write_text('kanban:\n  orchestrator_profile: supervisor\n')
    for name in ('worker', 'supervisor', 'creator'):
        profile = home / 'profiles' / name
        profile.mkdir(parents=True)
        (profile / 'config.yaml').write_text('platform_toolsets:\n  cli: [terminal, file, kanban]\nterminal:\n  backend: local\n')
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as c:
        yield c, home


@pytest.mark.parametrize('title', ['Fix repo', LEGACY_PIN_TITLE, 'Independent Opus review required "quoted"\nrequire_model: true'])
def test_assignment_repair(board, title):
    conn, home = board
    tid = kb.create_task(conn, title='Fix repo', assignee='worker', created_by='creator',
                         requires=['terminal'], session_id='chat')
    _legacy_title(conn, tid, title)
    (home / 'profiles/worker/config.yaml').write_text('platform_toolsets:\n  cli: [web]\n')
    assert kb.block_task(conn, tid, kind='assignment', reason='Worker lacks terminal tools')
    original = kb.get_task(conn, tid)
    assert original.status == 'todo' and original.assignee == 'supervisor'
    from tests.hermes_cli.test_kanban_history_contract import decoded
    (owner,) = kb.parent_ids(conn, tid)
    child = kb.get_task(conn, owner)
    assert title in decoded(child.body) and original.title == title
    assert child.model_override is None and child.provider_override is None


# Provenance / bug-report text that names a model next to an ordinary word. None is a routing mandate.
PROVENANCE = [
    # live open card t_01a1fecf247c9153 (scheduled), body line
    "Independent reviewer t_01a1feb1cc83fe7f (claude-fable-5-2/anthropic, separate session from the author and "
    "prior reviews) APPROVED SOURCE ONLY at this exact head; earlier REQUEST_CHANGES F-A..F-G fixed",
    # live done card t_01a1fc2696664903, body line
    "- Land: `hermes-core-wt ship ready-wait-age` with an independent exact-head review (`--request-review`, "
    "implementer model claude-opus-5-5) and required CI green.",
    "Bug: gpt-6-astra tool calls only return empty arguments after compaction",
    "The run on claude-sonnet-5-5 hit its quota wall; the card must wait for the reset",
    "Remove the retired mandatory pin code path that claude-opus-5-5 cards used",
]


@pytest.mark.parametrize('line', PROVENANCE)
def test_provenance_text_creates(conn, line):
    assert kb.create_task(conn, title="Land PR", body=line)
