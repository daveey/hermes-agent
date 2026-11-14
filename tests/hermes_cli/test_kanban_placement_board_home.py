"""``kanban_placement.config()`` falls back to the board home's ``kanban.placement``. [daveey]

Managed worker profiles (bb-tuner, coach, alpha-follow) carry a ``kanban:`` section with no ``placement``
block, while the shared board's policy lives in the root ``config.yaml``. Before the fallback a create under
such a profile read ``{}``: no ``pool:worker`` queue, no home markers (t_01a1db0fb5d6a9cc).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_placement as kp

ROOT_PLACEMENT = """\
kanban:
  placement:
    worker_queue:
      worker-7: pool:worker
    worker_pools:
      hub: [worker-7]
    extra_home_markers: [/srv/only-here]
"""


def _homes(tmp_path: Path, profile_kanban: str) -> tuple[Path, Path]:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "bb-tuner"
    profile.mkdir(parents=True)
    (root / "config.yaml").write_text(ROOT_PLACEMENT)
    (profile / "config.yaml").write_text(profile_kanban)
    return root, profile


@pytest.fixture
def no_kanban_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)


def test_profile_without_placement_reads_the_board_home_block(tmp_path, monkeypatch, no_kanban_override):
    root, profile = _homes(tmp_path, "kanban:\n  review_dispatch: true\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    from hermes_cli.kanban_db import kanban_home
    assert kanban_home() == root  # the profile's board is the root's board
    cfg = kp.config()
    assert cfg["worker_queue"] == {"worker-7": "pool:worker"}
    assert cfg["extra_home_markers"] == ["/srv/only-here"]


def test_profile_with_its_own_placement_wins_whole(tmp_path, monkeypatch, no_kanban_override):
    _, profile = _homes(tmp_path, "kanban:\n  placement:\n    worker_queue:\n      worker-9: pool:other\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert kp.config() == {"worker_queue": {"worker-9": "pool:other"}}


def test_explicit_kanban_home_is_where_the_fallback_reads(tmp_path, monkeypatch):
    board = tmp_path / "board"
    board.mkdir()
    (board / "config.yaml").write_text(ROOT_PLACEMENT)
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "config.yaml").write_text("kanban: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(lone))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(board))
    assert kp.config()["worker_pools"] == {"hub": ["worker-7"]}


def test_no_placement_anywhere_stays_empty(tmp_path, monkeypatch, no_kanban_override):
    root, profile = _homes(tmp_path, "kanban:\n  review_dispatch: true\n")
    (root / "config.yaml").write_text("kanban:\n  review_dispatch: true\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert kp.config() == {}


def test_snapshot_pins_the_fallback_block(tmp_path, monkeypatch, no_kanban_override):
    root, profile = _homes(tmp_path, "kanban: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    with kp.config_snapshot() as snap:
        (root / "config.yaml").write_text("kanban: {}\n")
        assert kp.config() is snap
        assert snap["worker_queue"] == {"worker-7": "pool:worker"}
