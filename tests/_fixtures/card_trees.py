"""Card worktree lookups never read the developer's real ``~/code/_wt`` in tests. [daveey]"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _card_trees_off_the_real_home(monkeypatch, tmp_path_factory):
    """Card worktree lookups (``kanban_move_pool.card_trees``: every move and pool park) glob ``~/code/_wt`` etc.
    HOME stays real in tests, so on a developer host they would read real trees (and their gitdirs under the real
    Hermes home). Unless a test points ``Path.home`` at its own tmp dir, they see an empty home."""
    from hermes_cli import kanban_move_pool

    real = Path.home()
    empty = tmp_path_factory.getbasetemp() / "no-card-trees-home"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr(kanban_move_pool, "_trees_home", lambda: empty if Path.home() == real else Path.home())
