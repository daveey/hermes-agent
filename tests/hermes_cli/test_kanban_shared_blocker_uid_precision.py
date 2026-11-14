"""Only consistent, strictly earlier UUIDv7 precision can fence a remote claim."""
import uuid
from pathlib import Path

import pytest

from hermes_cli import kanban_shared_blocker_checks as checks
from hermes_cli.kanban_shared_blocker_fence import _uid_before_check


def _claim_uid(milliseconds, *, version=7, suffix=0):
    return str(uuid.UUID(int=(milliseconds << 80) | (version << 76) | (2 << 62) | suffix))


@pytest.mark.parametrize('identity', ['not-a-uuid', '', 17])
def test_invalid_claim_uuid_retains_fallback(identity):
    assert _uid_before_check(identity, 1, {'checked_at_ms': 1200}) is False


def test_non_v7_uuid_retains_fallback_even_with_earlier_timestamp_bits():
    identity = _claim_uid(1100, version=4)
    assert uuid.UUID(identity).version == 4
    assert _uid_before_check(identity, 1, {'checked_at_ms': 1200}) is False


@pytest.mark.parametrize('payload', [
    pytest.param({}, id='missing'),
    pytest.param({'checked_at_ms': None}, id='null'),
    pytest.param({'checked_at_ms': '200'}, id='numeric-string'),
    pytest.param({'checked_at_ms': 200.0}, id='float'),
    pytest.param({'checked_at_ms': []}, id='list'),
    pytest.param({'checked_at_ms': {}}, id='dict'),
    pytest.param({'checked_at_ms': True}, id='true-is-not-an-integer-clock'),
    pytest.param({'checked_at_ms': False}, id='false-is-not-an-integer-clock'),
])
def test_missing_or_invalid_check_precision_retains_fallback(payload):
    # Timestamp zero makes True satisfy the adjacent second/order predicates:
    # changing the exact int check to isinstance(..., int) must fail this test.
    assert _uid_before_check(_claim_uid(0), 0, payload) is False


@pytest.mark.parametrize(('claimed_ms', 'started', 'checked_ms'), [
    pytest.param(1100, 2, 2200, id='claim-second-disagrees'),
    pytest.param(1100, 1, 2200, id='check-second-disagrees'),
    pytest.param(1100, 2, 1200, id='started-second-disagrees-with-both'),
])
def test_inconsistent_seconds_retain_fallback(claimed_ms, started, checked_ms):
    assert _uid_before_check(_claim_uid(claimed_ms), started,
                             {'checked_at_ms': checked_ms}) is False


@pytest.mark.parametrize('suffix', [0, (1 << 62) - 1])
@pytest.mark.parametrize(('claimed_ms', 'expected'), [
    pytest.param(1199, True, id='strictly-earlier'),
    pytest.param(1200, False, id='equal-millisecond'),
    pytest.param(1201, False, id='later-claim'),
])
def test_same_second_requires_strictly_earlier_milliseconds(claimed_ms, expected, suffix):
    assert _uid_before_check(_claim_uid(claimed_ms, suffix=suffix), 1,
                             {'checked_at_ms': 1200}) is expected


@pytest.mark.platforms('posix')
def test_default_registered_check_runs_nonexecutable_stub_in_profile_home_with_spaces(
        tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    profile_home = tmp_path / 'hermes home' / 'profiles' / 'quota profile'
    monkeypatch.setenv('HERMES_HOME', str(profile_home))
    script = profile_home / 'bin' / 'github_quota.py'
    script.parent.mkdir(parents=True)
    script.write_text(
        'import sys\n'
        'assert sys.argv[1:] == ["--reserve-ok"], sys.argv\n'
        'print("profile quota stub ran")\n', encoding='utf-8')
    script.chmod(0o600)
    assert script.stat().st_mode & 0o111 == 0
    token = set_hermes_home_override(profile_home)
    try:
        command = checks.registered_checks({})['github-rest-quota']
        assert checks._run_check(command) == (0, 'profile quota stub ran')
    finally:
        reset_hermes_home_override(token)
