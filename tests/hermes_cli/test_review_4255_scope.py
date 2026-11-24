from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_session_activity as activity


def test_explicit_replica_does_not_read_ambient_config(monkeypatch):
    def forbidden():
        raise AssertionError('ambient sync_config consulted')
    monkeypatch.setattr(sync, 'sync_config', forbidden)
    assert links.replica_cards(['chat'], 'explicit-gw', replicas=[], profile='explicit-profile', origin_cards=[], ledger_fallback=False) == []


def test_explicit_empty_origin_cards_do_not_read_ambient_ledger(monkeypatch):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('ambient remote ledger consulted')
    monkeypatch.setattr(links, '_stale_after_s', lambda: 300)
    monkeypatch.setattr(activity, '_remote_links', forbidden)
    links._reset_for_tests()
    assert links.replica_cards(['chat'], 'explicit-gw', replicas=[], profile='explicit-profile', origin_cards=[], ledger_fallback=False) == []
    assert calls == []
