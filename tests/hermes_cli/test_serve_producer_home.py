"""Server-side home binding for the shared panel producer (canonical home + life + authorization)."""
import os
from types import SimpleNamespace

import pytest

from hermes_cli import serve_producer_home as home_mod
from hermes_cli.serve_producer_home import HomeBinding, HomeRevoked


class Authority:
    def __init__(self, home, epoch=1, instance_id='inst-a'):
        self.profile_id = str(home)
        self.instance_id = instance_id
        self.epoch = epoch
        self.retiring = False
        self.retired = False
        self.db = SimpleNamespace(db_path=str(home / 'state.db'))


def runner_for(launch, *authorities):
    from gateway.session_authorities import SessionAuthorities

    registry = SessionAuthorities(launch)
    registry.add(launch, None)
    for authority in authorities:
        registry.add(authority.profile_id, authority)
    return SimpleNamespace(session_authorities=registry)


@pytest.fixture
def root(tmp_path, monkeypatch):
    base = tmp_path / '.hermes'
    base.mkdir()
    (base / 'profiles').mkdir()
    monkeypatch.setenv('HERMES_HOME', str(base))
    return base


def named(root, name):
    path = root / 'profiles' / name
    path.mkdir()
    return path


def test_alias_shares_binding_and_equal_labels_do_not_collide(root, tmp_path):
    home = named(root, 'researcher')
    alias = tmp_path / 'alias'
    alias.symlink_to(home)
    other_root = tmp_path / 'other' / '.hermes'
    (other_root / 'profiles').mkdir(parents=True)
    twin = other_root / 'profiles' / 'researcher'
    twin.mkdir()
    ctx = SimpleNamespace(session_authority=Authority(home))
    assert home_mod.bind(alias, ctx) == home_mod.bind(home, ctx)
    assert home_mod.bind(home, ctx).home == str(home.resolve())
    twin_ctx = SimpleNamespace(session_authority=Authority(twin))
    assert home_mod.bind(twin, twin_ctx).identity != home_mod.bind(home, ctx).identity
    assert home_mod.bind(twin, twin_ctx).home != home_mod.bind(home, ctx).home


def test_recreated_home_is_a_new_lifecycle_generation(root):
    home = named(root, 'researcher')
    ctx = SimpleNamespace(session_authority=Authority(home))
    before = home_mod.bind(home, ctx)
    replacement = root / 'profiles' / 'replacement'
    replacement.mkdir()  # allocated while the old directory still exists: a distinct inode
    home.rename(root / 'profiles' / 'retired')
    replacement.rename(home)
    after = home_mod.bind(home, ctx)
    assert after.lifecycle_generation != before.lifecycle_generation
    assert after.authorization_generation == before.authorization_generation
    assert not home_mod.is_current(before, ctx)
    assert home_mod.is_current(after, ctx)


def test_tombstone_and_missing_homes_are_revoked(root, tmp_path):
    from hermes_constants import mark_named_profile_deleted

    home = named(root, 'researcher')
    ctx = SimpleNamespace(session_authority=Authority(home))
    binding = home_mod.bind(home, ctx)
    mark_named_profile_deleted(home)
    with pytest.raises(HomeRevoked, match='tombstoned'):
        home_mod.bind(home, ctx)
    assert not home_mod.is_current(binding, ctx)
    with pytest.raises(HomeRevoked, match='does not exist'):
        home_mod.bind(tmp_path / 'missing', ctx)
    plain_file = tmp_path / 'file'
    plain_file.write_text('x')
    with pytest.raises(HomeRevoked, match='not a directory'):
        home_mod.bind(plain_file, ctx)


def test_standalone_serve_authorizes_only_its_launch_home(root, tmp_path):
    first = home_mod.bind(root)
    assert first.home == str(root.resolve())
    assert home_mod.bind(root) == first
    assert home_mod.is_current(first)
    other = named(root, 'researcher')
    with pytest.raises(HomeRevoked, match='another home'):
        home_mod.bind(other)
    assert not home_mod.is_current(HomeBinding(str(other.resolve()), first.lifecycle_generation, 'x'))


def test_request_home_override_does_not_authorize_a_standalone_home(root):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    other = named(root, 'researcher')
    token = set_hermes_home_override(other)
    try:
        with pytest.raises(HomeRevoked, match='another home'):
            home_mod.bind(other)
        assert home_mod.bind(root).home == str(root.resolve())
    finally:
        reset_hermes_home_override(token)


def test_standalone_generation_is_this_process_life(root, monkeypatch):
    first = home_mod.bind(root)
    real = os.getpid()
    monkeypatch.setattr(os, 'getpid', lambda: real + 1)
    assert home_mod.bind(root).authorization_generation != first.authorization_generation


def test_runner_authority_epoch_retirement_and_unserve_revoke(root):
    launch = root
    secondary = named(root, 'researcher')
    auth = Authority(secondary)
    runner = runner_for(launch, auth)
    state = SimpleNamespace(gateway_runner=runner)
    first = home_mod.bind(secondary, state)
    assert home_mod.is_current(first, state)
    auth.epoch += 1  # a successor authority generation
    assert not home_mod.is_current(first, state)
    second = home_mod.bind(secondary, state)
    assert second.authorization_generation != first.authorization_generation
    auth.retiring = True
    with pytest.raises(HomeRevoked, match='active authority'):
        home_mod.bind(secondary, state)
    auth.retiring = False
    auth.retired = True
    with pytest.raises(HomeRevoked, match='active authority'):
        home_mod.bind(secondary, state)
    auth.retired = False
    runner.session_authorities.remove(secondary)  # hot-unserved
    with pytest.raises(HomeRevoked, match='active authority'):
        home_mod.bind(secondary, state)
    assert not home_mod.is_current(second, state)


def test_instance_change_is_a_new_authorization_generation(root):
    secondary = named(root, 'researcher')
    auth = Authority(secondary)
    state = SimpleNamespace(gateway_runner=runner_for(root, auth))
    first = home_mod.bind(secondary, state)
    auth.instance_id = 'inst-b'
    assert home_mod.bind(secondary, state).authorization_generation != first.authorization_generation


def test_single_authority_state_must_own_the_home(root):
    home = named(root, 'researcher')
    other = named(root, 'other')
    state = SimpleNamespace(session_authority=Authority(home))
    assert home_mod.bind(home, state).home == str(home.resolve())
    with pytest.raises(HomeRevoked):
        home_mod.bind(other, state)  # not the authority's home, and not an authorized standalone launch
    broken = SimpleNamespace(session_authority=SimpleNamespace(db=None))
    with pytest.raises(HomeRevoked, match='no home'):
        home_mod.bind(home, broken)


def test_runner_without_authority_for_home_is_revoked(root):
    secondary = named(root, 'researcher')
    state = SimpleNamespace(gateway_runner=runner_for(root))
    with pytest.raises(HomeRevoked, match='active authority'):
        home_mod.bind(secondary, state)


def test_from_wire_is_strict(root):
    binding = home_mod.bind(root)
    assert HomeBinding.from_wire(binding.wire()) == binding
    good = binding.wire()
    bad = [None, [], {}, dict(good, extra='x'), {k: v for k, v in good.items() if k != 'home'},
           dict(good, home=''), dict(good, home='relative/path'), dict(good, home=3),
           dict(good, lifecycle_generation=''), dict(good, authorization_generation=None),
           dict(good, home='/' + 'x' * 5000)]
    for value in bad:
        with pytest.raises(ValueError, match='invalid home partition'):
            HomeBinding.from_wire(value)
