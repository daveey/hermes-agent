"""2026-11-20 sync outage: one board's ContainmentRefused must end only that board's round.

Before the fix the refusal escaped ``sync_all_once`` (and so ``run_forever``) and stopped every board on the host.
"""
from __future__ import annotations

import logging

import pytest

from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_refusal as refusal
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused


class _Ident:
    gateway = "gw-test"


class _Hub:
    ident = _Ident()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    refusal.reset()
    monkeypatch.setattr(ksc, "publish_pickup_if_changed", lambda hub: None)
    import hermes_cli.kanban_source_clones as sc
    monkeypatch.setattr(sc, "attest_if_due", lambda hub: None)
    yield
    refusal.reset()


def _boards(tmp_path):
    return [("refused", tmp_path / "refused.db"), ("healthy", tmp_path / "healthy.db")]


def test_refused_board_does_not_stop_the_other_boards(monkeypatch, tmp_path, caplog):
    synced = []

    def fake_sync_board(slug, path, hub, gateway):
        if slug == "refused":
            raise ContainmentRefused("replica event differs from its canonical receipt")
        synced.append(slug)
        return ksc.RoundResult(board=slug, mode="primary", pulled=3)

    monkeypatch.setattr(ksc, "sync_board", fake_sync_board)
    with caplog.at_level(logging.ERROR, logger="hermes_cli.kanban_sync_client"):
        results = ksc.sync_all_once(_Hub(), boards=_boards(tmp_path))

    assert synced == ["healthy"]
    by = {r.board: r for r in results}
    assert by["refused"].error == "ContainmentRefused: replica event differs from its canonical receipt"
    assert by["healthy"].error is None and by["healthy"].pulled == 3
    assert sum("round refused" in r.getMessage() for r in caplog.records) == 1


def test_refused_board_backs_off_then_retries_and_logs_once(monkeypatch, tmp_path, caplog):
    clock = {"t": 1000.0}
    monkeypatch.setattr(refusal.time, "monotonic", lambda: clock["t"])
    calls = {"refused": 0}
    state = {"fixed": False}

    def fake_sync_board(slug, path, hub, gateway):
        if slug == "refused":
            calls["refused"] += 1
            if not state["fixed"]:
                raise ContainmentRefused("canonical receipt identity changed")
        return ksc.RoundResult(board=slug, mode="primary")

    monkeypatch.setattr(ksc, "sync_board", fake_sync_board)
    boards = _boards(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="hermes_cli.kanban_sync_client"):
        ksc.sync_all_once(_Hub(), boards=boards)          # refused: backoff 60 s
        clock["t"] += 30
        held = {r.board: r for r in ksc.sync_all_once(_Hub(), boards=boards)}
        # still backing off: not retried, and not `skipped` (run_forever reads skipped as "retry in 0.5 s")
        assert calls["refused"] == 1 and not held["refused"].skipped and held["refused"].error
        clock["t"] += 31
        ksc.sync_all_once(_Hub(), boards=boards)          # retried, refused again: backoff 120 s
        assert calls["refused"] == 2
        clock["t"] += 61
        ksc.sync_all_once(_Hub(), boards=boards)          # still inside 120 s
        assert calls["refused"] == 2
        state["fixed"] = True
        clock["t"] += 60
        done = {r.board: r for r in ksc.sync_all_once(_Hub(), boards=boards)}
        assert calls["refused"] == 3 and done["refused"].error is None
        clock["t"] += 1
        ksc.sync_all_once(_Hub(), boards=boards)          # cleared: runs every round again
        assert calls["refused"] == 4

    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "round refused" in r.getMessage()]
    assert len(errors) == 1  # one ERROR per distinct refusal, not one per retry


def test_backoff_caps_at_fifteen_minutes():
    clock = {"t": 0.0}
    now = lambda: clock["t"]
    for _ in range(12):
        refusal.refused("b", ContainmentRefused("x"), now=now)
    assert refusal.waiting("b", now=now) is not None
    clock["t"] += refusal.MAX_BACKOFF_S
    assert refusal.waiting("b", now=now) is None


def test_run_forever_never_selects_or_wakes_on_a_held_board(tmp_path):
    """Review of #3759: a held board must leave run_forever's selection and wake set, not spin it every 0.5 s."""
    import inspect

    clock = {"t": 0.0}
    now = lambda: clock["t"]
    boards = _boards(tmp_path)
    refusal.refused("refused", ContainmentRefused("x"), now=now)
    assert refusal.runnable(boards, now=now) == [boards[1]]
    clock["t"] += refusal.FIRST_BACKOFF_S
    assert refusal.runnable(boards, now=now) == boards
    src = inspect.getsource(ksc._run_forever)
    assert "selected = refusal.runnable(selected)" in src
    assert "ready = refusal.runnable(" in src
