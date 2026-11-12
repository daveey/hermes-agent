"""A hub re-mint revokes every relay-minted token at once: re-pull, don't give up (t_01a1d064a8dc8bd6).

worker-4, run 10089 (2026-11-12): the hub refreshed its Anthropic grants (revoking each previous
access token), a turn's request landed before the worker's pull, and the 401 ``OAuth access
token has been revoked`` cascaded through every ``manual:hub_minted`` row in under a second.
The turn failed with ``auth`` and the Kanban worker exited 78 (terminal_provider -> gave_up).

These tests drive the real pool recovery (``recover_with_credential_pool``) against a fake
Anthropic that accepts only the tokens the relay has pushed, and a fake ``worker-oauth-pull``
that writes the re-minted rows to auth.json.
"""

import json
import shlex
import sys
import time

import pytest

import agent.credential_pool as cp
from agent.agent_runtime_helpers import recover_with_credential_pool
from hermes_cli.turn_exit import turn_exit_code

LABELS = ("a@example.com", "b@example.com", "c@example.com")
REVOKED = {"message": "OAuth access token has been revoked.", "reason": "authentication_error"}

_PULL = r'''
import json, sys
auth, counter, ready_after = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    n = int(open(counter).read())
except OSError:
    n = 0
open(counter, "w").write(str(n + 1))
if n + 1 < ready_after:
    sys.exit(0)  # the hub's push has not landed in SSM yet
doc = json.load(open(auth))
for row in doc["credential_pool"]["anthropic"]:
    row["access_token"] = "new-" + row["label"]
    for k in ("last_status", "last_status_at", "last_error_code", "last_error_reason", "last_error_message"):
        row.pop(k, None)
json.dump(doc, open(auth, "w"))
'''


def _row(label, idx, now):
    return dict(id=f"r{idx}", label=label, auth_type="oauth", priority=idx, source="manual:hub_minted",
                access_token="old-" + label, expires_at_ms=int((now + 7200) * 1000))


class _Agent:
    provider = "anthropic"
    base_url = "https://api.anthropic.com"
    model = "claude-opus-5-5"
    api_mode = "anthropic_messages"
    log_prefix = ""
    _interrupt_requested = False

    def __init__(self, pool):
        self._credential_pool = pool
        entry = pool.select()
        self.api_key = entry.runtime_api_key
        self._credential_pool_entry_id = entry.id

    def _swap_credential(self, entry):
        self.api_key = entry.runtime_api_key
        self._credential_pool_entry_id = entry.id
        return True

    def _is_entitlement_failure(self, *_a):
        return False

    def _vprint(self, *_a, **_k):
        pass

    def _touch_activity(self, *_a):
        pass


def _turn(agent, accepted):
    """One turn's request loop: the result dict the conversation loop would hand back."""
    for _ in range(20):
        if agent.api_key in accepted:
            return {"completed": True, "final_response": "ok"}
        recovered, _ = recover_with_credential_pool(
            agent, status_code=401, has_retried_429=False, error_context=dict(REVOKED))
        if not recovered:
            return {"failed": True, "completed": False, "failure_reason": "auth",
                    "error": "HTTP 401: OAuth access token has been revoked."}
    raise AssertionError("request loop did not settle")


@pytest.fixture
def relay(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    now = time.time()
    auth = home / "auth.json"
    auth.write_text(json.dumps({"version": 1, "credential_pool": {
        "anthropic": [_row(label, i, now) for i, label in enumerate(LABELS)]}}))
    script = tmp_path / "fake_pull.py"
    script.write_text(_PULL)
    counter = tmp_path / "pulls"
    settings = {}
    try:  # absent before the fix: the exit code, not an ImportError, is what fails there
        from agent import credential_relay_repull as rr
        monkeypatch.setattr(rr, "_PULL_EVERY_S", 0.05)
        monkeypatch.setattr(rr, "_POLL_S", 0.02)
        monkeypatch.setattr(rr, "_settings", lambda: settings)
    except ImportError:
        pass

    def configure(ready_after=1, wait_s=5.0):
        settings.update(pull_command=shlex.join([sys.executable, str(script), str(auth), str(counter), str(ready_after)]),
                        repull_wait_s=wait_s)
        pool = cp.load_pool("anthropic")
        assert {e.source for e in pool.entries()} == {"manual:hub_minted"}
        return pool

    def pulls():
        return int(counter.read_text()) if counter.exists() else 0

    return configure, pulls


def test_revoked_hub_minted_401_repulls_and_the_turn_succeeds(relay):
    configure, pulls = relay
    pool = configure(ready_after=3)  # the push lands two pulls after the 401
    agent = _Agent(pool)
    result = _turn(agent, accepted={"new-" + label for label in LABELS})

    assert turn_exit_code(result, kanban_worker=True) == 0, result
    assert agent.api_key.startswith("new-")
    assert pulls() >= 3
    # The same push re-minted the siblings: they are adopted and healthy, not benched.
    entries = pool.entries()
    assert all(e.access_token.startswith("new-") for e in entries)
    assert all(e.last_status in (None, "ok") for e in entries), [e.last_status for e in entries]


def test_a_re_pulled_token_refused_again_is_still_an_auth_failure(relay):
    configure, pulls = relay
    pool = configure(ready_after=1, wait_s=0.3)
    agent = _Agent(pool)
    started = time.monotonic()
    result = _turn(agent, accepted=set())  # the account is genuinely locked out

    assert turn_exit_code(result, kanban_worker=True) == 78
    assert time.monotonic() - started < 10, "the re-pull wait must stay bounded"
    assert pulls() >= 1


def test_only_revoked_401s_on_relay_rows_take_the_re_pull_path():
    from agent.credential_relay_repull import is_relay_managed, is_revoked_401

    minted = cp.PooledCredential.from_dict("anthropic", _row("a@example.com", 0, time.time()))
    own_grant = cp.PooledCredential.from_dict(
        "anthropic", {**_row("a@example.com", 0, time.time()), "source": "manual", "refresh_token": "rt"})
    assert is_relay_managed(minted)
    assert not is_relay_managed(own_grant)
    assert is_revoked_401(401, REVOKED)
    assert not is_revoked_401(401, {"message": "invalid x-api-key"})
    assert not is_revoked_401(403, REVOKED)
