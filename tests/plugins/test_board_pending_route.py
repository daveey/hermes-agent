"""A board scope still activating answers 503, not a 500 (``board_fact_views.response``/``legacy``). [daveey]

``BoardRuntime.provider`` returns ``Pending('board_activating')`` while a scope's owner starts; the route called
``.read()`` on it (AttributeError) because only ``Unavailable`` was handled.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hermes_cli.fact_contract import Pending, Ready, Unavailable
from plugins.kanban.dashboard import board_fact_views


@pytest.fixture
def pending(monkeypatch):
    v1 = SimpleNamespace(Ready=Ready, Pending=Pending, Unavailable=Unavailable)
    monkeypatch.setattr(board_fact_views, '_installed', (v1, lambda board: Pending('board_activating')))
    return v1


def test_an_activating_scope_answers_503_with_its_reason(pending):
    with pytest.raises(HTTPException) as error:
        board_fact_views.response('board', board_fact_views.board_selector(), board='default')
    assert error.value.status_code == 503
    assert error.value.detail == {'code': 'facts_unavailable', 'reason': 'board_activating'}


def test_the_legacy_guard_answers_503_too(pending):
    with pytest.raises(HTTPException) as error:
        board_fact_views.legacy('default')
    assert error.value.status_code == 503 and error.value.detail['reason'] == 'board_activating'
