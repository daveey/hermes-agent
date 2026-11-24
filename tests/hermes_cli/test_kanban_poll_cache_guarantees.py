"""Freshness and bounded-work contracts for Desktop poll cache revalidation."""

import os
from collections import Counter

from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_unchanged_inputs as helpers

clock = helpers.clock
spies = helpers.spies


def test_recent_stat_board_same_tick_write_is_read_after_ttl(clock, spies, tmp_path):
    path = tmp_path / "kanban.db"
    conn = helpers._board(path, [("t1", "running", "s1")], counted=False)
    try:
        recent = int((clock["wall"] - 0.5) * 1e9)
        os.utime(path, ns=(recent, recent))
        assert helpers._poll()["kanban_active"] == 1
        before = ksa._activity_fingerprint()
        conn.execute("UPDATE tasks SET status = 'archived'")
        os.utime(path, ns=(recent, recent))
        # Equal file stats no longer hide commits or replacement reader identities.
        after = ksa._activity_fingerprint()
        assert after[0][1][1] == before[0][1][1]  # same filesystem stat
        assert after != before
        helpers._advance(clock, ksa._CACHE_TTL_S + 0.5)
        assert "kanban_active" not in helpers._poll()
        assert spies["recompute"] == 2
    finally:
        conn.close()


def test_replaced_reader_cannot_reuse_same_stat_board_row(clock, spies, tmp_path):
    # PRAGMA data_version is connection-local: a reader opened after a commit can report the value the evicted one
    # saw before it, so only the reader's identity in the memo key tells the two apart (fix-F3, t_01a19ec10011d888).
    path = tmp_path / "kanban.db"
    conn = helpers._board(path, [("t1", "running", "s1")], counted=False)
    try:
        recent = int((clock["wall"] - 0.5) * 1e9)
        os.utime(path, ns=(recent, recent))
        assert helpers._poll()["kanban_active"] == 1
        before = ksa._activity_fingerprint()
        with ksa.cached_ro(path) as reader:
            evicted = reader
        conn.execute("UPDATE tasks SET status = 'archived'")
        os.utime(path, ns=(recent, recent))
        ksa.evict_readers(os.path.realpath(path))
        # Equal file stats no longer hide commits or replacement reader identities.
        after = ksa._activity_fingerprint()
        assert after[0][1][1] == before[0][1][1]  # same filesystem stat
        assert after != before
        with ksa.cached_ro(path) as reader:
            assert reader is not evicted
        helpers._advance(clock, ksa._CACHE_TTL_S + 0.5)
        assert "kanban_active" not in helpers._poll()
        assert spies["recompute"] == 2
    finally:
        conn.close()


def test_recent_stat_attention_rechecks_verdict_after_ttl(clock, tmp_path):
    path = tmp_path / "kanban.db"
    conn = helpers._board(path, [], counted=False)
    conn.execute("ALTER TABLE tasks ADD COLUMN title TEXT")
    conn.execute("ALTER TABLE tasks ADD COLUMN completed_at REAL")
    conn.execute("ALTER TABLE tasks ADD COLUMN result TEXT")
    conn.execute("INSERT INTO tasks VALUES ('t1', 'done', 's1', 'ship', ?, 'done')", (clock["wall"],))
    checked = []
    dispose = ksa.register_done_check("recent-stat", lambda card: checked.append(card["id"]) or True)
    try:
        recent = int((clock["wall"] - 0.5) * 1e9)
        os.utime(path, ns=(recent, recent))
        assert ksa.attention_task_counts() == {"s1": 1}
        helpers._advance(clock, ksa._ATTENTION_TTL_S + 1)
        assert ksa.attention_task_counts() == {"s1": 1}
        assert checked == ["t1", "t1"]
    finally:
        dispose()
        conn.close()


def test_inline_poll_reads_each_board_revision_once(clock, spies, tmp_path, monkeypatch):
    paths = [tmp_path / "kanban.db", tmp_path / "kanban" / "boards" / "second" / "kanban.db"]
    conns = [helpers._board(p, [(f"t{i}", "running", "s1")]) for i, p in enumerate(paths)]
    reads = Counter()
    real = ksa._board_content_key

    def counted(path, *args):
        reads[path] += 1
        return real(path, *args)

    monkeypatch.setattr(ksa, "_board_content_key", counted)
    try:
        assert ksa._board_db_paths() == paths
        assert all(real(path)[0] == "revision" for path in paths)
        assert helpers._poll()["kanban_active"] == 2
        cold_threads = spies["threads"]  # a cold start reads on a bounded worker
        reads.clear()
        helpers._advance(clock, ksa._CACHE_TTL_S + 0.5)
        assert helpers._poll()["kanban_active"] == 2
        assert {k: spies[k] for k in ("recompute", "threads")} == {"recompute": 1, "threads": cold_threads}
        assert reads == Counter({path: 1 for path in paths})
    finally:
        for conn in conns:
            conn.close()


def test_expired_attention_with_moved_key_requires_bounded_refresh(clock, spies, tmp_path):
    conn = helpers._done_board(tmp_path / "kanban.db")
    conn.execute("INSERT INTO tasks VALUES ('t1', 'done', 's1', 'ship', ?, 'done')", (clock["wall"],))
    checked = []
    dispose = ksa.register_done_check("bounded", lambda card: checked.append(card["id"]) or True)
    try:
        assert helpers._poll()["kanban_attention"] == 1
        cold_threads = spies["threads"]  # a cold start reads on a bounded worker
        helpers._advance(clock, ksa._ATTENTION_TTL_S + 1)
        conn.execute("INSERT INTO tasks VALUES ('t2', 'done', 's1', 'ship', ?, 'done')", (clock["wall"],))
        # Another consumer refreshes counts, not attention: only the latter is stale.
        ksa._all_counts()
        scope = ksa._origin_scope()
        assert ksa._revalidate.unchanged_inputs(scope) is None
        assert checked == ["t1"]
        assert helpers._poll()["kanban_attention"] == 2
        assert spies["threads"] == cold_threads + 1
        assert sorted(checked) == ["t1", "t1", "t2"]
    finally:
        dispose()
        conn.close()


def test_other_scope_insert_retains_expired_unchanged_attention(clock, tmp_path, monkeypatch):
    conn = helpers._done_board(tmp_path / "kanban.db")
    conn.execute("INSERT INTO tasks VALUES ('t1', 'done', 's1', 'ship', ?, 'done')", (clock["wall"],))
    checked = []
    scope = ["first"]
    monkeypatch.setattr(ksa, "_origin_scope", lambda: tuple(scope))
    dispose = ksa.register_done_check("scopes", lambda card: checked.append(card["id"]) or True)
    try:
        assert ksa.attention_task_counts() == {"s1": 1}
        helpers._advance(clock, ksa._ATTENTION_TTL_S + 1)
        scope[0] = "second"
        assert ksa.attention_task_counts() == {"s1": 1}
        assert checked == ["t1", "t1"]
        # Let the shared verdict expire too, so it cannot mask an evicted count.
        helpers._advance(clock, ksa._ATTENTION_TTL_S + 1)
        scope[0] = "first"
        assert ksa.attention_task_counts() == {"s1": 1}
        assert checked == ["t1", "t1"]
    finally:
        dispose()
        conn.close()
