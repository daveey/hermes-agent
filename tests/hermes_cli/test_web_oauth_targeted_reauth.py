"""Target-bound Codex device sessions: a selected pool row is replaced, never appended.

The dashboard's ordinary Codex login writes the ``providers.openai-codex`` singleton. A
recovery of one selected ``manual:*`` row must instead commit into that exact row, refuse a
different principal, lose to cancel/deletion, and never fall back to the untargeted login.
All credentials and provider replies here are fixtures.
"""
import base64
import json
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from hermes_cli.web_server import _SESSION_TOKEN, app
import hermes_cli.web_routers.oauth as _rt_oauth
import hermes_cli.web_server_oauth as _web_server_oauth

client = TestClient(app)
HEADERS = {"X-Hermes-Session-Token": _SESSION_TOKEN}
START = "/api/providers/oauth/openai-codex/start?profile=coder"


def _jwt(account, subject, nonce):
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": account}, "sub": subject, "nonce": nonce}
    return "fixture." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".fixture"


def _row(**over):
    row = dict(id="selected", source="manual:device_code", auth_type="oauth", label="Example", priority=3,
               access_token=_jwt("workspace-a", "subject-a", "old"), refresh_token="fixture-old-refresh",
               last_status="dead", last_error_code=401, last_error_reason="auth")
    row.update(over)
    return row


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = tmp_path / "profiles" / "coder"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("{}\n")
    path = home / "auth.json"
    other = _row(id="other", label="Other", access_token=_jwt("workspace-b", "subject-b", "old"),
                 refresh_token="fixture-other-refresh", last_status=None)
    path.write_text(json.dumps({"providers": {}, "credential_pool": {"openai-codex": [_row(), other]}}))
    created = set(_web_server_oauth._oauth_sessions)
    yield path
    for sid in set(_web_server_oauth._oauth_sessions) - created:
        _web_server_oauth._oauth_sessions.pop(sid, None)


def _rows(path):
    return json.loads(path.read_text())["credential_pool"]["openai-codex"]


class _Resp:
    def __init__(self, payload):
        self.status_code, self._payload = 200, payload

    def json(self):
        return self._payload


def _provider(monkeypatch, access_token, on_sleep=None):
    """Scripted OpenAI device-auth endpoints; the worker's real router logic runs against them."""
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            if url.endswith("/deviceauth/usercode"):
                return _Resp({"device_auth_id": "device-auth-id", "interval": 3, "user_code": "CODEX-1234"})
            if url.endswith("/deviceauth/token"):
                return _Resp({"authorization_code": "auth-code", "code_verifier": "verifier"})
            return _Resp({"access_token": access_token, "refresh_token": "fixture-new-refresh"})

    from hermes_cli import web_server as ws
    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(ws.time, "sleep", on_sleep or (lambda *_a, **_k: None))


def _fake_worker(monkeypatch):
    """Stand in for the provider round-trip so /start returns without polling."""
    ran = []

    def worker(sid):
        ran.append(sid)
        _web_server_oauth._oauth_sessions[sid].update(
            user_code="CODEX-1234", verification_url="https://auth.openai.com/codex/device", interval=5)

    monkeypatch.setattr(_rt_oauth, "_codex_full_login_worker", worker)
    return ran


def _bound_session(store):
    """Bind through the real /start route (provider round-trip stubbed); return the session id."""
    def worker(sid):
        _web_server_oauth._oauth_sessions[sid].update(
            user_code="CODEX-1234", verification_url="https://auth.openai.com/codex/device", interval=5)

    with patch.object(_rt_oauth, "_codex_full_login_worker", worker):
        resp = client.post(START, headers=HEADERS, json={"credential_id": "selected"})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def _poll(sid):
    resp = client.get(f"/api/providers/oauth/openai-codex/poll/{sid}?profile=coder", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_start_binds_selected_row_and_reports_it(store, monkeypatch):
    ran = _fake_worker(monkeypatch)
    resp = client.post(START, headers=HEADERS, json={"credential_id": "selected"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target_bound"] is True and body["credential_id"] == "selected"
    assert "fixture" not in json.dumps(body)
    sess = _web_server_oauth._oauth_sessions[body["session_id"]]
    assert sess["reauth_target"].credential_id == "selected"
    assert ran == [body["session_id"]]


def test_start_without_target_stays_an_ordinary_login(store, monkeypatch):
    _fake_worker(monkeypatch)
    resp = client.post(START, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "target_bound" not in body
    assert _web_server_oauth._oauth_sessions[body["session_id"]].get("reauth_target") is None


@pytest.mark.parametrize("payload,status", [
    ({"credential_id": ""}, 400), ({"credential_id": 7}, 400), ({"credential_id": None}, 400),
    (["selected"], 400), ({"credential_id": "missing"}, 409)])
def test_bad_target_never_falls_back_to_untargeted_login(store, monkeypatch, payload, status):
    ran = _fake_worker(monkeypatch)
    before = set(_web_server_oauth._oauth_sessions)
    resp = client.post(START, headers=HEADERS, json=payload)
    assert resp.status_code == status, resp.text
    assert ran == [] and set(_web_server_oauth._oauth_sessions) == before


@pytest.mark.parametrize("source", ["device_code", "env:EXAMPLE_TOKEN", "manual:unknown"])
def test_unowned_source_is_refused_at_start(store, monkeypatch, source):
    ran = _fake_worker(monkeypatch)
    data = json.loads(store.read_text())
    data["credential_pool"]["openai-codex"][0]["source"] = source
    store.write_text(json.dumps(data))
    resp = client.post(START, headers=HEADERS, json={"credential_id": "selected"})
    assert resp.status_code == 409, resp.text
    assert "original credential source" in resp.json()["detail"]
    assert ran == []


def test_target_on_provider_without_targeted_recovery_is_refused(store, monkeypatch):
    started = []
    monkeypatch.setattr(_rt_oauth, "_start_device_code_flow",
                        lambda *a, **k: started.append(a) or (_ for _ in ()).throw(AssertionError("started")))
    resp = client.post("/api/providers/oauth/xai-oauth/start?profile=coder", headers=HEADERS,
                       json={"credential_id": "selected"})
    assert resp.status_code == 400, resp.text
    assert started == []


def test_approval_replaces_only_the_selected_row(store, monkeypatch):
    sid = _bound_session(store)
    new_token = _jwt("workspace-a", "subject-a", "new")
    _provider(monkeypatch, new_token)
    other_before = _rows(store)[1]

    _rt_oauth._codex_full_login_worker(sid)

    rows = _rows(store)
    assert [r["id"] for r in rows] == ["selected", "other"]
    assert rows[0]["access_token"] == new_token and rows[0]["refresh_token"] == "fixture-new-refresh"
    assert rows[0]["label"] == "Example" and rows[0]["priority"] == 3 and rows[0]["source"] == "manual:device_code"
    assert rows[0].get("last_status") is None and "last_error_code" not in rows[0]
    assert rows[1] == other_before
    # The singleton login path must not have run: it would seed a second row for this account.
    assert not json.loads(store.read_text())["providers"].get("openai-codex")
    polled = _poll(sid)
    assert polled["status"] == "approved"
    assert polled["credential_id"] == "selected" and polled["identity_verified"] is True
    assert "fixture" not in json.dumps(polled)


def _run_refused(store, monkeypatch, access_token, mutate=None):
    sid = _bound_session(store)
    if mutate:
        mutate()
    before = store.read_bytes()
    _provider(monkeypatch, access_token)
    _rt_oauth._codex_full_login_worker(sid)
    assert store.read_bytes() == before
    polled = _poll(sid)
    assert polled["status"] == "error" and polled["reason"] == "reauth_conflict"
    assert polled.get("credential_id") is None
    return polled


@pytest.mark.parametrize("account,subject", [("workspace-b", "subject-a"), ("workspace-a", "subject-b")])
def test_other_principal_is_refused_without_write(store, monkeypatch, account, subject):
    polled = _run_refused(store, monkeypatch, _jwt(account, subject, "new"))
    assert "does not match the selected account" in polled["error_message"]


@pytest.mark.parametrize("change", ["delete", "rotate"])
def test_row_changed_during_sign_in_is_refused_without_write(store, monkeypatch, change):
    def mutate():
        data = json.loads(store.read_text())
        rows = data["credential_pool"]["openai-codex"]
        if change == "delete":
            rows.pop(0)
        else:
            rows[0]["refresh_token"] = "fixture-peer-rotated"
        store.write_text(json.dumps(data))

    _run_refused(store, monkeypatch, _jwt("workspace-a", "subject-a", "new"), mutate)


def test_cancel_during_poll_leaves_selected_row_untouched(store, monkeypatch):
    sid = _bound_session(store)
    before = store.read_bytes()

    def cancel(*_a, **_k):
        resp = client.delete(f"/api/providers/oauth/sessions/{sid}?profile=coder", headers=HEADERS)
        assert resp.status_code == 200, resp.text

    _provider(monkeypatch, _jwt("workspace-a", "subject-a", "new"), on_sleep=cancel)
    _rt_oauth._codex_full_login_worker(sid)
    assert store.read_bytes() == before
    assert sid not in _web_server_oauth._oauth_sessions


def test_second_session_for_the_same_row_cannot_overwrite_the_first(store, monkeypatch):
    first, second = _bound_session(store), _bound_session(store)
    first_token = _jwt("workspace-a", "subject-a", "first")
    _provider(monkeypatch, first_token)
    _rt_oauth._codex_full_login_worker(first)
    _provider(monkeypatch, _jwt("workspace-a", "subject-a", "second"))
    _rt_oauth._codex_full_login_worker(second)
    assert _rows(store)[0]["access_token"] == first_token
    assert _poll(first)["status"] == "approved"
    assert _poll(second)["reason"] == "reauth_conflict"
