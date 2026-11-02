"""A nested scratch home must not enumerate the native install's profiles."""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import json

import pytest

import hermes_constants as constants


@pytest.fixture
def homes(tmp_path, monkeypatch):
    native = tmp_path / ".hermes"
    scratch = native / "cache" / "scratch" / "probe" / "home"
    for home in (native, native / "profiles" / "real", scratch):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(constants, "_get_platform_default_hermes_home", lambda: native)
    monkeypatch.setenv("HERMES_HOME", str(scratch))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    return native, scratch


def test_nested_scratch_is_its_own_root(homes):
    native, scratch = homes
    assert constants.get_default_hermes_root() == scratch
    assert constants.get_default_hermes_root(home=scratch) == scratch
    assert constants.get_default_hermes_root(home=native) == native
    assert constants.get_default_hermes_root(home=native / "profiles" / "real") == native
    assert constants.get_default_hermes_root(home=scratch / "profiles" / "test") == scratch


def test_scratch_profile_enumeration_never_reads_native(homes):
    from hermes_cli.profiles import get_profile_dir, profiles_to_serve

    _, scratch = homes
    assert get_profile_dir("default") == scratch
    assert profiles_to_serve(multiplex=True, include_standalone=True, include_parked=True) == [
        ("default", scratch)
    ]


def test_sidebar_profile_all_targets_only_scratch(homes):
    from hermes_cli.web_routers.profiles import _profile_targets

    _, scratch = homes
    assert _profile_targets("isolation regression") == [("default", scratch)]


@pytest.mark.parametrize("role", ["serve", "desktop-serve", "gateway"])
def test_probe_lock_override_ignores_real_host_record(homes, monkeypatch, role):
    from gateway import host_rendezvous as hr

    native, scratch = homes
    shared = native / "shared-locks"
    shared.mkdir()
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(shared))
    shared_record = hr.record_path(role)
    record = hr.HostRecord(
        role=role, pid=12345, create_time=None, host="127.0.0.1", port=9119,
        protocol_version=hr.HOST_PROTOCOL_VERSION, token_fingerprint="test",
        profiles=("default", "real"), updated_at="", home=str(native),
    )
    payload = json.dumps(record.to_json())
    shared_record.write_text(payload)
    assert hr.read_record(role, include_stale=True) == record
    locks = scratch / "gateway-locks"
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(locks))
    assert hr.host_state_dir() == locks
    assert hr.read_record(role, include_stale=True) is None
    assert shared_record.read_text() == payload


def test_native_root_symlink_remains_native(homes, tmp_path):
    native, _ = homes
    alias = tmp_path / "native-alias"
    alias.symlink_to(native, target_is_directory=True)
    assert constants.get_default_hermes_root(home=alias) == native
    profile_alias = tmp_path / "profile-alias"
    profile_alias.symlink_to(native / "profiles" / "real", target_is_directory=True)
    assert constants.get_default_hermes_root(home=profile_alias) == native
