"""Preserve a capable dispatcher's verdict in pool-card wait notices."""
import pytest

from hermes_cli import kanban_dispatch_skip as skips
from hermes_cli import kanban_parent_unclaimed as pu
from hermes_cli import kanban_notice_verdict as nv
from hermes_cli import kanban_llm_pacing_dispatch as pd
from tests.hermes_cli import test_kanban_notice_pacing_admission as helpers

hub = helpers.hub


@pytest.mark.parametrize("path", sorted(helpers.NOTICES))
@pytest.mark.parametrize("governor", [True, False])
def test_recorded_pacing_survives_machine_lane(hub, path, governor):
    tid = helpers._hub_card(hub.conn)
    providers = [{"provider": "anthropic", "state": "paced", "limit": 1, "running": 1}]
    assert skips.record(hub.conn, tid, "llm_paced", detail="anthropic paced", extra={"providers": providers})
    if governor:
        helpers._store(("anthropic", "held", 1_900_000_000))
    place = pu._place(hub.conn, tid)
    assert place["lane"] == "machine" and not place.get("skip")
    payload, text = helpers.NOTICES[path](hub.conn, tid)
    assert payload["admission"]["reason"] == "llm_paced"
    assert payload["admission"]["providers"][0]["state"] == ("held" if governor else "paced")
    assert "admission reason llm_paced: held: LLM quota pacing" in text
    assert "it starts on its own once its provider pool is back on pace" in text
    assert "admission reason unknown" not in text and "re-place" not in text


@pytest.mark.parametrize("path", sorted(helpers.NOTICES))
def test_recorded_quota_wall_survives_machine_lane(hub, path):
    tid = helpers._hub_card(hub.conn)
    assert skips.record(hub.conn, tid, "pinned_quota_wall", detail="anthropic", until=1_900_000_000)
    payload, text = helpers.NOTICES[path](hub.conn, tid)
    assert payload["dispatch_skip"]["reason"] == "pinned_quota_wall"
    assert skips.advice(payload["dispatch_skip"], now=payload["observed_at"]) in text


def test_newest_capable_verdict_wins_even_with_newer_recorder_only_skip(hub):
    tid = helpers._hub_card(hub.conn)
    assert skips.record(hub.conn, tid, "llm_paced", detail="anthropic paced")
    assert skips.record(hub.conn, tid, "pinned_quota_wall", detail="anthropic")
    assert skips.record(hub.conn, tid, "claim_deferred", detail="labels_mismatch")
    helpers._store(("anthropic", "paced"))
    payload, text = helpers._chat_wait(hub.conn, tid)
    assert payload["dispatch_skip"]["reason"] == "pinned_quota_wall"
    assert "LLM quota pacing" not in text


@pytest.mark.parametrize("case", ["existing", "ordering", "recorder", "empty"])
def test_verdict_carriage_boundaries(hub, case):
    tid = helpers._pool_card(hub.conn)
    place = {}
    if case == "existing":
        place["skip"] = {"reason": "owner_down"}
        assert skips.record(hub.conn, tid, "pinned_quota_wall", detail="anthropic")
    elif case == "ordering":
        assert skips.record(hub.conn, tid, "pinned_quota_wall", detail="anthropic")
        assert skips.record(hub.conn, tid, "claim_deferred", detail="capacity")
    elif case == "recorder":
        assert skips.record(hub.conn, tid, "skipped_ineligible", detail="labels_mismatch")
    assert nv.fields(hub.conn, tid, place, 2400, pu._read_pacing) == {}


@pytest.mark.parametrize("precedence", ["busy", "core_update"])
def test_recorded_verdict_keeps_admission_precedence(hub, monkeypatch, precedence):
    tid = helpers._hub_card(hub.conn)
    assert skips.record(hub.conn, tid, "llm_paced", detail="anthropic paced")
    helpers._store(("anthropic", "paced"))
    place = pu._place(hub.conn, tid)
    if precedence == "busy":
        from hermes_cli import kanban_label_backlog as klb
        monkeypatch.setattr(klb, "busy", lambda _backlog: True)
    else:
        place["lane"] = "local"
    monkeypatch.setattr(pu, "_place", lambda *_a: place)
    payload = helpers._post(hub.conn, tid, core_update_hold=precedence == "core_update")
    assert payload["admission"]["reason"] == ("matching_pool_busy" if precedence == "busy" else "core_update_hold")
    assert "dispatch_skip" not in payload


def test_failed_governor_read_keeps_recorded_pacing(hub, monkeypatch):
    tid = helpers._hub_card(hub.conn)
    assert skips.record(hub.conn, tid, "llm_paced", detail="anthropic paced",
                        extra={"providers": [{"provider": "anthropic", "state": "paced"}]})

    def fail(*_args):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(pd, "paced_pools", fail)
    payload, text = helpers._chat_wait(hub.conn, tid)
    assert payload["admission"]["reason"] == "llm_paced"
    assert "it starts on its own once its provider pool is back on pace" in text
