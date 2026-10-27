"""The TUI gateway starts the account quota recovery watch at startup."""


def test_tui_gateway_starter_starts_the_watch(monkeypatch):
    import agent.account_usage_recovery as recovery
    from tui_gateway import entry

    started = []
    monkeypatch.setattr(recovery, "start_quota_recovery_watch", lambda: started.append(True))
    entry._start_quota_recovery_watch()
    assert started == [True]
