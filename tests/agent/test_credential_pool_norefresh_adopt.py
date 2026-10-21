"""A live pool adopts tokens pushed to disk for OAuth rows that carry no refresh_token.

A hub-side minter writes ``{access_token, expires_at_ms}`` rows (no refresh_token). Before the fix a
live CredentialPool kept the expired / 401-benched copy forever; only a fresh load_pool() saw the push.
"""

import base64
import json
import time

import pytest

import agent.credential_pool as cp
import hermes_cli.auth as auth_mod


def _now_ms():
    return int(time.time() * 1000)


def _jwt(exp_s):
    def b(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b({'alg': 'none'})}.{b({'exp': int(exp_s), 'iat': int(time.time())})}.sig"


def _row(label, token, exp_ms, eid):
    return {
        "id": eid, "label": label, "auth_type": "oauth", "priority": 0,
        "source": "manual:hub_minted", "access_token": token, "expires_at_ms": exp_ms,
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("agent.anthropic_credentials.read_claude_code_credentials", lambda: None)

    def boom(*a, **k):
        raise AssertionError("network refresh attempted")

    monkeypatch.setattr(cp, "refresh_anthropic_oauth_pure", boom, raising=False)
    for n in dir(auth_mod):
        if n.startswith(("refresh_codex", "_refresh_codex")) or n == "_refresh_expired_codex_probe_token":
            monkeypatch.setattr(auth_mod, n, boom)
    return h


def _write(home, provider, rows):
    path = home / "auth.json"
    d = json.loads(path.read_text()) if path.exists() else {"version": 1}
    d.setdefault("credential_pool", {})[provider] = rows
    path.write_text(json.dumps(d))


def test_valid_norefresh_row_is_selected(home):
    _write(home, "anthropic", [_row("a", "sk-ant-oat01-AAA", _now_ms() + 3_600_000, "aaaaaa")])
    sel = cp.load_pool("anthropic").select()
    assert sel is not None and sel.access_token == "sk-ant-oat01-AAA"


def test_expiring_norefresh_row_is_skipped_not_exhausted(home):
    _write(home, "anthropic", [_row("a", "sk-ant-oat01-AAA", _now_ms() + 30_000, "aaaaaa")])
    pool = cp.load_pool("anthropic")
    assert pool.select() is None
    assert [e.last_status for e in pool._entries] != ["exhausted"]


def test_live_pool_adopts_pushed_token_when_row_expiring(home):
    _write(home, "anthropic", [_row("a", "sk-ant-oat01-AAA", _now_ms() + 30_000, "aaaaaa")])
    pool = cp.load_pool("anthropic")
    assert pool.select() is None
    _write(home, "anthropic", [_row("a", "sk-ant-oat01-BBB", _now_ms() + 3_600_000, "aaaaaa")])
    sel = pool.select()
    assert sel is not None and sel.access_token == "sk-ant-oat01-BBB"


def test_401_rotates_then_live_pool_adopts_pushed_token_for_benched_row(home):
    _write(home, "anthropic", [
        _row("a", "sk-ant-oat01-BBB", _now_ms() + 3_600_000, "aaaaaa"),
        _row("b", "sk-ant-oat01-CCC", _now_ms() + 3_600_000, "bbbbbb"),
    ])
    pool = cp.load_pool("anthropic")
    first = pool.select()
    assert pool.try_refresh_current() is None  # no refresh_token, nothing newer on disk
    nxt = pool.mark_exhausted_and_rotate(status_code=401)
    assert nxt is not None and nxt.label != first.label

    # minter re-pushes the benched row with a new token and no status
    rows = auth_mod.read_credential_pool("anthropic")
    for r in rows:
        if r["label"] == first.label:
            r["access_token"] = "sk-ant-oat01-DDD"
            r["expires_at_ms"] = _now_ms() + 3_600_000
            for k in ("last_status", "last_status_at", "last_error_code", "last_error_reason",
                      "last_error_message", "last_error_reset_at"):
                r.pop(k, None)
    _write(home, "anthropic", rows)

    fresh = cp.load_pool("anthropic")
    assert {e.label: e.last_status for e in fresh._entries}[first.label] != "exhausted"

    avail, _ = pool._available_entries(clear_expired=True, refresh=True)
    live = {e.label: e for e in avail}
    assert live[first.label].access_token == "sk-ant-oat01-DDD"
    assert live[first.label].last_status == "ok"


def test_forced_refresh_on_norefresh_row_adopts_newer_disk_token(home):
    _write(home, "anthropic", [_row("a", "sk-ant-oat01-AAA", _now_ms() + 3_600_000, "aaaaaa")])
    pool = cp.load_pool("anthropic")
    pool.select()
    _write(home, "anthropic", [_row("a", "sk-ant-oat01-EEE", _now_ms() + 3_600_000, "aaaaaa")])
    refreshed = pool.try_refresh_current()
    assert refreshed is not None and refreshed.access_token == "sk-ant-oat01-EEE"


def test_unchanged_disk_token_is_not_adopted(home):
    _write(home, "anthropic", [_row("a", "sk-ant-oat01-AAA", _now_ms() + 3_600_000, "aaaaaa")])
    pool = cp.load_pool("anthropic")
    pool.select()
    assert pool.try_refresh_current() is None  # same token on disk -> benched as before
    assert pool._entries[0].last_status == "exhausted"


def test_codex_valid_norefresh_jwt_selected(home):
    _write(home, "openai-codex", [{
        "id": "cccccc", "label": "cx", "auth_type": "oauth", "priority": 0,
        "source": "manual:hub_minted", "access_token": _jwt(time.time() + 86400),
    }])
    assert cp.load_pool("openai-codex").select() is not None


def test_codex_live_pool_adopts_pushed_jwt_after_expiry(home):
    def codex_row(tok):
        return {"id": "cccccc", "label": "cx", "auth_type": "oauth", "priority": 0,
                "source": "manual:hub_minted", "access_token": tok}

    _write(home, "openai-codex", [codex_row(_jwt(time.time() + 10))])
    pool = cp.load_pool("openai-codex")
    assert pool.select() is None  # inside the expiry skew, nothing to refresh with
    new = _jwt(time.time() + 86400)
    _write(home, "openai-codex", [codex_row(new)])
    sel = pool.select()
    assert sel is not None and sel.access_token == new
