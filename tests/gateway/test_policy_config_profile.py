"""Followed profile selection compares hydrated frozen policies."""
import pytest

from tests.gateway import test_bot_chat_follow_entry as helpers
from tests.gateway import test_bot_chat_managed_follow as managed
from hermes_state_local import local_receipt
from hermes_state_policy_config import store_receipt

home = helpers.home
_state_db_in_profile = managed._state_db_in_profile


@pytest.mark.asyncio
async def test_interned_profile_selection_compares_exact_frozen_policy(home, monkeypatch):
    from gateway.session_profile_selection import commit_selection
    store, db, authority, owner, ref, _built = helpers._bot_chat_owner(home, monkeypatch)
    try:
        await helpers._open(authority, ref)
        original = local_receipt(db, ref.session_id)
        db._execute_write(lambda conn: store_receipt(conn, ref.session_id, original, intern=True))
        policy, config, model, ended = helpers._selection_inputs(authority, ref)
        commit_selection(authority, ref, policy, config, model,
                         db.get_session(ref.session_id)['runtime_generation'], ended=ended)
        assert local_receipt(db, ref.session_id)['policy'] == original['policy']
    finally:
        await owner.close()
        store.close_all_db_handles()
