"""The plugin start gate: a ``kanban_start_gate`` callback can keep a card ready and unclaimed for one tick. [daveey]"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_start_gate as gate
from hermes_cli import lifecycle
from hermes_cli import plugins
from tests.hermes_cli import test_kanban_llm_pacing as pacing
from tests.hermes_cli import test_kanban_model_tier as model_tier
from tests.hermes_cli.test_kanban_llm_pacing import _dispatch, _opus_card
from tests.hermes_cli.test_kanban_model_tier import _write_cfg

board = model_tier.board  # one unsynced gateway: every card is its own to lease
paced_board = pacing.paced_board  # that board with the governor's caches cleared


def _hook(monkeypatch, answers, seen=None):
    """Subscribe one fake plugin: ``answers`` is a list of returns, or a callable taking the hook kwargs."""
    def invoke(name, **kwargs):
        assert name == gate.HOOK
        if seen is not None:
            seen.append(kwargs)
        return answers(**kwargs) if callable(answers) else list(answers)
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == gate.HOOK)
    monkeypatch.setattr(lifecycle, "invoke_hook", invoke)


ASK = dict(task_id="t_1", lane="ready", assignee="default", board=None)


def test_no_subscriber_starts_the_card(monkeypatch):
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: False)
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda *a, **k: pytest.fail("not asked without a subscriber"))
    assert gate.hold(**ASK) is None


def test_a_hold_answer_names_its_reason(monkeypatch):
    seen = []
    _hook(monkeypatch, [None, {"hold": True, "reason": "token_throttle"}], seen)
    assert gate.hold(**ASK) == "start_gate:token_throttle"
    assert seen == [ASK]  # the callback gets the card, its lane, assignee and board
    _hook(monkeypatch, [{"hold": True, "reason": "Token Throttle: over the cap!!"}])
    assert gate.hold(**ASK) == "start_gate:token_throttle_over_the_cap"  # one lowercase word, bounded
    _hook(monkeypatch, [{"hold": True}])
    assert gate.hold(**ASK) == "start_gate:plugin"
    _hook(monkeypatch, [{"hold": True, "reason": "x" * 200}])
    assert gate.hold(**ASK) == "start_gate:" + "x" * 40


@pytest.mark.parametrize("answers", [[], [None], [False], ["hold"], [{"hold": False}], [{"hold": 1}],
                                     [{"hold": "true"}], [{"reason": "token_throttle"}], [[{"hold": True}]]])
def test_anything_but_an_exact_hold_starts_the_card(monkeypatch, answers):
    _hook(monkeypatch, answers)
    assert gate.hold(**ASK) is None


def test_a_failing_hook_starts_the_card(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("plugin fault")
    _hook(monkeypatch, boom)
    assert gate.hold(**ASK) is None


def test_the_hook_is_a_registered_hook_name():
    assert gate.HOOK in plugins.VALID_HOOKS


def test_dispatch_keeps_a_gated_card_ready_and_starts_it_once_the_gate_opens(paced_board, monkeypatch):
    _write_cfg(paced_board)
    holding = {"on": True}
    _hook(monkeypatch, lambda **kw: [{"hold": True, "reason": "token_throttle"}] if holding["on"] else [None])
    with kbc.connect() as conn:
        a, b = _opus_card(conn, "a"), _opus_card(conn, "b")
        seen, result = _dispatch(conn)
        assert seen == [] and kb.get_task(conn, a).status == "ready" and kb.get_task(conn, b).status == "ready"
        assert sorted(result.respawn_guarded) == sorted([(a, "start_gate:token_throttle"), (b, "start_gate:token_throttle")])
        holding["on"] = False
        seen, result = _dispatch(conn)
        assert sorted(seen) == sorted([a, b]) and not result.respawn_guarded  # nothing persisted: it just starts


def test_dispatch_dry_run_reports_the_gate_without_claiming(paced_board, monkeypatch):
    _write_cfg(paced_board)
    _hook(monkeypatch, [{"hold": True, "reason": "token_throttle"}])
    from hermes_cli import kanban_db_dispatch as kdd
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        result = kdd.dispatch_once(conn, dry_run=True)
        assert (tid, "start_gate:token_throttle") in result.respawn_guarded and kb.get_task(conn, tid).status == "ready"
