"""Kept-reader lookup does not repeatedly resolve every path component."""
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

from hermes_cli import kanban_sync_board_watch as watch_module

SQL = 'SELECT value FROM probe'


def make_board(path, value):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE probe(value INTEGER)')
    conn.execute('INSERT INTO probe VALUES (?)', (value,))
    conn.commit()
    conn.close()
    return path


def test_warm_queries_do_not_walk_path_components(tmp_path, monkeypatch):
    path = make_board(tmp_path / 'board.db', 1)
    watch = watch_module.BoardWatch()
    try:
        assert watch.query(path, SQL, unchanged_ok=True)[1] == (1,)
        realpath = Mock(wraps=os.path.realpath)
        # Replace only this module's os reference, not the process-wide guard.
        monkeypatch.setattr(watch_module, 'os', SimpleNamespace(
            stat=os.stat, path=SimpleNamespace(realpath=realpath)))
        assert watch.query(path, SQL, unchanged_ok=True)[1] == (1,)
        assert watch.query(path, SQL, unchanged_ok=True)[1] == (1,)
        assert realpath.call_count == 0
    finally:
        watch.reset()


def test_directory_symlink_retarget_changes_reader_generation(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    make_board(a / 'board.db', 1)
    make_board(b / 'board.db', 2)
    link = tmp_path / 'current'
    link.symlink_to(a, target_is_directory=True)
    watch = watch_module.BoardWatch()
    try:
        before = watch.query(link / 'board.db', SQL, unchanged_ok=True)
        link.unlink()
        link.symlink_to(b, target_is_directory=True)
        after = watch.query(link / 'board.db', SQL, unchanged_ok=True)
        assert before[1] == (1,) and after[1] == (2,)
        assert before[0] != after[0]
        assert len(watch._conns) == 1
    finally:
        watch.reset()


def test_alias_read_error_drops_its_reader_and_answer(tmp_path):
    path = make_board(tmp_path / 'board.db', 1)
    link = tmp_path / 'alias.db'
    link.symlink_to(path)
    watch = watch_module.BoardWatch()
    try:
        assert watch.query(link, SQL, unchanged_ok=True)[1] == (1,)
        next(iter(watch._conns.values()))[1].close()
        assert watch.query(link, SQL, unchanged_ok=True) is None
        assert not watch._conns and not watch._answers
        assert watch.query(link, SQL, unchanged_ok=True)[1] == (1,)
        link.unlink()
        assert watch.release_gone() == 1
        assert not watch._conns
    finally:
        watch.reset()


def test_alias_readers_remain_bounded(tmp_path):
    path = make_board(tmp_path / 'board.db', 1)
    watch = watch_module.BoardWatch()
    watch.MAX = 2
    handles = []
    try:
        for index in range(4):
            link = tmp_path / f'alias-{index}.db'
            link.symlink_to(path)
            assert watch.query(link, SQL, unchanged_ok=True)[1] == (1,)
            handles.append(list(watch._conns.values())[-1][1])
            assert len(watch._conns) <= 2
        import pytest
        with pytest.raises(sqlite3.ProgrammingError):
            handles[0].execute('SELECT 1')
    finally:
        watch.reset()


def test_aliases_and_relative_parent_keep_filesystem_semantics(tmp_path, monkeypatch):
    parent = tmp_path / 'parent'
    nested = parent / 'nested'
    nested.mkdir(parents=True)
    make_board(tmp_path / 'board.db', 1)
    make_board(parent / 'board.db', 2)
    link = tmp_path / 'link'
    link.symlink_to(nested, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    watch = watch_module.BoardWatch()
    try:
        # Lexically collapsing link/.. would read the WRONG board (value 1).
        relative = type(link)('link/../board.db')
        assert watch.query(relative, SQL, unchanged_ok=True)[1] == (2,)
        assert watch.query(parent / 'board.db', SQL, unchanged_ok=True)[1] == (2,)
        conn = sqlite3.connect(parent / 'board.db')
        conn.execute('UPDATE probe SET value=3')
        conn.commit()
        conn.close()
        assert watch.query(relative, SQL, unchanged_ok=True)[1] == (3,)
        assert watch.query(parent / 'board.db', SQL, unchanged_ok=True)[1] == (3,)
    finally:
        watch.reset()
