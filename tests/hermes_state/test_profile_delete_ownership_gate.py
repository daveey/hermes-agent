"""Deletion's public ownership gate must include profile resource custody."""
from contextlib import contextmanager

import pytest

from hermes_cli.profiles_remove import wait_profile_unowned
from hermes_constants import mark_named_profile_deleted


@pytest.mark.parametrize('deleted,drained,expected', [(False, False, True), (True, False, False), (True, True, True)])
def test_delete_checks_resources_before_gateway_lock(tmp_path, monkeypatch, deleted, drained, expected):
    home = tmp_path / '.hermes' / 'profiles' / 'retiring'
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('{}')
    if deleted:
        mark_named_profile_deleted(home)
    calls = []

    def resources(path, *, timeout):
        assert path == home
        assert timeout >= 0
        calls.append('resources')
        return drained

    @contextmanager
    def maintenance(homes):
        assert homes == [home]
        calls.append('gateway')
        yield

    monkeypatch.setattr('hermes_profile_resources.wait_profile_resources', resources)
    monkeypatch.setattr('gateway.runtime_ownership.exclusive_maintenance', maintenance)
    assert wait_profile_unowned(home, timeout=1) is expected
    assert calls == (['gateway'] if not deleted else ['resources', 'gateway'] if drained else ['resources'])
