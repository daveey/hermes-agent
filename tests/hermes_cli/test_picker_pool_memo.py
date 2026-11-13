"""``model.options`` reads each provider's pool and the auth store once per picker build (design
t_01a1d563 card J, rpc_dispatch row: load_pool was 27 of 39 hub samples under model.options).

The memo lives for ONE build only: a credential added, removed or exhausted between two builds is
seen by the next one, and an auth.json write in the middle of a build is re-read by the next check.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from hermes_cli import model_switch_providers as msp

_KEYS = {
    "OPENROUTER_API_KEY": "sk-or-test", "ANTHROPIC_API_KEY": "sk-ant-test", "OPENAI_API_KEY": "sk-test",
    "GEMINI_API_KEY": "g-test", "DEEPSEEK_API_KEY": "d-test", "XAI_API_KEY": "x-test",
    "MISTRAL_API_KEY": "m-test", "TOGETHER_API_KEY": "t-test",
}


@pytest.fixture
def home(monkeypatch):
    from hermes_constants import get_hermes_home
    h = get_hermes_home()
    for key in list(os.environ):
        if key.endswith("_API_KEY") or key.endswith("_TOKEN"):
            monkeypatch.delenv(key, raising=False)
    (h / ".env").write_text("".join(f"{k}={v}\n" for k, v in _KEYS.items()))
    for k, v in _KEYS.items():
        monkeypatch.setenv(k, v)
    (h / "config.yaml").write_text("model:\n  default: anthropic/claude-sonnet-4\n  provider: openrouter\n")
    return h


def _count(monkeypatch):
    import agent.credential_pool as cp
    from hermes_cli import auth
    calls = {"pool": [], "auth": 0}
    real_pool, real_auth = cp.load_pool, auth._load_auth_store

    def pool(provider, **kw):
        calls["pool"].append((provider or "").strip().lower())
        return real_pool(provider, **kw)

    def store(*a, **kw):
        calls["auth"] += 1
        return real_auth(*a, **kw)

    monkeypatch.setattr(cp, "load_pool", pool)
    monkeypatch.setattr(auth, "_load_auth_store", store)
    return calls


def _build():
    from hermes_cli.inventory import build_model_options_payload, load_picker_context
    return build_model_options_payload(load_picker_context())


def test_one_build_loads_each_pool_once(home, monkeypatch):
    _build()
    calls = _count(monkeypatch)
    _build()
    picker = [p for p in calls["pool"]]
    dupes = {p: picker.count(p) for p in set(picker) if picker.count(p) > 1}
    # inventory's usage/limit readers load the current provider outside the picker (two more)
    assert all(n <= 3 for n in dupes.values()), dupes
    assert len(picker) <= len(set(picker)) + 2, (len(picker), len(set(picker)))


def test_memo_is_scoped_to_one_build(home, monkeypatch):
    calls = _count(monkeypatch)
    msp._credential_pool_is_usable("openrouter")
    msp._credential_pool_is_usable("openrouter")
    assert calls["pool"].count("openrouter") == 2, "outside a build every call loads afresh"
    with msp.picker_build_scope():
        msp._credential_pool_is_usable("openrouter")
        msp._credential_pool_is_usable("openrouter", for_picker=True)
        with msp.picker_build_scope():  # nested: the outer build's memo
            msp._credential_pool_is_usable("OpenRouter ")
    assert calls["pool"].count("openrouter") == 3
    assert msp._POOL_STATE_MEMO.get() is None


def test_next_build_sees_an_exhausted_pool(home, monkeypatch):
    """A pool that runs dry between two builds drops out of the next picker (no cross-call memo)."""
    import agent.credential_pool as cp

    class _Pool:
        def __init__(self, available):
            self.available = available

        def has_credentials(self):
            return True

        def has_available(self, **_kw):
            return self.available

    state = {"available": True}
    monkeypatch.setattr(cp, "load_pool", lambda provider, **kw: _Pool(state["available"]))
    with msp.picker_build_scope():
        assert msp._credential_pool_is_usable("opencode-go")
    state["available"] = False
    with msp.picker_build_scope():
        assert not msp._credential_pool_is_usable("opencode-go")
        assert msp._credential_pool_is_usable("opencode-go", for_picker=True)


def test_auth_write_mid_build_is_reread(home, monkeypatch):
    """The shared auth parse is keyed on the file version: a load_pool seed that persists in the
    middle of a build is seen by the next read-only check."""
    from hermes_cli.auth import _auth_file_path
    path = _auth_file_path()
    path.write_text(json.dumps({"version": 1, "providers": {}}))
    calls = _count(monkeypatch)
    with msp.picker_build_scope():
        assert not msp._auth_store_has_provider("nous")
        assert not msp._auth_store_has_provider("nous")
        assert calls["auth"] == 1
        # a different size: the version changes even within one mtime tick
        path.write_text(json.dumps({"version": 1, "providers": {"nous": {"access_token": "t"}}}))
        assert msp._auth_store_has_provider("nous")
        assert calls["auth"] == 2


def _unguarded_io(monkeypatch) -> None:
    """Measure production cost: the suite's HomeIOGuard wraps os.stat/open in Python checks."""
    import builtins
    import io
    for module, name in ((os, "stat"), (os, "lstat"), (builtins, "open"), (io, "open")):
        fn = getattr(module, name)
        monkeypatch.setattr(module, name, getattr(fn, "__wrapped__", fn))


def test_replay_budget_model_options(home, monkeypatch):
    """Replay: a hub-sized auth.json (29 KB, pooled credentials for 8 providers) and the measured
    rate (11 model.options in ~400 s on the hub serve, P1 1316938). Budget: the picker's pool and
    auth reads at most one per provider per build (the gate above) and model.options at most
    0.3% of a core at that rate."""
    from hermes_cli.auth import _auth_file_path
    _unguarded_io(monkeypatch)
    pool = {}
    for env, key in _KEYS.items():
        slug = env.removesuffix("_API_KEY").lower()
        pool[slug] = [{"id": f"{slug}-{i}", "label": f"k{i}", "auth_type": "api_key", "priority": i,
                       "source": "manual", "access_token": f"{key}-{i:04d}" + "x" * 300} for i in range(10)]
    _auth_file_path().write_text(json.dumps({"version": 1, "providers": {}, "credential_pool": pool}))
    _build()

    def per_call() -> float:
        best = float("inf")  # best of N: CPU time still inflates on a loaded box (SMT, cache)
        for _ in range(5):
            t0 = time.thread_time()
            _build()
            best = min(best, time.thread_time() - t0)
        return best

    memo = per_call()
    with monkeypatch.context() as m:
        m.setattr(msp, "_POOL_STATE_MEMO", type("NoMemo", (), {
            "get": staticmethod(lambda: None), "set": staticmethod(lambda _v: None),
            "reset": staticmethod(lambda _t: None)})())
        unmemo = per_call()
    share = memo * 11 / 400
    print(f"model.options {memo * 1000:.1f} ms per call (unshared reads {unmemo * 1000:.1f} ms) "
          f"-> {share * 100:.3f}% of a core")
    assert share <= 0.003, f"{memo * 1000:.1f} ms per call = {share * 100:.3f}% (budget 0.3%)"
