"""Picker-only auth parsing reuse must never supply a writer's working copy."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier

import pytest

from hermes_cli import auth
from hermes_cli.model_switch_providers import picker_build_scope


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    monkeypatch.setattr(auth, "_auth_file_path", lambda: path)
    monkeypatch.setattr(auth, "_load_global_auth_store", dict)
    path.write_text(json.dumps({"providers": {}, "credential_pool": {
        "alpha": [{"id": "one", "access_token": "fixture-old", "nested": {"value": 1}}],
        "beta": [{"id": "two"}]}}))
    return path


def count_loads(monkeypatch):
    calls = []
    original = auth._load_auth_store

    def load(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(auth, "_load_auth_store", load)
    return calls


def test_distinct_provider_reads_share_parse_only_inside_picker(store, monkeypatch):
    calls = count_loads(monkeypatch)
    with picker_build_scope():
        assert auth.read_credential_pool("alpha")[0]["id"] == "one"
        with picker_build_scope():
            assert auth.read_credential_pool("beta")[0]["id"] == "two"
        assert len(calls) == 1
    auth.read_credential_pool("alpha")
    auth.read_credential_pool("beta")
    assert len(calls) == 3
    with picker_build_scope():
        auth.read_credential_pool("alpha")
    assert len(calls) == 4


def test_load_pool_reuses_picker_parse(store, monkeypatch):
    from agent import credential_pool as pools

    monkeypatch.setattr(pools, "_seed_from_singletons", lambda *a: (False, set()))
    monkeypatch.setattr(pools, "_seed_from_env", lambda *a: (False, set()))
    rows = [{"id": "one", "source": "manual", "auth_type": "api_key", "priority": 0,
             "access_token": "fixture"}]
    store.write_text(json.dumps({"credential_pool": {p: rows for p in ("alpha", "beta", "gamma")}}))
    calls = count_loads(monkeypatch)
    with picker_build_scope():
        for provider in ("alpha", "beta", "gamma"):
            assert [e.id for e in pools.load_pool(provider).entries()] == ["one"]
        assert len(calls) == 1


def test_returned_nested_payload_is_not_the_snapshot(store):
    with picker_build_scope():
        rows = auth.read_credential_pool("alpha")
        rows[0]["nested"]["value"] = 9
        rows.clear()
        all_rows = auth.read_credential_pool()
        assert all_rows["alpha"][0]["nested"]["value"] == 1
        all_rows["alpha"][0]["nested"]["value"] = 7
        assert auth.read_credential_pool("alpha")[0]["nested"]["value"] == 1


@pytest.mark.parametrize("replacement", [False, True])
def test_same_size_mtime_write_visible_during_and_after_pass(store, replacement):
    with picker_build_scope():
        assert auth.read_credential_pool("alpha")[0]["access_token"] == "fixture-old"
        before = store.stat()
        changed = store.read_text().replace("fixture-old", "fixture-new")
        target = store.with_suffix(".new") if replacement else store
        target.write_text(changed)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        if replacement:
            target.replace(store)
        assert auth.read_credential_pool("alpha")[0]["access_token"] == "fixture-new"
    with picker_build_scope():
        assert auth.read_credential_pool("alpha")[0]["access_token"] == "fixture-new"


def test_writer_always_gets_fresh_independent_store(store, monkeypatch):
    calls = count_loads(monkeypatch)
    with picker_build_scope():
        auth.read_credential_pool("alpha")
        first = auth._load_auth_store()
        second = auth._load_auth_store()
        assert len(calls) == 3
        first["credential_pool"]["alpha"][0]["nested"]["value"] = 42
        assert second["credential_pool"]["alpha"][0]["nested"]["value"] == 1
        assert auth.read_credential_pool("alpha")[0]["nested"]["value"] == 1


def test_concurrent_passes_and_writer(store, monkeypatch):
    calls = count_loads(monkeypatch)
    barrier = Barrier(3)

    def reader():
        with picker_build_scope():
            old = auth.read_credential_pool("alpha")
            barrier.wait(timeout=10)
            barrier.wait(timeout=10)
            new = auth.read_credential_pool("alpha")
            return old[0]["access_token"], new[0]["access_token"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reader) for _ in range(2)]
        barrier.wait(timeout=10)
        assert len(calls) == 2
        value = auth._load_auth_store()
        value["credential_pool"]["alpha"][0]["access_token"] = "fixture-new"
        # Atomic publication, like _save_auth_store, without its unrelated home permission setup.
        replacement = store.with_suffix(".new")
        replacement.write_text(json.dumps(value))
        replacement.replace(store)
        barrier.wait(timeout=10)
        assert [f.result(timeout=10) for f in futures] == [("fixture-old", "fixture-new")] * 2
    assert len(calls) == 5


def test_missing_then_created_and_read_errors_are_not_memoized(store, monkeypatch):
    store.unlink()
    with picker_build_scope():
        assert auth.read_credential_pool("alpha") == []
        store.write_text('{"credential_pool":{"alpha":[{"id":"new"}]}}')
        assert auth.read_credential_pool("alpha")[0]["id"] == "new"
        store.unlink()
        def denied(*args, **kwargs):
            raise PermissionError("fixture")
        monkeypatch.setattr(auth, "_load_auth_store", denied)
        with pytest.raises(PermissionError):
            auth.read_credential_pool("alpha")


def test_copied_context_cannot_extend_or_cross_thread_scope(store, monkeypatch):
    calls = count_loads(monkeypatch)
    with picker_build_scope():
        auth.read_credential_pool("alpha")
        context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(context.run, auth.read_credential_pool, "beta").result(timeout=10)
        assert len(calls) == 2
        auth.read_credential_pool("beta")
        assert len(calls) == 2
    context.run(auth.read_credential_pool, "alpha")
    context.run(auth.read_credential_pool, "beta")
    assert len(calls) == 4


def test_corruption_keeps_loader_behavior(store, monkeypatch):
    store.write_text("not-json")
    calls = count_loads(monkeypatch)
    with picker_build_scope():
        assert auth.read_credential_pool("alpha") == []
        assert auth.read_credential_pool("beta") == []
    assert len(calls) == 2
    assert store.with_suffix(".json.corrupt").read_text() == "not-json"


def test_real_writer_preserves_other_provider_and_invalidates_snapshot(store):
    with picker_build_scope():
        auth.read_credential_pool("alpha")
        auth.write_credential_pool("beta", [{"id": "three", "source": "manual", "access_token": "fixture"}],
                                   removed_ids=["two"])
        assert auth.read_credential_pool("alpha")[0]["id"] == "one"
        assert [row["id"] for row in auth.read_credential_pool("beta")] == ["three"]


def test_warm_stat_failure_does_not_serve_cached_credentials(store, monkeypatch):
    from hermes_cli import auth_pool_read_scope as scope_module
    with picker_build_scope():
        auth.read_credential_pool("alpha")
        def denied(*args, **kwargs):
            raise PermissionError("fixture")
        monkeypatch.setattr(scope_module, "file_cache_version", denied)
        monkeypatch.setattr(auth, "_load_auth_store", denied)
        with pytest.raises(PermissionError):
            auth.read_credential_pool("alpha")


def test_sampled_path_is_the_path_parsed(store, monkeypatch):
    from hermes_cli import auth_pool_read_scope as scope_module
    other = store.with_suffix(".other")
    other.write_text(store.read_text().replace("fixture-old", "fixture-new"))
    selected = [store]
    monkeypatch.setattr(auth, "_auth_file_path", lambda: selected[0])
    real_version = scope_module.file_cache_version

    def switch_after_stat(path):
        version = real_version(path)
        selected[0] = other
        return version

    monkeypatch.setattr(scope_module, "file_cache_version", switch_after_stat)
    with picker_build_scope():
        assert auth.read_credential_pool("alpha")[0]["access_token"] == "fixture-old"
        selected[0] = store
        assert auth.read_credential_pool("alpha")[0]["access_token"] == "fixture-old"


def test_global_fallback_and_nested_copy(store, monkeypatch):
    global_rows = {"alpha": [{"id": "shadowed"}], "gamma": [{"id": "root", "nested": [1]}]}
    monkeypatch.setattr(auth, "_load_global_auth_store", lambda: {"credential_pool": global_rows})
    with picker_build_scope():
        assert auth.read_credential_pool("alpha")[0]["id"] == "one"
        rows = auth.read_credential_pool("gamma")
        rows[0]["nested"].append(2)
        assert auth.read_credential_pool()["gamma"][0]["nested"] == [1]
        assert auth.read_credential_pool("missing") == []

