"""Nonjournaled foreign appendages bind their original canonical snapshot. [daveey]"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_home_command as home
from hermes_cli import kanban_foreign_comment as comments
from tests.hermes_cli import test_kanban_foreign_comment as support

foreign = support.foreign
live = support.live


def test_foreign_comment_and_writer_event_keep_one_canonical_epoch(foreign, monkeypatch):
    primary, _, _, task, _, _, _, _ = foreign
    hub = support.setup(monkeypatch, task)
    hub.target_damage = {'home_generation': 'minted-epoch-00001', 'home_transfer_pending': None}
    comments.append(primary, task, 'actor', 'new mandate', writer_kind='agent', writer_session='session')
    assert len(hub.sent) == 2
    assert all(op.get('home_generations') == {task: 'minted-epoch-00001'} for op in hub.sent)


def test_foreign_comment_pending_refuses_before_any_post(foreign, monkeypatch):
    primary, _, _, task, _, _, _, _ = foreign
    hub = support.setup(monkeypatch, task)
    hub.target_damage = {'home_generation': 'minted-epoch-00001', 'home_transfer_pending': 'pending'}
    with pytest.raises(ValueError, match='home transfer pending'):
        comments.append(primary, task, 'actor', 'new mandate', writer_kind='agent', writer_session='session')
    assert not hub.sent


def test_evidence_capture_is_strict_but_keeps_legacy_absence():
    assert home.from_evidence('t_fixture', {'id':'t_fixture'}) is None
    for row in [{'id':'other'}, {'id':'t_fixture', 'home_generation': []},
                {'id':'t_fixture', 'home_generation': ''}]:
        with pytest.raises(ValueError):
            home.from_evidence('t_fixture', row)
