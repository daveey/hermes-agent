"""Deterministic, reversible post-merge sampling without board authority."""
import hashlib
import random

import pytest

from hermes_cli.kanban_selfwork_risk import requires_full_check
from hermes_cli.kanban_selfwork_sampling import decide, sample_rate
from tests.hermes_cli.test_kanban_selfwork_risk import change


def selection(number=1, rate=0.2, **kwargs):
    return decide(repository="example/hermes", merged_sha=f"{number:040x}",
                  forced=False, config={"kanban": {"self_work": {"sample_rate": rate}}}, **kwargs)


def test_seeded_draw_is_stable_and_rate_is_honoured():
    rng = random.Random(738)
    ids = [rng.getrandbits(160) for _ in range(2000)]
    receipts = [selection(i, seed="fixture") for i in ids]
    assert receipts == [selection(i, seed="fixture") for i in ids]
    assert 300 < sum(item["selected"] for item in receipts) < 500
    for i in ids[:50]:
        original = selection(i)
        assert not selection(i, 0)["selected"]
        assert selection(i, 1)["selected"]
        assert selection(i, 1)["draw"] == original["draw"]
        assert selection(i, original["draw"])["selected"] is False
        assert selection(i, seed="different")["draw"] != original["draw"]


def test_hash_identity_is_repo_case_normalized_and_unambiguous():
    args = dict(merged_sha="a" * 40, forced=False, config={})
    lower = decide(repository="example/hermes", **args)
    assert lower == decide(repository="EXAMPLE/HERMES", **args)
    assert lower["draw"] != decide(repository="other/hermes", **args)["draw"]
    expected = (int.from_bytes(hashlib.sha256(
        ("hermes-post-merge-v1\0example/hermes\0" + "a" * 40).encode()).digest()[:8], "big") >> 11) / (1 << 53)
    assert lower["draw"] == expected


@pytest.mark.parametrize("changes", [None, [], [change("tests/test_auth.py")],
    [change("hermes_cli/kanban_dispatch.py")], [change("hermes_cli/quota_guard.py")],
    [change("tests/test_money.py")], [change(patch="truncated")]])
def test_risky_or_unknown_changes_are_always_selected(changes):
    receipt = decide(repository="example/hermes", merged_sha="a" * 40,
                     forced=requires_full_check(changes),
                     config={"kanban": {"self_work": {"sample_rate": 0}}})
    assert receipt["forced"] and receipt["selected"]


@pytest.mark.parametrize("forced", [None, 0, "false", [], {}])
def test_unknown_classifier_output_cannot_skip(forced):
    assert decide(repository="example/hermes", merged_sha="a" * 40, forced=forced,
                  config={"kanban": {"self_work": {"sample_rate": 0}}})["selected"]


@pytest.mark.parametrize("path", ["tests/test_render.py", "hermes_cli/render.py"])
def test_ordinary_changes_can_be_sampled(path):
    assert requires_full_check([change(path)]) is False


@pytest.mark.parametrize("rate", [True, False, None, "0.2", -0.1, 1.1, float("nan"), float("inf")])
def test_invalid_rate_refuses(rate):
    with pytest.raises(ValueError):
        selection(rate=rate)


@pytest.mark.parametrize("config", [None, [], {"kanban": None}, {"kanban": {"self_work": []}}])
def test_malformed_sections_refuse(config):
    with pytest.raises(ValueError):
        sample_rate(config)


@pytest.mark.parametrize("field,value", [("repository", None), ("repository", "repo"),
    ("repository", "example/repo\n"), ("merged_sha", "a" * 39), ("merged_sha", "A" * 40),
    ("merged_sha", None), ("seed", ""), ("seed", None), ("seed", "a\0b")])
def test_invalid_identity_refuses(field, value):
    args = dict(repository="example/hermes", merged_sha="a" * 40, forced=False, config={})
    args[field] = value
    with pytest.raises(ValueError):
        decide(**args)


def test_effective_config_loader_changes_selection_without_reroll(tmp_path, monkeypatch):
    from hermes_cli.config import load_config
    from hermes_cli.config import save_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = load_config()
    assert sample_rate(config) == 0.2
    config["kanban"]["self_work"]["sample_rate"] = 0
    save_config(config)
    args = dict(repository="example/hermes", merged_sha="a" * 40, forced=False)
    before = decide(config=load_config(), **args)
    config["kanban"]["self_work"]["sample_rate"] = 1
    save_config(config)
    after = decide(config=load_config(), **args)
    assert not before["selected"] and after["selected"]
    assert before["draw"] == after["draw"]
