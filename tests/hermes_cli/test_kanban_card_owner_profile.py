"""Local card identity carries the board-root owner profile, never the reading process's active profile.

``kanban_home()`` is shared across profiles, so two consumers under different active profiles read the
same card. Their observations must have one ``identity_key`` (the hub ``collect_qualified_tasks`` rule:
``<root>`` is ``default``, ``<root>/profiles/<P>`` is ``P``). Driven through the real list, sidebar-count,
done-receipt and card-detail entry paths.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_execution_observations as observations
from hermes_cli import kanban_origin
from hermes_cli import kanban_session_activity as activity
from hermes_cli import profiles
from hermes_cli.execution_observations import identity_key
from tests.hermes_state.test_completion_receipt import receipt

GATEWAY = "fixture-gateway"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: (GATEWAY, {GATEWAY}))
    monkeypatch.setattr(kanban_origin, "gateway_name", lambda: GATEWAY)
    monkeypatch.setattr(observations, "_gateway_name", lambda: GATEWAY)
    return tmp_path


def _board(monkeypatch, home):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    activity._reset_cache_for_tests()
    return kbc.connect_closing(kb.kanban_db_path(board="default"))


def _cards(monkeypatch, active):
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: active)
    activity._reset_cache_for_tests()
    listed = [v for r in activity.session_tasks(["session"], statuses=("running", "ready", "done"))
              for v in r.get("execution_observations", [])]
    rows = [{"id": "session"}]
    activity.annotate_kanban_activity(rows)
    return listed, rows[0].get("execution_observations", [])


def _seed(conn):
    running = kb.create_task(conn, title="Run", assignee="coder", session_id="session")
    assert kb.claim_task(conn, running) is not None
    done = kb.create_task(conn, title="Done", assignee="coder", session_id="session",
                          completion_contract="local-only")
    assert kb.claim_task(conn, done) is not None
    assert kb.complete_task(conn, done, summary="Finished",
                            metadata={"completion_receipt": receipt("delivered")})
    return running, done


@pytest.mark.parametrize("active", ["default", "coder"])
def test_shared_root_cards_are_owned_by_default_under_any_active_profile(root, monkeypatch, active):
    with _board(monkeypatch, root) as conn:
        running, done = _seed(conn)
    listed, counted = _cards(monkeypatch, active)
    for values in (listed, counted):
        by_id = {v["identity"]["work_id"]: v for v in values}
        assert {running, done} <= set(by_id), values
        for tid in (running, done):
            assert by_id[tid]["identity"]["profile"] == "default"
            assert by_id[tid]["owner"]["profile"] == "default"


def test_same_card_has_one_identity_key_across_reader_profiles(root, monkeypatch):
    with _board(monkeypatch, root) as conn:
        _seed(conn)
    keys = []
    for active in ("default", "coder"):
        listed, counted = _cards(monkeypatch, active)
        keys.append(sorted(repr(identity_key(v)) for v in listed + counted))
    assert keys[0] == keys[1]


def test_profile_rooted_board_is_owned_by_that_profile(root, monkeypatch):
    home = root / "profiles" / "build"
    home.mkdir(parents=True)
    with _board(monkeypatch, home) as conn:
        _seed(conn)
    listed, counted = _cards(monkeypatch, "default")
    for values in (listed, counted):
        assert {v["identity"]["profile"] for v in values} == {"build"}


def test_board_outside_root_has_no_guessed_profile(root, tmp_path_factory, monkeypatch):
    with _board(monkeypatch, tmp_path_factory.mktemp("elsewhere")) as conn:
        _seed(conn)
    listed, _ = _cards(monkeypatch, "default")
    # Never the reader's profile; an unqualified owner cannot carry a qualified state.
    assert listed and all(v["identity"].get("profile") is None for v in listed)
    assert {v["state"] for v in listed} == {"unknown"}


def test_card_detail_uses_board_owner_profile(root, monkeypatch):
    from hermes_cli.kanban_queue_position import detail_fields
    with _board(monkeypatch, root) as conn:
        running, _ = _seed(conn)
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "coder")
        out = detail_fields(conn, kb.get_task(conn, running), "default")
    cards = [v for v in out["execution_observations"] if v["identity"]["kind"] == "card"]
    assert cards and {v["identity"]["profile"] for v in cards} == {"default"}


@pytest.mark.parametrize("rel,expect", [("kanban.db", "default"), ("kanban/boards/x/kanban.db", "default"),
                                        ("profiles/p1/kanban.db", "p1"), ("profiles/.deleted/x/kanban.db", None),
                                        ("profiles/kanban.db", None)])
def test_board_owner_profile_rule(root, rel, expect):
    assert observations.board_owner_profile(root / rel) == expect
    assert observations.board_owner_profile(None) is None
