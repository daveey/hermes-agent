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


def _legacy_key(path):
    from pathlib import Path
    return str(Path(path).absolute())


class _OddPath(type(watch_module.Path())):
    """A path whose text is not its location: only the platform's own class may skip the expression."""

    def __str__(self):
        return '/nowhere/odd.db'


def _spellings(tmp_path):
    from pathlib import Path
    board = tmp_path / 'parent' / 'board.db'
    return [
        board, board.parent / 'nested' / '..' / 'board.db', tmp_path / 'link' / '..' / 'board.db',
        Path(str(board) + '/'), Path('//' + str(board).lstrip('/')), Path(str(tmp_path) + '/./parent/board.db'),
        board.absolute(), board.resolve(), board.with_name('other.db'), Path('/'),
        Path('board.db'), Path('parent/board.db'), Path('link/../board.db'), Path('./board.db'), Path('.'), Path(''),
        str(board), 'board.db', 'link/../board.db', str(board) + '/', '//' + str(board).lstrip('/'), '',
        _OddPath(board), _OddPath('board.db'),
    ]


def test_reader_key_is_the_legacy_expression(tmp_path, monkeypatch):
    other = tmp_path / 'elsewhere'
    other.mkdir()
    for cwd in (tmp_path, other):
        monkeypatch.chdir(cwd)
        for spelling in _spellings(tmp_path):
            assert watch_module.reader_key(spelling) == _legacy_key(spelling), (cwd, spelling)


def test_warm_query_builds_no_path(tmp_path, monkeypatch):
    path = make_board(tmp_path / 'board.db', 1)
    watch = watch_module.BoardWatch()
    try:
        assert watch.query(path, SQL, unchanged_ok=True)[1] == (1,)
        built = Mock(wraps=watch_module.Path)
        monkeypatch.setattr(watch_module, 'Path', built)
        assert watch.query(path, SQL, unchanged_ok=True)[1] == (1,)
        assert watch.query(path, SQL)[1] == (1,)
        assert built.call_count == 0
        # A spelling the short cut does not cover is still spelled by the expression, once a read.
        assert watch.query(str(path), SQL, unchanged_ok=True)[1] == (1,)
        assert built.call_count == 1
        assert list(watch._conns) == [str(path)]
    finally:
        watch.reset()


def test_relative_key_tracks_cwd(tmp_path, monkeypatch):
    from pathlib import Path
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    make_board(a / 'board.db', 1)
    make_board(b / 'board.db', 2)
    watch = watch_module.BoardWatch()
    try:
        for spelling in (Path('board.db'), 'board.db'):
            monkeypatch.chdir(a)
            assert watch.query(spelling, SQL, unchanged_ok=True)[1] == (1,)
            monkeypatch.chdir(b)
            assert watch.query(spelling, SQL, unchanged_ok=True)[1] == (2,)
            assert set(watch._conns) == {str(a / 'board.db'), str(b / 'board.db')}
            assert set(watch._answers) == set(watch._conns)
    finally:
        watch.reset()


def test_reader_and_answers_share_one_key(tmp_path, monkeypatch):
    from pathlib import Path
    make_board(tmp_path / 'board.db', 1)
    make_board(tmp_path / 'next.db', 2)
    monkeypatch.chdir(tmp_path)
    watch = watch_module.BoardWatch()
    try:
        for spelling in (Path('board.db'), tmp_path / 'board.db', _OddPath(tmp_path / 'board.db')):
            assert watch.query(spelling, SQL, unchanged_ok=True)[1] == (1,)
            assert list(watch._conns) == list(watch._answers) == [str(tmp_path / 'board.db')]
    finally:
        watch.reset()


def test_replaced_file_revalidates_warm_reader(tmp_path):
    path = make_board(tmp_path / 'board.db', 1)
    old_image = path.read_bytes()
    newer = make_board(tmp_path / 'newer.db', 2)
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))  # any later write moves the mtime, whatever its grain
    watch = watch_module.BoardWatch()
    try:
        first = watch.query(path, SQL, unchanged_ok=True)
        assert first[1] == (1,)
        os.replace(newer, path)  # another inode under the same name
        second = watch.query(path, SQL, unchanged_ok=True)
        assert second[1] == (2,) and second[0] != first[0]
        os.utime(path, ns=(2_000_000_000, 2_000_000_000))
        assert watch.query(path, SQL, unchanged_ok=True)[1] == (2,)
        reader = watch._conns[str(path)][1]
        with open(path, 'r+b') as raw:  # the same inode, older bytes written over it
            raw.write(old_image)
            raw.truncate()
        third = watch.query(path, SQL, unchanged_ok=True)
        assert third[1] == (1,)
        assert watch._conns[str(path)][1] is not reader
    finally:
        watch.reset()
