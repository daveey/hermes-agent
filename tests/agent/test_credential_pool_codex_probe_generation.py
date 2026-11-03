"""A probe-restored Codex quota must reach disk, survive reload, and lose to a newer 429.

Issue #43747's live probe lifts a stale Codex cooldown on the pool's hot selection path.
That clear used to drop ``last_status_at`` to None; the auth-store recency merge read the
missing stamp as epoch 0, judged the still-future on-disk cooldown newer, and kept it. The
process that probed served the account, but disk and every fresh process stayed exhausted.

The recovery now keeps the failure generation it superseded (the exhaustion's
``last_status_at``). That stamp lets the merge tell which writes win:
  * a fresh reload reads the restored row as ok;
  * a stale writer still holding that same superseded exhaustion cannot write it back;
  * a 429 recorded after the probed generation (a newer stamp) still wins, on disk and on reload.
The same merge runs when a named profile writes the root's borrowed rows (#100339).

Real imports, real temp HERMES_HOME (root + named profile), real auth.json I/O; only the
``/usage`` probe is replaced.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

import hermes_cli.auth as auth_mod
from hermes_cli import auth_codex

PROVIDER = "openai-codex"
ROW_ID = "cred-quota"


def _jwt(claims: dict) -> str:
    def _part(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{_part({'alg': 'none'})}.{_part(claims)}.sig"


def _exhausted_row(observed_at: float) -> dict:
    return {
        "id": ROW_ID,
        "label": "quota-frozen",
        "auth_type": "oauth",
        "priority": 0,
        # manual: no singleton re-sync from providers.openai-codex, so the pool row is the
        # only status authority under test.
        "source": "manual:device_code",
        "access_token": _jwt({"exp": time.time() + 6 * 3600}),
        "refresh_token": "rf-live",
        "last_status": "exhausted",
        "last_status_at": observed_at,
        "last_error_code": 429,
        "last_error_reason": "usage_limit_reached",
        "last_error_message": "The usage limit has been reached",
        "last_error_reset_at": observed_at + 5 * 24 * 3600,
    }


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    auth_mod._codex_quota_probe_cache.clear()
    yield
    auth_mod._codex_quota_probe_cache.clear()


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Root HERMES_HOME whose only Codex pool row sits in a still-binding 429 cooldown."""
    root = tmp_path / "hermes-root"
    root.mkdir()
    (tmp_path / "fakehome").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    for var in ("OPENAI_API_KEY", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    import hermes_constants
    hermes_constants._default_hermes_root_memo = None  # type: ignore[attr-defined]

    observed_at = time.time() - 600  # the exhaustion this test's probe supersedes
    store = {"version": 1, "providers": {}, "credential_pool": {PROVIDER: [_exhausted_row(observed_at)]}}
    (root / "auth.json").write_text(json.dumps(store))

    probe = {"result": True, "calls": 0, "side_effect": None}

    def _probe(token, **kw):
        probe["calls"] += 1
        side_effect, probe["side_effect"] = probe["side_effect"], None  # one shot: a peer's
        if side_effect is not None:  # own rotate-and-select probes again and must not recurse
            side_effect()
        return probe["result"]

    monkeypatch.setattr(auth_mod, "_probe_codex_quota_restored", _probe)
    monkeypatch.setattr(auth_codex, "_probe_codex_quota_restored", _probe)

    def use(home):
        """Switch the process to *home* (root or a profile dir), as a new process would."""
        monkeypatch.setenv("HERMES_HOME", str(home))
        hermes_constants._default_hermes_root_memo = None  # type: ignore[attr-defined]
        auth_mod._global_auth_store_cache = None
        auth_mod._oauth_heal_clean_marks.clear()
        auth_mod._codex_quota_probe_cache.clear()

    def row(home):
        p = home / "auth.json"
        if not p.exists():
            return None
        rows = (json.loads(p.read_text()).get("credential_pool") or {}).get(PROVIDER) or []
        return next((r for r in rows if r.get("id") == ROW_ID), None)

    return {"root": root, "observed_at": observed_at, "probe": probe, "use": use, "row": row}


def _fresh_select(fleet, home):
    """A fresh process at *home* with a probe that says "still exhausted": only disk speaks."""
    from agent.credential_pool import load_pool

    fleet["use"](home)
    fleet["probe"]["result"] = False
    fleet["probe"]["side_effect"] = None
    return load_pool(PROVIDER).select()


def _profile(fleet, name):
    from hermes_cli.profiles import create_profile

    fleet["use"](fleet["root"])
    return create_profile(name)


def test_probe_restored_quota_persists_and_survives_fresh_reload(fleet):
    from agent.credential_pool import STATUS_OK, load_pool

    selected = load_pool(PROVIDER).select()

    assert fleet["probe"]["calls"] == 1
    assert selected is not None and selected.last_status == STATUS_OK
    disk = fleet["row"](fleet["root"])
    assert disk["last_status"] == STATUS_OK
    assert disk["last_error_reset_at"] is None and disk["last_error_code"] is None
    # The superseded generation, not "now" and not None: see the module docstring.
    assert disk["last_status_at"] == pytest.approx(fleet["observed_at"])

    reloaded = _fresh_select(fleet, fleet["root"])
    assert reloaded is not None and reloaded.id == ROW_ID and reloaded.last_status == STATUS_OK
    assert fleet["probe"]["calls"] == 1  # the restored row needs no second probe


def test_probe_still_exhausted_keeps_cooldown_on_disk_and_reload(fleet):
    """Control: a probe that says the quota is still used up clears nothing anywhere."""
    from agent.credential_pool import STATUS_EXHAUSTED, load_pool

    fleet["probe"]["result"] = False
    assert load_pool(PROVIDER).select() is None

    disk = fleet["row"](fleet["root"])
    assert disk["last_status"] == STATUS_EXHAUSTED
    assert disk["last_status_at"] == pytest.approx(fleet["observed_at"])
    assert _fresh_select(fleet, fleet["root"]) is None


def test_newer_concurrent_429_beats_probe_restored_quota(fleet):
    """A peer records a fresh 429 while this process probes: the peer's newer cooldown stays."""
    from agent.credential_pool import STATUS_EXHAUSTED, STATUS_OK, load_pool

    peer = load_pool(PROVIDER)
    restorer = load_pool(PROVIDER)
    fresh_reset = time.time() + 3 * 3600

    def _peer_429():
        fleet["probe"]["result"] = False  # the peer's own rotate-and-select probe: still limited
        peer.mark_exhausted_and_rotate(
            status_code=429, credential_id=ROW_ID,
            error_context={"reason": "usage_limit_reached", "message": "limit", "reset_at": fresh_reset},
        )
        assert fleet["row"](fleet["root"])["last_error_reset_at"] == pytest.approx(fresh_reset)
        fleet["probe"]["result"] = True  # the restorer's probe saw the pre-429 window open

    fleet["probe"]["side_effect"] = _peer_429
    selected = restorer.select()
    assert selected is not None and selected.last_status == STATUS_OK  # this process's own view

    disk = fleet["row"](fleet["root"])
    assert disk["last_status"] == STATUS_EXHAUSTED
    assert disk["last_status_at"] > fleet["observed_at"]
    assert disk["last_error_reset_at"] == pytest.approx(fresh_reset)
    assert _fresh_select(fleet, fleet["root"]) is None


def test_stale_writer_cannot_resurrect_the_superseded_cooldown(fleet):
    """A process loaded before the recovery flushes its stale exhausted snapshot; then a NEW 429."""
    from agent.credential_pool import STATUS_EXHAUSTED, STATUS_OK, load_pool

    stale = load_pool(PROVIDER)
    assert stale._entries[0].last_status == STATUS_EXHAUSTED
    assert load_pool(PROVIDER).select().last_status == STATUS_OK

    stale._persist()  # an ordinary flush carrying the exhaustion the probe superseded
    assert fleet["row"](fleet["root"])["last_status"] == STATUS_OK
    assert _fresh_select(fleet, fleet["root"]).last_status == STATUS_OK

    # The same stale writer then observes a genuinely new 429: that one is newer and binds.
    stale.mark_exhausted_and_rotate(
        status_code=429, credential_id=ROW_ID,
        error_context={"reason": "usage_limit_reached", "message": "limit", "reset_at": time.time() + 3600},
    )
    disk = fleet["row"](fleet["root"])
    assert disk["last_status"] == STATUS_EXHAUSTED and disk["last_status_at"] > fleet["observed_at"]
    assert _fresh_select(fleet, fleet["root"]) is None


def test_borrowing_profile_recovery_writes_root_row_and_survives_reload(fleet):
    """A named profile borrows the root's Codex rows (#100339): its recovery goes through the
    root write-through merge and must persist there, for root and profile reloads alike."""
    from agent.credential_pool import STATUS_EXHAUSTED, STATUS_OK, load_pool

    profile = _profile(fleet, "worker")
    fleet["use"](fleet["root"])
    stale_root = load_pool(PROVIDER)  # root process loaded before the profile's recovery

    fleet["use"](profile)
    pool = load_pool(PROVIDER)
    assert pool._borrowed_root_ids == {ROW_ID}
    assert pool.select().last_status == STATUS_OK
    assert fleet["row"](profile) is None  # still no profile-local fork of the grant

    root_row = fleet["row"](fleet["root"])
    assert root_row["last_status"] == STATUS_OK
    assert root_row["last_status_at"] == pytest.approx(fleet["observed_at"])
    assert _fresh_select(fleet, profile).last_status == STATUS_OK
    assert _fresh_select(fleet, fleet["root"]).last_status == STATUS_OK

    fleet["use"](fleet["root"])
    stale_root._persist()
    assert fleet["row"](fleet["root"])["last_status"] == STATUS_OK

    # A newer 429 from the profile still binds the shared root row.
    fleet["use"](profile)
    load_pool(PROVIDER).mark_exhausted_and_rotate(
        status_code=429, credential_id=ROW_ID,
        error_context={"reason": "usage_limit_reached", "message": "limit", "reset_at": time.time() + 3600},
    )
    assert fleet["row"](fleet["root"])["last_status"] == STATUS_EXHAUSTED
    assert _fresh_select(fleet, profile) is None
    assert _fresh_select(fleet, fleet["root"]) is None
