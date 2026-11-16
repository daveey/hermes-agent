"""Whole-payload memo for ``build_model_options_payload`` (the ``model.options`` RPC).

Every input that can change the payload is a named part of the memo stamp
(``inventory._STAMP_PARTS``). For each part there is (a) a test that changing it rebuilds, and (b) a
mutant check: with that part pinned to a constant the same change is served stale, so (a) can fail.
Fixtures only: HERMES_HOME is the suite's temp home.
"""
import json
import os
import socket
import threading

import pytest

from hermes_cli import auth, inventory


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from agent import models_dev
    monkeypatch.setattr(models_dev, "fetch_models_dev", dict)

    def refuse(*a, **k):
        raise AssertionError("memo fixtures must stay offline")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(inventory, "_prewarm_pricing_async", lambda *a, **k: None)
    monkeypatch.setattr(inventory, "_MEMO_ENABLED", True)
    inventory.reset_model_options_memo()
    yield
    inventory.reset_model_options_memo()


@pytest.fixture
def home(monkeypatch):
    from hermes_constants import get_hermes_home
    h = get_hermes_home()
    for key in list(os.environ):
        if key.endswith(("_API_KEY", "_TOKEN")):
            monkeypatch.delenv(key, raising=False)
    (h / "config.yaml").write_text("model:\n  default: x\n  provider: openrouter\n")
    return h


@pytest.fixture
def builds(monkeypatch):
    """Counts real payload builds."""
    real = inventory.build_models_payload
    calls = []

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(inventory, "build_models_payload", counting)
    return calls


def _call(**kw):
    return inventory.build_model_options_payload(inventory.load_picker_context(), **kw)


_ROW = {"id": "a", "label": "l", "auth_type": "api_key", "priority": 0,
        "source": "manual", "access_token": "fixture-key"}


def _auth_write(pool):
    auth._auth_file_path().write_text(json.dumps({"providers": {}, "credential_pool": pool}))


def test_repeat_call_is_served_from_memo(home, builds):
    first = _call()
    second = _call()
    assert len(builds) == 1
    assert first == second


def test_memo_returns_independent_copies(home, builds, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    first = _call()
    assert first["providers"]
    first["providers"].clear()
    first["poison"] = True
    again = _call()
    assert len(builds) == 1
    assert again["providers"] and "poison" not in again


def test_refresh_bypasses_and_clears_memo(home, builds):
    _call()
    _call(refresh=True)
    assert len(builds) == 2
    _call(refresh=True)
    assert len(builds) == 3
    _call()
    assert len(builds) == 4  # refresh dropped the entry; this one rebuilds


def test_exception_is_not_memoized(home, monkeypatch):
    real = inventory.build_models_payload
    state = {"n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(inventory, "build_models_payload", flaky)
    with pytest.raises(RuntimeError):
        _call()
    _call()
    _call()
    assert state["n"] == 2


def test_ttl_expiry_rebuilds(home, builds, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(inventory, "_memo_clock", lambda: clock["t"])
    _call()
    clock["t"] += inventory._MEMO_TTL_S - 0.1
    _call()
    assert len(builds) == 1
    clock["t"] += 0.2
    _call()
    assert len(builds) == 2


# --- one entry per stamp part: (setup, change) ---------------------------------------------------

def _cfg(home, extra=""):
    (home / "config.yaml").write_text("model:\n  default: x\n  provider: openrouter\n" + extra)


_PROV = "providers:\n  myco:\n    api: http://127.0.0.1:9/v1\n    api_key: k\n    default_model: %s\n"


def _set_config(home, monkeypatch):
    # Outside ``model``/``providers``, so the picker context is unchanged: only the config stamp sees it.
    _cfg(home, "agent:\n  max_turns: 7\n")


def _set_dotenv(home, monkeypatch):
    (home / ".env").write_text("DEEPSEEK_API_KEY=from-dotenv\n")


def _set_auth(home, monkeypatch):
    _auth_write({"deepseek": [_ROW]})


def _set_environ(home, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")


def _set_catalog_file(home, monkeypatch):
    (home / "provider_models_cache.json").write_text("{}")


def _set_plugins(home, monkeypatch):
    import providers
    real = providers.bound_registry_key
    monkeypatch.setattr(providers, "bound_registry_key", lambda: (real(), "new-plugin"))


def _set_usage(home, monkeypatch):
    from datetime import UTC, datetime
    from agent import account_usage_cache as cache
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
    window = AccountUsageWindow(label="5h", used_percent=10.0, reset_at=datetime.now(UTC))
    snapshot = AccountUsageSnapshot(provider="anthropic", source="test", fetched_at=datetime.now(UTC),
                                    windows=(window,))
    cache.remember_account_usage("anthropic", snapshot)


def _set_home(home, monkeypatch, tmp_path_factory=None):
    other = home.parent / "other-home"
    other.mkdir(exist_ok=True)
    (other / "config.yaml").write_text("model:\n  default: x\n  provider: openrouter\n")
    monkeypatch.setenv("HERMES_HOME", str(other))


def _set_pricing(home, monkeypatch):
    from hermes_cli.models_pricing import _cache_catalog
    _cache_catalog("fixture-pricing-key", {})


CASES = {
    "pricing": _set_pricing,
    "config": _set_config,
    "dotenv": _set_dotenv,
    "auth": _set_auth,
    "environ": _set_environ,
    "catalog_files": _set_catalog_file,
    "plugins": _set_plugins,
    "usage": _set_usage,
    "home": _set_home,
}


def test_every_case_names_a_stamp_part():
    assert set(CASES) <= set(inventory._STAMP_PARTS)
    assert set(inventory._STAMP_PARTS) - set(CASES) <= {"external_creds"}


def _isolate(name, monkeypatch):
    """Switching HERMES_HOME moves every file-derived part too; hold those so only ``home`` can move."""
    if name == "home":
        for other in inventory._STAMP_PARTS:
            if other != "home":
                monkeypatch.setitem(inventory._STAMP_PARTS, other, lambda: "held")


@pytest.mark.parametrize("name", sorted(CASES))
def test_change_in_stamp_part_rebuilds(name, home, builds, monkeypatch):
    _isolate(name, monkeypatch)
    _call()
    assert len(builds) == 1
    CASES[name](home, monkeypatch)
    _call()
    assert len(builds) == 2, f"change in {name!r} was served from the memo"


@pytest.mark.parametrize("name", sorted(CASES))
def test_mutant_pinning_part_is_served_stale(name, home, builds, monkeypatch):
    """The mutation check: pin one stamp part and its change goes unseen, so the test above is live."""
    monkeypatch.setitem(inventory._STAMP_PARTS, name, lambda: "pinned")
    _isolate(name, monkeypatch)
    _call()
    CASES[name](home, monkeypatch)
    _call()
    assert len(builds) == 1


def test_external_credential_file_change_rebuilds(home, builds, monkeypatch):
    fake_home = home.parent / "fakehome"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(inventory, "_external_credential_paths", lambda: [fake_home / ".claude" / ".credentials.json"])
    _call()
    (fake_home / ".claude" / ".credentials.json").write_text("{}")
    _call()
    assert len(builds) == 2


def test_same_size_same_tick_rewrite_of_credential_files_is_seen(home, builds):
    (home / ".env").write_text("DEEPSEEK_API_KEY=aaaa\n")
    _call()
    st = (home / ".env").stat()
    (home / ".env").write_text("DEEPSEEK_API_KEY=bbbb\n")
    os.utime(home / ".env", ns=(st.st_atime_ns, st.st_mtime_ns))  # identical size and mtime
    _call()
    assert len(builds) == 2


@pytest.mark.parametrize("kw", [{"explicit_only": True}, {"include_unconfigured": True}])
def test_flags_are_part_of_the_key(kw, home, builds):
    _call()
    _call(**kw)
    assert len(builds) == 2
    _call(**kw)
    assert len(builds) == 2


def test_ctx_overrides_are_part_of_the_key(home, builds):
    ctx = inventory.load_picker_context()
    inventory.build_model_options_payload(ctx)
    inventory.build_model_options_payload(ctx.with_overrides(current_provider="deepseek"))
    assert len(builds) == 2


# --- the J lever's add/change/remove visibility matrix, memo on vs memo off ----------------------

def test_visibility_matrix_matches_unmemoized_output(home, monkeypatch):
    steps = []
    # env key add / change / remove
    steps += [lambda: monkeypatch.setenv("DEEPSEEK_API_KEY", "k1"),
              lambda: monkeypatch.setenv("DEEPSEEK_API_KEY", "k2"),
              lambda: monkeypatch.delenv("DEEPSEEK_API_KEY")]
    # config provider add / change / remove
    steps += [lambda: _cfg(home, _PROV % "m1"), lambda: _cfg(home, _PROV % "m2"), lambda: _cfg(home)]
    # auth pool add / change / remove
    steps += [lambda: _auth_write({"deepseek": [_ROW]}),
              lambda: _auth_write({"deepseek": [dict(_ROW, access_token="fixture-key-2")]}),
              lambda: _auth_write({})]
    # .env add / remove
    steps += [lambda: (home / ".env").write_text("DEEPSEEK_API_KEY=e1\n"), lambda: (home / ".env").unlink()]
    assert len(steps) == 11
    _call()  # prime memo with the initial state
    for step in steps:
        step()
        # the memoized entry (primed by the previous iteration) must not be what we compare against
        cur = _call()
        inventory.reset_model_options_memo()
        inventory._MEMO_ENABLED = False
        try:
            fresh = _call()
        finally:
            inventory._MEMO_ENABLED = True
        assert cur == fresh
        _call()  # re-prime for the next step
