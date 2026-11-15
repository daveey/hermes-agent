"""Model-picker pass costs: repeated lookups inside one ``model.options`` pass resolve once, and
every input change shows on the next pass (no clock, no cross-pass staleness)."""
import contextvars
import threading

import providers
from agent import codex_service_tiers as tiers
from agent import models_dev
from agent.pass_memo import PassMemo
from providers.base import ProviderProfile

CODEX_ROW = {"slug": "openai-codex", "models": ["gpt-a", "gpt-b", "gpt-c", "gpt-d", "gpt-e"],
             "api_url": "https://chatgpt.com/backend-api/codex"}


def _count_stored_logins(monkeypatch, login):
    from hermes_cli import auth_codex

    calls = []

    def resolve(**kw):
        calls.append(kw)
        return {"api_key": login[0]}

    monkeypatch.setattr(auth_codex, "resolve_codex_runtime_credentials", resolve)
    monkeypatch.setattr(tiers, "_cyber_programs_for_model", lambda *a, **k: None)
    monkeypatch.setattr("agent.credential_pool_disabled.token_disabled", lambda *a, **k: False)
    monkeypatch.setattr("hermes_cli.auth_constants._decode_jwt_claims", lambda t: {"ok": 1})
    return calls


def _capabilities(rows):
    from hermes_cli.inventory import _apply_capabilities

    _apply_capabilities(rows, metadata_config={})


def test_capabilities_pass_reads_the_stored_codex_login_once(monkeypatch):
    calls = _count_stored_logins(monkeypatch, ["tok-1"])
    _capabilities([dict(CODEX_ROW, models=list(CODEX_ROW["models"]))])
    assert len(calls) == 1, f"one stored-login read per pass, got {len(calls)}"


def test_next_pass_sees_a_changed_login(monkeypatch):
    login = ["tok-1"]
    seen = []
    calls = _count_stored_logins(monkeypatch, login)
    monkeypatch.setattr(tiers, "_cyber_programs_for_model",
                        lambda token, base, model: seen.append(token) or None)
    _capabilities([dict(CODEX_ROW, models=["gpt-a"])])
    login[0] = "tok-2"  # refresh / re-login between passes
    _capabilities([dict(CODEX_ROW, models=["gpt-a"])])
    assert seen == ["tok-1", "tok-2"] and len(calls) == 2


def test_scope_does_not_outlive_the_pass(monkeypatch):
    login = ["tok-1"]
    calls = _count_stored_logins(monkeypatch, login)
    with tiers.account_token_scope():
        assert tiers._account_catalog_token(None) == "tok-1"
        login[0] = "tok-2"  # inside the pass the memo answers
        assert tiers._account_catalog_token(None) == "tok-1"
    assert tiers._account_catalog_token(None) == "tok-2"
    assert len(calls) == 2


def test_explicit_token_bypasses_the_scope(monkeypatch):
    _count_stored_logins(monkeypatch, ["stored"])
    with tiers.account_token_scope():
        assert tiers._account_catalog_token(None) == "stored"
        assert tiers._account_catalog_token("passed") == "passed"


def test_a_missing_login_is_also_resolved_once_per_pass(monkeypatch):
    from hermes_cli import auth_codex
    from hermes_cli.auth_constants import AuthError

    calls = []

    def resolve(**kw):
        calls.append(1)
        raise AuthError("no login", provider="openai-codex")

    monkeypatch.setattr(auth_codex, "resolve_codex_runtime_credentials", resolve)
    with tiers.account_token_scope():
        assert [tiers._account_catalog_token(None) for _ in range(4)] == [None] * 4
    assert len(calls) == 1


def test_a_concurrent_request_does_not_inherit_this_pass(monkeypatch):
    login = ["tok-1"]
    _count_stored_logins(monkeypatch, login)
    other = []
    with tiers.account_token_scope():
        assert tiers._account_catalog_token(None) == "tok-1"
        login[0] = "tok-2"
        t = threading.Thread(target=lambda: other.append(tiers._account_catalog_token(None)))
        t.start()
        t.join(5)
    assert other == ["tok-2"]


def test_a_copied_context_cannot_use_a_closed_token_pass(monkeypatch):
    login = ["tok-1"]
    _count_stored_logins(monkeypatch, login)
    with tiers.account_token_scope():
        assert tiers._account_catalog_token(None) == "tok-1"
        ctx = contextvars.copy_context()
    login[0] = "tok-2"
    assert ctx.run(tiers._account_catalog_token, None) == "tok-2"


# ── models.dev profile ids ────────────────────────────────────────────────


def _isolated_registry(monkeypatch):
    monkeypatch.setattr(providers, "_REGISTRY", {})
    monkeypatch.setattr(providers, "_ALIASES", {})
    monkeypatch.setattr(providers, "_PROVIDER_LIST_CACHE", None)
    monkeypatch.setattr(providers, "_SOURCES", {})
    monkeypatch.setattr(providers, "_discovered", True)


def test_profile_ids_are_read_once_per_pass(monkeypatch):
    _isolated_registry(monkeypatch)
    providers.register_provider(ProviderProfile(name="fx-one", models_dev_id="fx-one-dev"))
    real = providers.list_providers
    calls = []
    monkeypatch.setattr(providers, "list_providers", lambda: calls.append(1) or real())
    with models_dev.profile_ids_scope():
        for _ in range(50):
            assert models_dev.models_dev_id_for("fx-one") == "fx-one-dev"
    assert len(calls) == 1


def test_outside_a_pass_every_lookup_reads_the_registry(monkeypatch):
    _isolated_registry(monkeypatch)
    providers.register_provider(ProviderProfile(name="fx-one", models_dev_id="fx-one-dev"))
    real = providers.list_providers
    calls = []
    monkeypatch.setattr(providers, "list_providers", lambda: calls.append(1) or real())
    for _ in range(3):
        models_dev.models_dev_id_for("fx-one")
    assert len(calls) == 3


def test_every_registry_change_is_visible_on_the_next_pass(monkeypatch):
    _isolated_registry(monkeypatch)
    prof = ProviderProfile(name="fx-one", models_dev_id="fx-one-dev")
    providers.register_provider(prof)

    def one_pass(name):
        with models_dev.profile_ids_scope():
            return models_dev.models_dev_id_for(name)

    assert one_pass("fx-two") is None
    providers.register_provider(ProviderProfile(name="fx-two", models_dev_id="fx-two-dev"))
    assert one_pass("fx-two") == "fx-two-dev"
    providers.register_provider(ProviderProfile(name="fx-two", models_dev_id="fx-two-dev-v2"))
    assert one_pass("fx-two") == "fx-two-dev-v2"
    # Changes that bypass register_provider (registry restore + list-cache reset) also show.
    providers._REGISTRY.pop("fx-two")
    providers._PROVIDER_LIST_CACHE = None
    assert one_pass("fx-two") is None
    object.__setattr__(prof, "models_dev_id", "mutated")
    assert one_pass("fx-one") == "mutated"


def test_callers_cannot_poison_the_pass_memo(monkeypatch):
    _isolated_registry(monkeypatch)
    providers.register_provider(ProviderProfile(name="fx-one", models_dev_id="fx-one-dev"))
    with models_dev.profile_ids_scope():
        models_dev._profile_models_dev_ids()["fx-one"] = "poison"
        models_dev.models_dev_provider_map()["fx-one"] = "poison"
        assert models_dev.models_dev_id_for("fx-one") == "fx-one-dev"


def test_a_copied_context_cannot_use_a_closed_profile_pass(monkeypatch):
    """Warm-up workers started inside a pass run in copy_context() and may outlive it."""
    _isolated_registry(monkeypatch)
    providers.register_provider(ProviderProfile(name="fx-one", models_dev_id="fx-one-dev"))
    with models_dev.profile_ids_scope():
        assert models_dev.models_dev_id_for("fx-one") == "fx-one-dev"
        ctx = contextvars.copy_context()
    providers.register_provider(ProviderProfile(name="fx-one", models_dev_id="fx-one-dev-v2"))
    assert ctx.run(models_dev.models_dev_id_for, "fx-one") == "fx-one-dev-v2"


def test_pass_scope_is_released_when_the_pass_raises():
    memo = PassMemo("fx")
    try:
        with memo.scope():
            assert memo.get("k", lambda: 1) == 1
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert memo.get("k", lambda: 2) == 2  # nothing leaked past the failed pass


def test_nested_scopes_share_the_outer_pass():
    memo = PassMemo("fx")
    n = []
    with memo.scope():
        memo.get("k", lambda: n.append(1) or 1)
        with memo.scope():
            memo.get("k", lambda: n.append(1) or 1)
        memo.get("k", lambda: n.append(1) or 1)
    assert n == [1]


def test_build_models_payload_is_the_pass_boundary(monkeypatch):
    import hermes_cli.inventory as inv
    import hermes_cli.model_switch as ms

    seen = []

    def fake_list(**kw):
        seen.append(models_dev._PROFILE_IDS_MEMO._var.get() is not None)
        return []

    monkeypatch.setattr(ms, "list_authenticated_providers", fake_list)
    monkeypatch.setattr(inv, "_local_runtime_row", lambda ctx: None)
    monkeypatch.setattr(inv, "_moa_provider_row", lambda p: None)
    inv.build_models_payload(inv.ConfigContext("", "", "", {}, []))
    assert seen == [True]
    assert models_dev._PROFILE_IDS_MEMO._var.get() is None
