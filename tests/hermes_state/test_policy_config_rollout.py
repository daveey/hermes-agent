"""Disabled-by-default producer switch and transaction-owned representation rollout."""
from contextlib import closing
from dataclasses import asdict
import json

import pytest

from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX, commit_local_session, local_receipt
from hermes_state_policy_config import BLOB_PREFIX, REF_PREFIX
import hermes_state_runtime as rt


def create(db, epoch, cwd, flag, request_id='first'):
    from gateway.config import Platform
    from gateway.session import SessionEntry, SessionSource
    from gateway.session_lifecycle import _now
    from gateway.session_local_recovery import local_identity
    from gateway.session_policy import build_policy
    sid = local_identity('profile', 'human', request_id)
    source = SessionSource(platform=Platform.LOCAL, chat_id=sid, user_id='human', chat_type='dm')
    now = _now()
    entry = SessionEntry('local:' + sid, sid, now, now, origin=source, platform=Platform.LOCAL)
    config = {'platform_toolsets': {'cli': []}, 'sessions': {'intern_policy_configs': flag}}
    policy = build_policy({'source': 'cli', 'cwd': str(cwd), 'model': 'm', 'toolsets': []},
                          config, private_secrets={})
    expected = dict(profile_id='profile', principal_id='human', request_id=request_id, session_id=sid,
                    route=entry.session_key, entry=entry.to_dict(), policy=asdict(policy))
    expected = json.loads(json.dumps(expected))  # persisted JSON normalizes policy tuples
    assert commit_local_session(db, epoch=epoch, receipt=expected) == expected
    return sid, expected


@pytest.mark.parametrize('flag,interned', [(True, True), (False, False), ('true', False), (1, False), (None, False)])
def test_creation_switch_is_exact_opt_in_and_retry_preserves_values(tmp_path, flag, interned):
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        sid, expected = create(db, epoch, tmp_path, flag)
        with db._read_ctx() as conn:
            raw = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + sid,)).fetchone()[0])
            assert ('config_ref_v1' in raw['policy']) is interned
            assert ('config_json' in raw['policy']) is not interned
            blobs = conn.execute('SELECT count(*) FROM state_meta WHERE key GLOB ?', (BLOB_PREFIX + '*',)).fetchone()[0]
            assert blobs == int(interned)
        assert local_receipt(db, sid) == expected
        assert commit_local_session(db, epoch=epoch, receipt=expected) == expected


def test_bounded_conversion_resumes_and_rolls_back_exact_values(tmp_path):
    from hermes_state_policy_rollout import convert_policy_config_batch
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        expected = dict(create(db, epoch, tmp_path, False, str(i)) for i in range(3))
        first = convert_policy_config_batch(db, intern=True, apply=True, readers_compatible=True, limit=1)
        assert first['converted'] == 1 and first['scanned'] == 1 and first['more']
        second = convert_policy_config_batch(db, intern=True, apply=True, readers_compatible=True,
                                             after=first['after'], limit=2)
        assert second['converted'] == 2 and not second['more']
        for sid, receipt in expected.items():
            assert local_receipt(db, sid) == receipt
        with db._read_ctx() as conn:
            assert conn.execute('SELECT count(*) FROM state_meta WHERE key GLOB ?', (BLOB_PREFIX + '*',)).fetchone()[0] == 1
        back = convert_policy_config_batch(db, intern=False, apply=True)
        assert back['converted'] == 3
        with db._read_ctx() as conn:
            assert conn.execute('SELECT key FROM state_meta WHERE key GLOB ? OR key GLOB ?',
                                (BLOB_PREFIX + '*', REF_PREFIX + '*')).fetchall() == []
        for sid, receipt in expected.items():
            assert local_receipt(db, sid) == receipt
