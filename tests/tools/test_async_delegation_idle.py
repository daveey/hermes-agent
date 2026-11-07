"""An unchanged, terminally settled ledger needs no orphan scan. [daveey]"""
import queue


def test_unchanged_empty_ledger_skips_orphan_scan(monkeypatch):
    from tools import async_delegation as ad
    with ad._transaction():
        pass
    q = queue.Queue()
    ad.maybe_sweep_orphaned_completions(q, now=1000000)
    calls = []
    def forbidden():
        calls.append(True)
        raise AssertionError("unchanged ledger scanned")
    monkeypatch.setattr(ad, "recover_abandoned_delegations", forbidden)
    ad.maybe_sweep_orphaned_completions(q, now=1000031)
    assert calls == []


def test_replacement_and_errors_never_reuse_quiet_proof(tmp_path, monkeypatch):
    import sqlite3
    from tools import async_delegation_idle as idle
    path = tmp_path / "ledger.db"
    replacement = tmp_path / "replacement.db"
    for p in (path, replacement):
        with sqlite3.connect(p) as conn:
            conn.execute("CREATE TABLE async_delegations (state TEXT, delivery_state TEXT)")
            if p == replacement:
                conn.execute("INSERT INTO async_delegations VALUES ('running', NULL)")
    assert idle.ledger_quiet(path)
    replacement.replace(path)
    assert not idle.ledger_quiet(path)
    def failed(conn):
        raise sqlite3.OperationalError("read failed")
    monkeypatch.setattr(idle, "_has_work", failed)
    assert not idle.ledger_quiet(path)
    path.unlink()
    assert not idle.ledger_quiet(path)


def test_commit_racing_scan_is_rechecked(tmp_path, monkeypatch):
    import sqlite3
    from tools import async_delegation_idle as idle
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as writer:
        writer.execute("CREATE TABLE async_delegations (state TEXT, delivery_state TEXT)")
        scan = idle._has_work
        def racing(conn):
            result = scan(conn)
            writer.execute("INSERT INTO async_delegations VALUES ('completed', 'pending')")
            writer.commit()
            return result
        monkeypatch.setattr(idle, "_has_work", racing)
        assert idle.ledger_quiet(path)
        monkeypatch.setattr(idle, "_has_work", scan)
        assert not idle.ledger_quiet(path)


def test_commit_invalidates_quiet_proof_and_pending_is_never_cached(tmp_path, monkeypatch):
    import sqlite3
    from tools import async_delegation_idle as idle
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as writer:
        writer.execute("CREATE TABLE async_delegations (state TEXT, delivery_state TEXT)")
        writer.commit()
        assert idle.ledger_quiet(path)
        scan = idle._has_work
        monkeypatch.setattr(idle, "_has_work", lambda c: (_ for _ in ()).throw(AssertionError("scan")))
        assert idle.ledger_quiet(path)
        monkeypatch.setattr(idle, "_has_work", scan)
        writer.execute("INSERT INTO async_delegations VALUES ('completed', 'pending')")
        writer.commit()
        assert not idle.ledger_quiet(path)
        assert not idle.ledger_quiet(path)
        writer.execute("UPDATE async_delegations SET delivery_state='delivered'")
        writer.commit()
        assert idle.ledger_quiet(path)
        writer.execute("INSERT INTO async_delegations VALUES ('running', NULL)")
        writer.commit()
        assert not idle.ledger_quiet(path)
