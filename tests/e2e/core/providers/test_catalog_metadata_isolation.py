"""Public metadata is local; the foreign-provider routing sentinel stays strict."""
from dataclasses import replace

from tests.e2e.core.providers._catalog_helpers import Row, TurnResult, evaluate, write_home


def test_catalog_home_has_offline_public_metadata(tmp_path, monkeypatch):
    home = write_home(tmp_path, {"provider": "zai"})
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    from agent import model_metadata as metadata

    monkeypatch.setattr(metadata, "_model_metadata_cache", {})
    monkeypatch.setattr(metadata, "_model_metadata_cache_time", 0)

    def no_network(*args, **kwargs):
        raise AssertionError("public catalog must be available without network")

    monkeypatch.setattr(metadata.model_metadata_http, "get", no_network)
    model = metadata.fetch_model_metadata()["catalog-model-a"]
    assert model["context_length"] == 128000
    assert model["pricing"] == {}  # unknown prices must not become invented charges


def test_catalog_sentinel_still_rejects_foreign_provider_egress():
    own = Row("own", "chat_completions", "api_key", "OWN_API_KEY", "https://own.example/v1", (), True)
    other = replace(own, name="other", base_url="https://other.example/v1")
    result = TurnResult(own, 0, "", "", [], ["other.example:443"], "own-key", {"own-key"}, None, "", 0)
    assert not evaluate(result, [own, other])["no_egress_to_foreign_provider_hosts"]
