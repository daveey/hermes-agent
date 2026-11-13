"""Cross-account exhaustion transplant through _merge_pool_row_generation (fake tokens only).

Controlled reproduced shape, NOT a proven historical incident."""
import base64
import json
import time

from hermes_cli.auth import _merge_pool_row_generation


def _jwt(account_id, sub, email):
    def b(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'none'})}.{b({'sub': sub, 'email': email, 'https://api.openai.com/auth': {'chatgpt_account_id': account_id}})}.sig"


def _exhausted_row(access, refresh, now):
    return {"id": "r1", "access_token": access, "refresh_token": refresh,
            "last_status": "exhausted", "last_status_at": now,
            "last_error_code": 429, "last_error_reset_at": now + 3600}


def test_account_swap_does_not_inherit_old_account_exhaustion():
    now = time.time()
    a = _jwt("acct-A", "user-A", "same@example.com")  # same email, different account id
    b = _jwt("acct-B", "user-B", "same@example.com")
    stale = _exhausted_row(a, "rA", now)
    disk = {"id": "r1", "access_token": b, "refresh_token": "rB", "last_status": "ok",
            "last_status_at": now - 100}
    out = _merge_pool_row_generation(stale, disk, "openai-codex", base_pair=(a, "rA"))
    assert out["access_token"] == b
    assert out.get("last_status") != "exhausted"
    assert not out.get("last_error_reset_at")


def test_same_account_refresh_preserves_real_exhaustion():
    now = time.time()
    a1 = _jwt("acct-A", "user-A", "a@example.com")
    a2 = _jwt("acct-A", "user-A", "a@example.com") + "x"  # rotated token, same identity
    stale = _exhausted_row(a1, "r1", now)
    disk = {"id": "r1", "access_token": a2, "refresh_token": "r2", "last_status": "ok",
            "last_status_at": now - 100}
    out = _merge_pool_row_generation(stale, disk, "openai-codex", base_pair=(a1, "r1"))
    assert out["access_token"] == a2
    assert out["last_status"] == "exhausted"


def test_account_swap_adopts_all_disk_model_state():
    a, b = _jwt('A', 'uA', 'a@example.com'), _jwt('B', 'uB', 'b@example.com')
    stale = {**_exhausted_row(a, 'rA', time.time()), 'model_cooldowns': {'old': 1234}}
    disk = {'id': 'r1', 'access_token': b, 'refresh_token': 'rB', 'last_status': 'ok',
            'model_cooldowns': {'new': 5678}, 'model_cooldown_recorded_at': {'new': 100},
            'model_cooldown_cleared_at': {'cleared': 90}, 'cooldown_until': 42}
    out = _merge_pool_row_generation(stale, disk, 'openai-codex', base_pair=(a, 'rA'))
    for field in ('model_cooldowns', 'model_cooldown_recorded_at', 'model_cooldown_cleared_at', 'cooldown_until'):
        assert out[field] == disk[field]

