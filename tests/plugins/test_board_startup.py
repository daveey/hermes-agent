"""Actual bundled mount activation and emergency rollback. [daveey]"""
import json
import time
import pytest

from hermes_cli import kanban_db
from plugins.kanban.dashboard import board_fact_views
from tests.plugins import test_kanban_board_shadow as helpers
from tests.waiting import wait_until

shadow_board = helpers.shadow_board


def test_native_startup_prewarms_then_serves_without_legacy(shadow_board, monkeypatch, caplog):
    caplog.set_level('DEBUG', logger='hermes_cli.fact_board_inputs')
    from hermes_cli.fact_board_startup import BoardRuntime
    api = shadow_board['mod']
    monkeypatch.setattr(time, 'time', lambda: 1_700_000_000.0)
    expected = helpers._read(shadow_board)
    runtime = BoardRuntime(api, boards=('default',))
    try:
        runtime.start()
        try:
            assert wait_until(lambda: runtime.ready('default'), timeout=10)
        except AssertionError:
            print(runtime.owners['default'].store.snapshot(runtime.owners['default'].scope))
            print([(key, fact.health, fact.error_code) for key, fact in runtime.owners['default'].store._facts.items()])
            raise
        def forbidden(*args, **kwargs):
            raise AssertionError('legacy/source read on warmed request')
        monkeypatch.setattr(api, '_conn', forbidden)
        monkeypatch.setattr(kanban_db, 'get_current_board', forbidden)
        monkeypatch.setattr(api, '_get_board_uncached', forbidden)
        assert api.get_board(board='default', **board_fact_views.board_selector()) == expected
        import asyncio
        response = asyncio.run(api.get_board_endpoint(board='default', **board_fact_views.board_selector()))
        assert json.loads(response.body) == expected
        with board_fact_views._request.pin('default', board_fact_views._installed):
            result = board_fact_views.response('board', board_fact_views.board_selector(), board='default')
        assert json.loads(result.body) == expected
    finally:
        runtime.close()


def test_current_selection_reads_published_catalog_only(shadow_board, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli.fact_board_startup import BoardRuntime
    from hermes_cli.fact_contract import Ready, Pending, Unavailable
    runtime = BoardRuntime(shadow_board['mod'], boards=('default',))
    selected = object()
    runtime.views = {'other': selected}
    monkeypatch.setattr(kanban_db, 'get_current_board', lambda: (_ for _ in ()).throw(AssertionError('source I/O')))
    assert isinstance(runtime.provider(None), Unavailable)
    result = Pending()
    snapshot = SimpleNamespace(view=lambda *_args: result)
    owner = SimpleNamespace(scope=None, handle=None)
    runtime.owners = {runtime.current: owner}
    assert isinstance(runtime.provider(None), Unavailable)
    owner.handle = SimpleNamespace(v1=SimpleNamespace(snapshot=lambda _scope: snapshot))
    assert isinstance(runtime.provider(None), Unavailable)
    result = Ready(b'{"current":"other"}', 'catalog', 1)
    assert runtime.provider(None) is selected
    result = Ready(b'{"current":"unknown"}', 'catalog', 2)
    assert isinstance(runtime.provider(None), Unavailable)


def test_board_provider_rejects_foreign_home_and_closed_owner(shadow_board, monkeypatch):
    from hermes_cli import config
    from hermes_cli.fact_board_startup import BoardRuntime
    from hermes_cli.fact_contract import Unavailable
    from pathlib import Path
    runtime = BoardRuntime(shadow_board['mod'], boards=('default',))
    selected = object()
    runtime.views = {'default': selected}
    assert runtime.provider('default') is selected
    monkeypatch.setattr(config, 'get_hermes_home', lambda: Path(runtime.home) / 'foreign')
    assert isinstance(runtime.provider('default'), Unavailable)
    monkeypatch.setattr(config, 'get_hermes_home', lambda: Path(runtime.home))
    assert isinstance(runtime.provider('unknown'), Unavailable)
    runtime._closed = True
    assert isinstance(runtime.provider('default'), Unavailable)


def test_actual_mount_entry_selects_native_runtime(shadow_board, monkeypatch):
    from hermes_cli import fact_board_startup as board_startup
    from hermes_cli.fact_plugin_mount import PluginLifecycles
    from pathlib import Path
    api = shadow_board['mod']
    calls = []
    class Handle:
        def close(self):
            calls.append('close')
    monkeypatch.delenv('HERMES_FACTS_BOARD', raising=False)
    monkeypatch.setattr(board_startup, 'start', lambda module: calls.append(module) or Handle())
    registry = PluginLifecycles()
    registry.start(Path(api.__file__), api)
    assert calls == [api]
    registry.close()
    assert calls == [api, 'close']


def test_same_process_off_rollback_restores_legacy_admission(shadow_board, monkeypatch):
    from hermes_cli import fact_board_startup as board_startup
    from plugins.kanban.dashboard import board_legacy
    api = shadow_board['mod']
    path = str(kanban_db.kanban_db_path())
    runtime = board_startup.BoardRuntime(api, boards=('default',)).start()
    runtime.close()
    monkeypatch.setenv('HERMES_FACTS_BOARD', 'off')
    handle = board_startup.start(api)
    try:
        with board_legacy.borrowed(path):
            assert shadow_board['live_id'] in helpers._cards(helpers._read(shadow_board))
    finally:
        handle.close()


@pytest.mark.parametrize('flag,platform', [('off', 'linux'), ('1', 'darwin'), ('1', 'win32')])
def test_flag_off_starts_legacy_once_and_never_captures(shadow_board, monkeypatch, flag, platform):
    from hermes_cli import fact_board_startup as board_startup
    from types import SimpleNamespace
    api = shadow_board['mod']
    calls = []
    monkeypatch.setenv('HERMES_FACTS_BOARD', flag)
    monkeypatch.setattr(board_startup, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(api.board_prewarm, 'LegacyLifecycle', lambda api: calls.append('legacy') or type(
        'Handle', (), {'close': lambda self: calls.append('close')})())
    monkeypatch.setattr(board_startup, 'BoardRuntime', lambda *a, **k: (_ for _ in ()).throw(AssertionError('facts start')))
    handle = board_startup.start(api)
    assert calls == ['legacy']
    handle.close()
    assert calls == ['legacy', 'close']
