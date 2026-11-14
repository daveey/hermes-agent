"""Released roots stay minimal: an ancestor release replaces roots under it and a descendant of a released
root adds none, so repeated profile releases keep the set bounded without changing which stores are released."""
import contextlib
import sqlite3

import pytest

from hermes_cli.sqlite_safe_read import has_live_connection
from hermes_state_registry import close_all_under
from tui_gateway import watch_store


@pytest.fixture(autouse=True)
def clean():
    watch_store.reset()
    yield
    watch_store.reset()


def create(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        conn.commit()


def observe(path):
    with watch_store.observed(path) as (_key, conn):
        conn.execute("SELECT count(*) FROM sessions").fetchone()


def test_ancestor_release_compacts_descendant_roots(tmp_path):
    ancestor = tmp_path / "profiles"
    for n in range(3):
        close_all_under(ancestor / f"p{n}")
    assert len(watch_store._released_roots) == 3
    close_all_under(ancestor)
    assert watch_store._released_roots == {ancestor.resolve()}


def test_descendant_release_under_released_ancestor_adds_no_root(tmp_path):
    ancestor = tmp_path / "profiles"
    close_all_under(ancestor)
    for n in range(3):
        close_all_under(ancestor / f"p{n}")
    assert watch_store._released_roots == {ancestor.resolve()}


def test_compacted_root_keeps_inside_outside_policy(tmp_path):
    ancestor = tmp_path / "profiles"
    child = ancestor / "p0" / "state.db"
    sibling = ancestor / "p1" / "state.db"
    outside = tmp_path / "other" / "state.db"
    for path in (child, sibling, outside):
        create(path)
    close_all_under(child.parent)
    close_all_under(ancestor)
    for _ in range(2):
        for path in (child, sibling, outside):
            observe(path)
        assert not has_live_connection(child)
        assert not has_live_connection(sibling)
        assert has_live_connection(outside)
