"""Optional explicit-operator attribution on the core-update requeue reclaim. [daveey]

The worker core-update containment (state ``hermes-sync``) lets only an explicit operator
emergency interrupt managed worker runs. That route keeps the exact requeue reason and adds
``urgency_source="explicit_operator"`` plus ``interrupt_target`` (the 40-hex minimum core
target) to the existing durable ``reclaimed`` event, so a later 6 h query can tell explicit
interruptions from routine (now forbidden) ones. Legacy reclaims carry neither field
(= unknown). Invalid or partial attribution is refused before any signal or claim change.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_core_update_requeue import (
    CORE_UPDATE_REQUEUE_REASON,
    reclaim_attribution,
)

TARGET = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Never signal a real process from these tests.
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker",
                        lambda *a, **k: {"termination_attempted": False})
    kb.init_db()
    return home


def _parser():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(parser.add_subparsers(dest="command"))
    return parser


def _running(title="t"):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=title, assignee="default")
        assert kb.claim_task(conn, tid) is not None
    return tid


def _reclaims(tid):
    with kbc.connect_closing() as conn:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'reclaimed'", (tid,)).fetchall()
        status = kb.get_task(conn, tid).status
    return [json.loads(r["payload"]) for r in rows], status


def _cli(*argv):
    args = _parser().parse_args(["kanban", *argv])
    return kc.kanban_command(args)


def test_parser_to_event_carries_explicit_operator_attribution(kanban_home):
    tid = _running()
    rc = _cli("reclaim", tid, "--reason", CORE_UPDATE_REQUEUE_REASON,
              "--urgency-source", "explicit_operator", "--interrupt-target", TARGET)
    assert rc == 0
    events, status = _reclaims(tid)
    assert status == "ready"
    assert len(events) == 1
    assert events[0]["reason"] == CORE_UPDATE_REQUEUE_REASON
    assert events[0]["urgency_source"] == "explicit_operator"
    assert events[0]["interrupt_target"] == TARGET


def test_legacy_reclaim_payload_is_unchanged(kanban_home):
    tid = _running()
    assert _cli("reclaim", tid, "--reason", CORE_UPDATE_REQUEUE_REASON) == 0
    events, _ = _reclaims(tid)
    assert len(events) == 1
    assert "urgency_source" not in events[0]
    assert "interrupt_target" not in events[0]


def test_help_lists_both_flags(capsys):
    with pytest.raises(SystemExit) as exc:
        _parser().parse_args(["kanban", "reclaim", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--urgency-source" in out and "--interrupt-target" in out


def test_unknown_urgency_source_is_a_usage_error(kanban_home):
    tid = _running()
    with pytest.raises(SystemExit) as exc:
        _parser().parse_args(["kanban", "reclaim", tid, "--reason", CORE_UPDATE_REQUEUE_REASON,
                              "--urgency-source", "routine", "--interrupt-target", TARGET])
    assert exc.value.code == 2
    _, status = _reclaims(tid)
    assert status == "running"


@pytest.mark.parametrize("extra", [
    ["--urgency-source", "explicit_operator"],                       # target missing
    ["--interrupt-target", TARGET],                                  # source missing
    ["--urgency-source", "explicit_operator", "--interrupt-target", TARGET.upper()],
    ["--urgency-source", "explicit_operator", "--interrupt-target", TARGET[:-1]],
    ["--urgency-source", "explicit_operator", "--interrupt-target", TARGET + "0"],
    ["--urgency-source", "explicit_operator", "--interrupt-target", "g" * 40],
    ["--urgency-source", "explicit_operator", "--interrupt-target", " " + TARGET[1:]],
])
def test_invalid_or_partial_attribution_refuses_before_reclaim(kanban_home, extra, capsys):
    tid = _running()
    rc = _cli("reclaim", tid, "--reason", CORE_UPDATE_REQUEUE_REASON, *extra)
    assert rc != 0
    assert "attribution" in capsys.readouterr().err
    events, status = _reclaims(tid)
    assert events == [] and status == "running"


def test_attribution_requires_the_exact_requeue_reason(kanban_home, capsys):
    tid = _running()
    rc = _cli("reclaim", tid, "--reason", "something else",
              "--urgency-source", "explicit_operator", "--interrupt-target", TARGET)
    assert rc != 0
    events, status = _reclaims(tid)
    assert events == [] and status == "running"


def test_db_layer_refuses_before_signalling(kanban_home, monkeypatch):
    tid = _running()
    signalled = []
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda *a, **k: signalled.append(a) or {})
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError):
            kb.reclaim_task(conn, tid, reason=CORE_UPDATE_REQUEUE_REASON,
                            urgency_source="explicit_operator", interrupt_target="nope")
    assert signalled == []
    assert _reclaims(tid) == ([], "running")


@pytest.mark.parametrize("source,target", [
    (None, None), ("explicit_operator", TARGET)])
def test_reclaim_attribution_valid(source, target):
    got = reclaim_attribution(CORE_UPDATE_REQUEUE_REASON, source, target)
    assert got == ({} if source is None else {"urgency_source": source, "interrupt_target": target})


@pytest.mark.parametrize("source,target", [
    ("explicit_operator", None), (None, TARGET), ("routine", TARGET), (True, TARGET),
    ("explicit_operator", 123), ("explicit_operator", TARGET + "\n"), ("", TARGET)])
def test_reclaim_attribution_invalid(source, target):
    with pytest.raises(ValueError):
        reclaim_attribution(CORE_UPDATE_REQUEUE_REASON, source, target)
