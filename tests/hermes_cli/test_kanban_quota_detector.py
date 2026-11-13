"""Isolated detector proofs; never open a real board."""
from concurrent.futures import ThreadPoolExecutor


def test_threshold_persists_across_instances_and_scopes(tmp_path):
    from hermes_cli.kanban_quota_detector import Detector
    path = tmp_path / 'detector.db'
    for now in (100, 101):
        assert Detector(path).record('host', 'anthropic', 'model', now=now) is None
    assert Detector(path).record('other', 'anthropic', 'model', now=102) is None
    report = Detector(path).record('host', 'anthropic', 'model', now=103)
    assert report['count'] == 3
    assert report['host'] == 'host'
    assert len(Detector(path).pending()) == 1


def test_expired_events_do_not_count(tmp_path):
    from hermes_cli.kanban_quota_detector import Detector
    detector = Detector(tmp_path / 'detector.db')
    assert detector.record('h', 'p', 'm', now=0) is None
    assert detector.record('h', 'p', 'm', now=1) is None
    assert detector.record('h', 'p', 'm', now=1802) is None


def test_concurrent_threshold_has_one_outbox_receipt(tmp_path):
    from hermes_cli.kanban_quota_detector import Detector
    # Exercise transactional dedup, not the production fail-soft busy deadline under CI starvation.
    detector = Detector(tmp_path / 'detector.db', timeout=5)
    detector.record('h', 'p', 'm', now=10)
    detector.record('h', 'p', 'm', now=11)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: detector.record('h', 'p', 'm', now=12), range(8)))
    assert sum(result is not None for result in results) == 1
    assert len(detector.pending()) == 1


def test_owner_delivery_failure_retains_pending(tmp_path):
    from hermes_cli.kanban_quota_detector import Detector
    detector = Detector(tmp_path / 'detector.db')
    for now in (1, 2, 3):
        detector.record('h', 'p', 'm', now=now)
    def offline(report):
        raise OSError('offline')
    assert detector.deliver(offline) == 0
    assert len(detector.pending()) == 1
    receipts = []
    assert detector.deliver(lambda report: receipts.append(report) or 'card-1') == 1
    assert receipts[0]['key'].startswith('quota-contradiction:')
    assert detector.pending() == []


ACCTS = [{'id': 'acct-1', 'cooldown_until': 5000, 'token': 'sk-secret', 'email': 'a@b.c'},
         {'id': 'a@b.c', 'cooldown_until': 1}, 'acct-2']


def _board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'kanban.db'))
    for k in ('HERMES_KANBAN_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(k, raising=False)
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    return kb, kbc.connect(tmp_path / 'kanban.db')


def _three(path, **kw):
    from hermes_cli.kanban_quota_detector import record_contradiction
    for now in (100, 101, 102):
        record_contradiction('anthropic', 'm', ACCTS, host='h', now=now, path=path, **kw)


def test_record_contradiction_stores_only_ids_and_cooldowns(tmp_path):
    from hermes_cli.kanban_quota_detector import Detector
    path = tmp_path / 'd.db'
    _three(path)
    (report,) = Detector(path).pending()
    assert report['accounts'] == [{'id': 'acct-1', 'cooldown_until': 5000.0},
                                  {'id': 'acct-2', 'cooldown_until': None}]
    assert 'sk-secret' not in str(report) and '@' not in str(report)


def test_owner_drain_creates_one_card_then_comments_when_open(tmp_path, monkeypatch):
    from hermes_cli.kanban_quota_detector import Detector, drain_owner, record_contradiction
    _kb, conn = _board(tmp_path, monkeypatch)
    path = tmp_path / 'd.db'
    _three(path)
    det = Detector(path)
    assert drain_owner(conn, detector=det) == 1
    assert drain_owner(conn, detector=det) == 0
    rows = conn.execute('SELECT id, status FROM tasks').fetchall()
    assert len(rows) == 1
    # a later window while the card is open -> comment, no second card
    for now in (3000, 3001, 3002):
        record_contradiction('anthropic', 'm', ['acct-3'], host='h', now=now, path=path)
    assert drain_owner(conn, detector=det) == 1
    assert len(conn.execute('SELECT id FROM tasks').fetchall()) == 1
    assert len(_kb.list_comments(conn, rows[0][0])) == 1


def test_drain_after_card_done_creates_new_card(tmp_path, monkeypatch):
    from hermes_cli.kanban_quota_detector import Detector, drain_owner, record_contradiction
    _kb, conn = _board(tmp_path, monkeypatch)
    path = tmp_path / 'd.db'
    _three(path)
    det = Detector(path)
    drain_owner(conn, detector=det)
    conn.execute("UPDATE tasks SET status='done'")
    conn.commit()
    for now in (3000, 3001, 3002):
        record_contradiction('anthropic', 'm', [], host='h', now=now, path=path)
    drain_owner(conn, detector=det)
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 2


def test_managed_worker_cannot_drain(tmp_path, monkeypatch):
    from hermes_cli.kanban_quota_detector import Detector, drain_owner
    _kb, conn = _board(tmp_path, monkeypatch)
    path = tmp_path / 'd.db'
    _three(path)
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_x')
    assert drain_owner(conn, detector=Detector(path)) == 0
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    assert len(Detector(path).pending()) == 1


def test_drain_without_flock_skips_quietly_and_keeps_reports(tmp_path, monkeypatch, caplog):
    """Windows has no fcntl: the drain is an explicit no-op with one debug line, not a warning per tick."""
    import logging
    import sys
    from hermes_cli import kanban_quota_detector as detector
    _kb, conn = _board(tmp_path, monkeypatch)
    path = tmp_path / 'd.db'
    _three(path)
    monkeypatch.setitem(sys.modules, 'fcntl', None)  # `import fcntl` raises ImportError, as on Windows
    monkeypatch.setattr(detector, '_fcntl', None, raising=False)
    monkeypatch.setattr(detector, '_NO_FLOCK_LOGGED', False, raising=False)
    with caplog.at_level(logging.DEBUG, logger=detector.__name__):
        assert detector.drain_owner(conn, detector=detector.Detector(path)) == 0
        assert detector.drain_owner(conn, detector=detector.Detector(path)) == 0
    records = [r for r in caplog.records if r.name == detector.__name__]
    assert [r.levelno for r in records] == [logging.DEBUG]
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    assert len(detector.Detector(path).pending()) == 1


def test_drain_failsoft_and_concurrent_owners_single_card(tmp_path, monkeypatch):
    from hermes_cli.kanban_quota_detector import Detector, drain_owner
    _kb, conn = _board(tmp_path, monkeypatch)
    path = tmp_path / 'd.db'
    _three(path)
    def boom(c, r):
        raise RuntimeError('board down')
    assert drain_owner(conn, detector=Detector(path), writer=boom) == 0
    assert len(Detector(path).pending()) == 1

    def one(_):
        c = __import__("hermes_cli.kanban_db_connect", fromlist=["x"]).connect(tmp_path / 'kanban.db')
        try:
            return drain_owner(c, detector=Detector(path))
        finally:
            c.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, range(4)))
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1


def test_record_contradiction_failsoft(tmp_path):
    from hermes_cli.kanban_quota_detector import record_contradiction
    assert record_contradiction('p', 'm', [], path=str(tmp_path / 'no' / 'such' / 'x.db')) is None


def test_dispatch_tick_drains_via_owner_hook(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli.kanban_quota_detector import Detector
    _kb, conn = _board(tmp_path, monkeypatch)
    path = tmp_path / 'd.db'
    monkeypatch.setattr('hermes_cli.kanban_quota_detector.default_path', lambda: str(path))
    _three(path)
    kbd._dispatch_once_locked(conn, spawn_fn=lambda *a: None, max_spawn=0)
    assert Detector(path).pending() == []
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
