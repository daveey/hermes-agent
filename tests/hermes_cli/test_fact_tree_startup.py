"""Native tree startup, direct/HTTP projections and source mutation. [daveey]"""
import asyncio
import json
import pytest

from hermes_cli import projects_fact_routes
from hermes_cli.web_routers import profiles
from tests.hermes_cli import test_profiles_sidebar_scope as helpers
from tests.waiting import wait_until

profiles_on_disk = helpers.profiles_on_disk


def test_tree_startup_parity_and_native_mutation(profiles_on_disk, monkeypatch):
    from hermes_cli.fact_tree_startup import TreeRuntime
    from hermes_cli.fact_contract import Ready
    import time
    monkeypatch.setattr(time, 'time', lambda: 1_700_000_000.0)
    for name, home in profiles_on_disk.items():
        helpers._seed_session(home, name + '-chat', source='cli', cwd='/fixture/repo')
    monkeypatch.setattr(profiles, '_SIDEBAR_CACHE_TTL_SECONDS', 0)
    expected = profiles.get_profiles_projects_tree()
    runtime = TreeRuntime(targets=tuple(profiles_on_disk.items()))
    selector = {'surface': 'profiles', 'preview_limit': 3, 'session_limit': 2000, 'profile': None}
    try:
        runtime.start()
        bound = runtime.provider(None)
        assert wait_until(lambda: isinstance(bound.read('tree', selector), Ready), timeout=10)
        actual = profiles.get_profiles_projects_tree()
        import difflib
        diff = [line for line in difflib.ndiff(json.dumps(expected, sort_keys=True, indent=2).splitlines(),
                                             json.dumps(actual, sort_keys=True, indent=2).splitlines())
                if line.startswith(('- ', '+ '))]
        assert actual == expected, diff
        assert json.loads(asyncio.run(profiles.get_profiles_projects_tree.http_endpoint()).body) == expected
        helpers._seed_session(profiles_on_disk['worker'], 'new-chat', source='cli', cwd='/fixture/repo')
        assert wait_until(lambda: 'new-chat' in str(bound.read('tree', selector)), timeout=10)
        def forbidden(*args, **kwargs):
            raise AssertionError('legacy tree source called from warmed request')
        monkeypatch.setattr(profiles, 'profile_project_trees', forbidden)
        assert 'new-chat' in str(profiles.get_profiles_projects_tree())
    finally:
        runtime.close()


def test_real_app_lifespan_enters_tree_owner(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from hermes_cli import web_server_app, fact_tree_startup, fact_plugin_mount, fact_links_startup
    from hermes_cli import sessions_native_startup
    from gateway import host_attach
    calls = []
    @asynccontextmanager
    async def tree(app):
        calls.append('tree-start')
        try:
            yield
        finally:
            calls.append('tree-close')
    @asynccontextmanager
    async def quiet(app):
        yield
    monkeypatch.setattr(fact_tree_startup, 'lifespan', tree)
    monkeypatch.setattr(fact_plugin_mount, 'lifespan', quiet)
    monkeypatch.setattr(fact_links_startup, 'lifespan', quiet)
    monkeypatch.setattr(sessions_native_startup, 'lifespan', quiet)
    monkeypatch.setattr(web_server_app, 'standalone_lifespan', quiet)
    monkeypatch.setattr(host_attach, 'enable_stale_while_revalidate', lambda *a: None)
    async def exercise():
        async with web_server_app.app_lifespan(SimpleNamespace(state=SimpleNamespace())):
            assert calls == ['tree-start']
    asyncio.run(exercise())
    assert calls == ['tree-start', 'tree-close']


def test_tree_foreign_profile_never_falls_back(profiles_on_disk):
    from hermes_cli.fact_tree_startup import TreeRuntime
    from hermes_cli.fact_contract import Unavailable
    runtime = TreeRuntime(targets=tuple(profiles_on_disk.items()))
    assert isinstance(runtime.provider('foreign'), Unavailable)
    runtime.close()


def test_tree_provider_binds_authenticated_home_and_rejects_escape(profiles_on_disk, monkeypatch):
    from hermes_cli import config
    from hermes_cli.fact_tree_startup import TreeRuntime, TreeBinding
    from hermes_cli.fact_contract import Unavailable
    runtime = TreeRuntime(targets=tuple(profiles_on_disk.items()))
    try:
        assert isinstance(runtime.provider(None), TreeBinding)
        assert runtime.provider(None).profile is None
        monkeypatch.setattr(config, 'get_hermes_home', lambda: profiles_on_disk['worker'])
        assert runtime.provider(None).profile == 'worker'
        assert runtime.provider('worker').profile == 'worker'
        assert isinstance(runtime.provider('default'), Unavailable)
        monkeypatch.setattr(config, 'get_hermes_home', lambda: profiles_on_disk['worker'] / 'foreign')
        assert isinstance(runtime.provider('worker'), Unavailable)
        monkeypatch.setattr(config, 'get_hermes_home', lambda: profiles_on_disk['default'])
        assert isinstance(runtime.provider('unknown'), Unavailable)
        runtime.closed = True
        assert isinstance(runtime.provider(None), Unavailable)
    finally:
        runtime.close()


@pytest.mark.parametrize('flag,platform', [('off', 'linux'), ('1', 'darwin'), ('1', 'win32')])
def test_rollback_and_unsupported_native_backend_skip_tree(monkeypatch, flag, platform):
    from types import SimpleNamespace
    from hermes_cli import fact_tree_startup
    monkeypatch.setenv('HERMES_FACTS_BOARD', flag)
    monkeypatch.setattr(fact_tree_startup, 'sys', SimpleNamespace(platform=platform))
    def forbidden():
        raise AssertionError('native capture on rollback/unsupported platform')
    monkeypatch.setattr(fact_tree_startup, 'TreeRuntime', forbidden)
    async def exercise():
        async with fact_tree_startup.lifespan(SimpleNamespace()):
            pass
    asyncio.run(exercise())
