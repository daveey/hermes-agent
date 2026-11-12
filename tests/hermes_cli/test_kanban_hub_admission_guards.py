"""Each fail-closed guard of the two-pool hub admission has a test that fails when the guard is removed."""
from contextlib import closing

import pytest

from hermes_cli import kanban_admission_pools as pools
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import machine_cpu_metrics as cm
from hermes_cli import machine_metrics as mm
from tests.hermes_cli import test_kanban_hub_admission as helpers

pytestmark = pytest.mark.platforms("posix")

board = helpers.board
configure, advisory, task, fake_sample = helpers.configure, helpers.advisory, helpers.task, helpers.fake_sample


def enable(home, extra=""):
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n" + extra)


def no_lease_or_sample(monkeypatch):
    monkeypatch.setattr(sync, "claim_gate", lambda *a, **k: pytest.fail("advisory refusal must not ask for a lease"))
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("advisory refusal must not sample"))


@pytest.mark.parametrize("key,value,reason", [
    ("priority_slots", -1, "priority_slots must be a nonnegative integer"),
    ("priority_slots", "true", "priority_slots must be a nonnegative integer"),
    ("priority_slots", "'3'", "priority_slots must be a nonnegative integer"),
    ("priority_cpu_busy_below_pct", 0, "priority CPU ceiling must be in (0,100]"),
    ("priority_cpu_busy_below_pct", 101, "priority CPU ceiling must be in (0,100]"),
    ("priority_cpu_busy_below_pct", "'85'", "priority CPU ceiling must be in (0,100]"),
])
def test_invalid_pool_setting_refuses_with_its_own_reason(board, key, value, reason):
    _conn, home = board
    (home / "config.yaml").write_text(f"kanban:\n  admission:\n    {key}: {value}\n")
    with pytest.raises(ValueError) as error:
        pools.root_settings()
    assert str(error.value) == reason


def test_capacity_receipt_without_origin_invalidates_origin_census(board):
    conn, home = board
    configure(home, limit=None)
    tid = task(conn)
    kb.edit_task(conn, tid, admission_class="capacity", admission_frees="frees: worker slots")
    assert kb.claim_task(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET admission_origin=NULL WHERE task_id=?", (tid,))
    assert mm.active_card_runs(detail={"pools": {"regular": 0, "priority": 0}, "capacity_origins": {}}) is None
    # Callers that do not count origins (dispatch budget, telemetry) still read the census.
    assert mm.active_card_runs(detail={"pools": {"regular": 0, "priority": 0}}) == 1


def test_dispatch_budget_keeps_legacy_scalar_when_cpu_policy_is_off(board):
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, home = board
    configure(home, cap=7, limit=None)
    for index in range(7):
        assert kb.claim_task(conn, task(conn, title=f"regular {index}"))

    def budget():
        return dispatch._tick_spawn_budget(conn, dispatch.DispatchResult(), max_spawn=None,
                                           max_in_progress=7, board=None)

    legacy = budget()
    with (home / "config.yaml").open("a") as config:
        config.write("  admission:\n    priority_slots: 3\n")
    assert pools.root_settings()[0] == 3
    assert budget() == legacy


def test_dispatch_budget_unknown_census_spawns_nothing(board, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, home = board
    enable(home)
    monkeypatch.setattr(mm, "active_card_runs", lambda **kw: None)
    assert dispatch._tick_spawn_budget(conn, dispatch.DispatchResult(), max_spawn=None,
                                       max_in_progress=7, board=None) == (False, None)


@pytest.mark.parametrize("review", [False, True])
def test_normal_card_keeps_regular_advisory_ceiling_on_opted_in_hub(board, monkeypatch, review):
    conn, home = board
    enable(home)
    advisory(home, pct=70)
    no_lease_or_sample(monkeypatch)
    t = task(conn, review)
    assert helpers.claim(conn, t, review) is None
    helpers.refused(conn, t, review, "hub_cpu_at_or_above_limit")


@pytest.mark.parametrize("review", [False, True])
def test_invalid_pool_policy_defers_at_advisory_before_lease(board, monkeypatch, review):
    conn, home = board
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: wrong\n")
    advisory(home)
    no_lease_or_sample(monkeypatch)
    t = task(conn, review)
    assert helpers.claim(conn, t, review) is None
    helpers.refused(conn, t, review, "hub_cpu_unknown")


@pytest.mark.parametrize("review", [False, True])
def test_pool_census_error_at_final_gate_refuses_instead_of_scalar_admit(board, monkeypatch, review):
    conn, home = board
    enable(home)
    advisory(home)
    monkeypatch.setattr(cm, "sample", fake_sample(10))

    def broken(*a, **k):
        raise ValueError("unknown admission pool receipt")
    monkeypatch.setattr(pools, "candidate_pool", broken)
    t = task(conn, review)
    assert helpers.claim(conn, t, review) is None
    helpers.refused(conn, t, review, "hub_capacity_full")


def test_candidate_pool_unknown_census_selects_no_pool(board, monkeypatch):
    conn, home = board
    enable(home)
    tid = task(conn)
    kb.edit_task(conn, tid, admission_class="urgent")
    monkeypatch.setattr(mm, "active_card_runs", lambda **kw: None)
    pool, ceiling, _detail = pools.candidate_pool(conn, tid, 7)
    assert (pool, ceiling) == (None, 85)


def test_pool_change_during_sample_rechecks_cpu_against_new_pool(board, monkeypatch):
    conn, home = board
    configure(home, limit=None)
    for index in range(6):
        assert kb.claim_task(conn, task(conn, title=f"regular {index}"))
    folder = kb.boards_root() / "late"
    folder.mkdir(parents=True)
    (folder / "board.json").write_text("{}")
    late = folder / "kanban.db"
    with closing(kbc.connect(late)) as other:
        for index in range(3):
            assert kb.claim_task(other, task(other, title=f"late {index}"))
        with kb.write_txn(other):
            other.execute("UPDATE task_runs SET admission_pool='priority', admission_class='urgent'")
            other.execute("UPDATE tasks SET status='blocked'")
    enable(home)
    advisory(home, pct=70)
    calls = []

    def fill_priority_pool():
        # Another board's three priority runs become live while this claim samples: the recount moves the
        # candidate from the priority pool (85%) to the last regular slot (50%), where 70% must refuse.
        with closing(kbc.connect(late)) as other, kb.write_txn(other):
            other.execute("UPDATE tasks SET status='running'")
    monkeypatch.setattr(cm, "sample", fake_sample(70, calls=calls, check=fill_priority_pool))
    candidate = task(conn, title="incident")
    kb.edit_task(conn, candidate, admission_class="urgent")
    assert kb.claim_task(conn, candidate) is None
    assert len(calls) == 1
    assert kb.get_task(conn, candidate).status == "ready"
    assert sync.pop_claim_deferral(candidate) == "hub_cpu_at_or_above_limit"
    assert conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (candidate,)).fetchone()[0] == 0
