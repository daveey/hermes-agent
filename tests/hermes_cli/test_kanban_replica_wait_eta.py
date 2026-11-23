"""Replica ETA data must reach the actual sidebar projection, not only describe_replica."""
import pytest

from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_external_activity as kea
from hermes_cli.kanban_activity_identity import freeze_annotation, row_ids


def _card(kind="clock", **fields):
    return {"key": "edge/default/default/t_wait", "status": "scheduled", "parked_at": 1793100000,
            "awaiting": {"kind": kind, "why": "window"}, "park": {"wake_at": 1793107200}, **fields}


def test_markers_survive_snapshot_freezing_and_dedupe_lineage():
    markers = kea.replica_markers(_card())
    snapshot = freeze_annotation(({}, {}, {}, {}, {}, {}, {}, {}, {"s1": markers, "s0": markers}, {}, {}))
    markers.clear()  # no mutable payload in the frozen snapshot
    rows = [{"id": "s1", "_lineage_ids": ["s0"]}]
    kea.stamp_replica_counts(rows, snapshot[8], row_ids)
    assert rows[0]["kanban_external"] == 1
    assert len(rows[0]["kanban_external_cards"]) == 1
    assert rows[0]["kanban_external_cards"][0]["eta"]["expected_s"] == 7200


def test_stale_replica_has_no_eta_or_healthy_clock_evidence():
    markers = kea.replica_markers(_card(stale=True))
    rows = [{"id": "s1"}]
    kea.stamp_replica_counts(rows, {"s1": markers}, row_ids)
    assert rows[0]["kanban_external_unknown"] == 1
    assert "kanban_external_cards" not in rows[0]


def test_replica_markers_exclude_other_waits_and_finished_cards():
    assert not kea.replica_markers(_card("card"))
    assert not kea.replica_markers(_card(status="done"))


def test_replica_card_list_is_capped_but_counts_are_not():
    markers = set().union(*(kea.replica_markers(_card(key=f"edge/default/default/t_{n}")) for n in range(12)))
    rows = [{"id": "s1"}]
    kea.stamp_replica_counts(rows, {"s1": markers}, row_ids)
    assert rows[0]["kanban_external"] == 12
    assert len(rows[0]["kanban_external_cards"]) == kea.CARDS_CAP


@pytest.mark.parametrize("kind", ["clock", "ci", "deploy"])
def test_remote_wait_reaches_sidebar_eta(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.setattr(ksa, "_waiting_sources", {})
    ksa._reset_cache_for_tests()
    card = {"key": "edge/default/default/t_wait", "id": "t_wait", "session": "s1",
            "status": "scheduled", "gateway": "edge", "board": "default", "profile": "default",
            "unfinished": True, "stale": False, "parked_at": 1793100000,
            "awaiting": {"kind": kind, "ref": "https://github.com/acme/widgets/actions/runs/1", "why": "window"},
            "park": {"wake_at": 9999999999, "outcome": None}}
    reads = []

    def remote(*args, **kwargs):
        reads.append(True)
        return [card]

    monkeypatch.setattr(ksa, "_remote_cards", remote)
    try:
        row = ksa.annotate_kanban_activity([{"id": "s1"}])[0]
        assert row.get("kanban_external") == 1
        (external,) = row["kanban_external_cards"]
        assert external["eta"]["wait_kind"] == kind
        assert external["eta"]["waited_since"] == 1793100000
        assert external["task_id"] == card["key"]
        if kind == "clock":
            assert external["eta"]["expected_s"] == 9999999999 - 1793100000
            assert external["external"]["adapter"] == "clock"
            assert external["external"]["state"] == "external_running"
            assert not row.get("kanban_external_unknown")
        else:
            assert external["eta"]["expected_s"] is None
            assert external["eta"]["estimate_source"] == "no history yet"
            assert external["external"]["state"] == "external_unknown"
            assert row["kanban_external_unknown"] == 1
        # Projection must use the same read as the parked counts, not refetch a newer snapshot.
        assert len(reads) == 1
    finally:
        ksa._reset_cache_for_tests()
