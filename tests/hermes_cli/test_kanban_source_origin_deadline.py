"""The ending's origin proof shares the preflight deadline and runs once per ending (card t_01a1d0b341560679)."""
from dataclasses import replace

import pytest

from hermes_cli import kanban_source_board as board_calls
from hermes_cli import kanban_source_clones as kcl
from hermes_cli import kanban_source_publish as pub
from hermes_cli import kanban_source_registration as reg
from tests.hermes_cli import test_kanban_source_publish as helpers

world = helpers.world
board = helpers.board
card = helpers.card


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _v2(card):
    run = replace(card.run(), source_protocol=2, source_identity=reg.source_identity(str(card.path), "task_worktree"))
    board_calls.record_run(card.conn, run)
    assert card.receipt()["state"] == "published"
    return card.run()


def _count_proofs(monkeypatch):
    calls = []
    real = kcl.prove_clone

    def counted(*a, **kw):
        calls.append(a)
        return real(*a, **kw)
    monkeypatch.setattr(kcl, "prove_clone", counted)
    return calls


def test_v2_new_commit_ending_proves_origin_once(card, monkeypatch):
    _v2(card)
    helpers.commit(card.path, "next.txt", "new")
    calls = _count_proofs(monkeypatch)
    assert card.receipt()["state"] == "published"
    assert len(calls) == 1


def test_origin_proof_git_calls_are_capped_by_the_remaining_deadline(card, monkeypatch):
    clock = Clock()
    deadline = pub._Deadline(3, clock=clock)
    seen = []
    real = kcl.git

    def recording(path, *args, timeout=30, check=True):
        seen.append(timeout)
        return real(path, *args, timeout=timeout, check=check)
    monkeypatch.setattr(kcl, "git", recording)
    pub.prove_origin(str(card.path), card.cfg, deadline=deadline)
    assert seen and max(seen) <= 3


def test_origin_proof_github_lookup_is_capped_by_the_remaining_deadline(card, monkeypatch):
    clock = Clock()
    seen = []
    monkeypatch.setattr(kcl, "_cache_path", lambda: card.world.tmp / "no-cache" / "proof.json")
    monkeypatch.setattr(kcl, "github_repo", lambda name, timeout=30: seen.append(timeout) or
                        {"id": 42, "full_name": "acme/widget"})
    pub.prove_origin(str(card.path), card.cfg, deadline=pub._Deadline(4, clock=clock))
    assert seen and max(seen) <= 4


def test_deadline_spent_during_origin_proof_is_preflight_timeout(card, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(kcl, "_cache_path", lambda: card.world.tmp / "no-cache" / "proof.json")

    def slow_lookup(name, timeout=30):
        clock.now += 10  # the API call used up the whole budget, then timed out
        raise TimeoutError("gh api timed out")
    monkeypatch.setattr(kcl, "github_repo", slow_lookup)
    with pytest.raises(pub.PublishRefused) as err:
        pub.prove_origin(str(card.path), card.cfg, deadline=pub._Deadline(5, clock=clock))
    assert err.value.code == "preflight_timeout"


def test_unbounded_origin_proof_keeps_its_own_failure_code(card, monkeypatch):
    monkeypatch.setattr(kcl, "_cache_path", lambda: card.world.tmp / "no-cache" / "proof.json")

    def failing(name, timeout=30):
        raise TimeoutError("gh api timed out")
    monkeypatch.setattr(kcl, "github_repo", failing)
    with pytest.raises(pub.PublishRefused) as err:
        pub.prove_origin(str(card.path), card.cfg)
    assert err.value.code == "origin_unreachable"
