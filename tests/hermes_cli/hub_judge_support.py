"""Fake hub-admission judge for placement suites: tests never reach a live model. [daveey]

Legacy placement suites pin the pre-admission decision logic, so their default judge admits every
hub candidate (the old "stays on the hub" outcome) and records the call; a test that cares about the
deny path flips ``judge.admit_next = False``.
"""
from __future__ import annotations

import pytest


class FakeJudge:
    def __init__(self):
        self.calls: list[dict] = []
        self.admit_next = True

    def __call__(self, card, *, hub_reason=None):
        from hermes_cli.kanban_hub_admission import Verdict
        self.calls.append({"card": dict(card), "hub_reason": hub_reason})
        if self.admit_next:
            return Verdict(admit=True, need="hub_live_process", evidence=str(card.get("title") or ""),
                           why="fake judge admits")
        return Verdict(admit=False, why="fake judge denies")


@pytest.fixture
def hub_judge(monkeypatch):
    from hermes_cli import kanban_hub_admission as judge
    fake = FakeJudge()
    monkeypatch.setattr(judge, "admit", fake)
    return fake
