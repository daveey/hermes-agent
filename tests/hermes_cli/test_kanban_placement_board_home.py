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


@pytest.mark.parametrize("broken", ["read_config_doc_readonly", "config_exists"])
def test_unreadable_board_config_means_empty_policy(tmp_path, monkeypatch, no_kanban_override, broken):
    """A profile with no placement block whose board-home config cannot be read gets ``{}``, not the exception."""
    from hermes_cli import config_backend

    _, profile = _homes(tmp_path, "kanban:\n  review_dispatch: true\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    calls: list[str] = []

    def boom(*_a, **_k):
        calls.append(broken)
        raise OSError("board config unreadable")

    monkeypatch.setattr(config_backend, broken, boom)
    assert kp.config() == {}
    assert calls == [broken]  # the board-home read was reached and failed, not skipped


def test_unreadable_board_config_does_not_hide_the_profile_block(tmp_path, monkeypatch, no_kanban_override):
    """Control: a profile's own non-empty block wins and never touches the (broken) board-home read."""
    from hermes_cli import config_backend

    _, profile = _homes(tmp_path, "kanban:\n  placement:\n    worker_queue:\n      worker-9: pool:other\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))

    def boom(*_a, **_k):
        raise OSError("board config unreadable")

    monkeypatch.setattr(config_backend, "read_config_doc_readonly", boom)
    monkeypatch.setattr(config_backend, "config_exists", boom)
    assert kp.config() == {"worker_queue": {"worker-9": "pool:other"}}


def test_snapshot_pins_the_empty_policy_of_an_unreadable_board_config(tmp_path, monkeypatch, no_kanban_override):
    """Control: a snapshot taken while the board config is unreadable pins ``{}`` for the whole block."""
    from hermes_cli import config_backend

    _, profile = _homes(tmp_path, "kanban: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    real = config_backend.read_config_doc_readonly

    def boom(*_a, **_k):
        raise OSError("board config unreadable")

    monkeypatch.setattr(config_backend, "read_config_doc_readonly", boom)
    with kp.config_snapshot() as snap:
        assert snap == {}
        monkeypatch.setattr(config_backend, "read_config_doc_readonly", real)
        assert kp.config() is snap
    assert kp.config()["worker_queue"] == {"worker-7": "pool:worker"}  # the next block loads afresh
