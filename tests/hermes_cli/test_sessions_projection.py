"""Pure projection contracts and legacy annotation ordering."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from hermes_cli import sessions_projection as projection


def test_list_window_stable_sort_offset_pins_and_detachment():
    rows = [dict(id="old-pin", started_at=1, pinned=True, nested={"x": []}),
            dict(id="first", started_at=4), dict(id="tie", started_at=4),
            dict(id="recent", started_at=2, last_active=8)]
    before = deepcopy(rows)
    window = projection.select_list_window(rows, 1, 1, "recent", "include", True)
    assert [r["id"] for r in window] == ["first", "old-pin"]
    window[-1]["nested"]["x"].append(1)
    assert rows == before
    assert [r["id"] for r in projection.select_list_window(rows, 2, 0, "created", "include", True)] == [
        "first", "tie", "old-pin"]


def test_archived_cursor_and_zero_limit_pinned_backfill():
    rows = [dict(id="live", started_at=5, pinned=True),
            dict(id="a", started_at=4, archived=True),
            dict(id="b", started_at=3, archived=True),
            dict(id="pin", started_at=1, archived=True, pinned=True)]
    assert [r["id"] for r in projection.select_list_window(rows, 1, 1, "recent", "only", False)] == [
        "b", "pin", "live"]
    assert [r["id"] for r in projection.select_list_window(rows, 0, 0, "recent", "include", False)] == [
        "live", "pin"]


def test_privacy_stripping_and_full_rows():
    rows = [dict(id="s", system_prompt="private", model_config={"provider": "openai"},
                 model_provider="explicit", nested={"v": []})]
    window = projection.select_sidebar_window(rows, 1)
    assert window == [{"id": "s", "model_provider": "explicit", "nested": {"v": []}}]
    assert projection.select_list_window(rows, 1, 0, "recent", "include", True) == rows
    assert "system_prompt" in rows[0]


def test_assembly_preserves_authoritative_counts_and_copies():
    rows = [{"id": "s", "annotation": {"nested": []}}]
    totals = {"a": 200, "b": 7}
    errors = [{"profile": "missing", "error": "busy"}]
    result = projection.assemble_sessions(rows, totals, 1, 100, errors, {})
    assert result == dict(sessions=rows, total=207, profile_totals=totals,
                          limit=1, offset=100, errors=errors, storage={})
    result["sessions"][0]["annotation"]["nested"].append(1)
    result["errors"][0]["error"] = "changed"
    result["profile_totals"]["a"] = 0
    assert rows[0]["annotation"] == {"nested": []}
    assert errors[0]["error"] == "busy"
    assert totals["a"] == 200


def sidebar(**overrides):
    args = dict(windows={key: [] for key in projection.SIDEBAR_SLICES}, recents_scope="all",
                errors=[], retryable=[], storage={}, scanned=["a"], contributed=set(),
                truncated={"a": True}, usage={"a": {"tokens": 50}}, messaging_total=20)
    args.update(overrides)
    return projection.assemble_sidebar(**args)


@pytest.mark.parametrize("scope", ["all", "a"])
def test_failed_scan_is_not_empty_success(scope):
    errors = [{"profile": "a", "error": "busy"}]
    result = sidebar(windows={}, recents_scope=scope, errors=errors, retryable=errors)
    assert result["recents"] == dict(failed=True, retry=True, errors=errors,
                                     profiles_truncated={}, profiles_usage={})
    assert result["messaging"] == dict(failed=True, retry=True, errors=errors, total=0)
    assert all("sessions" not in result[key] for key in projection.SIDEBAR_SLICES)


def test_partial_failure_corrupt_and_successful_empty():
    errors = [{"profile": "a", "error": "busy"}]
    result = sidebar(errors=errors, retryable=errors, contributed={"b"}, scanned=["a", "b"])
    assert list(result) == ["recents", "cron", "messaging", "errors", "storage", "profiles_failed"]
    assert result["profiles_failed"] == {"a": dict(failed=True, retry=True, error="busy")}
    for key in projection.SIDEBAR_SLICES:
        assert result[key]["sessions"] == []
        assert result[key]["errors"] == errors
        assert result[key]["profiles_failed"] == result["profiles_failed"]
    assert result["messaging"]["total"] == 20
    assert result["recents"]["profiles_usage"] == {"a": {"tokens": 50}}
    assert sidebar()["recents"]["sessions"] == []
    assert sidebar(storage={"a": {"corrupt": True}}, scanned=["a"])["recents"]["sessions"] == []
    assert sidebar(scanned=[])["recents"]["sessions"] == []


def test_stamps_only_new_public_keys_and_fixed_columns():
    rows = [dict(id="s", profile="a", title="private", old_annotation="changed", cwd="/work",
                 new_annotation={"nested": []}, _internal="hidden")]
    before = [{"id", "profile", "title", "old_annotation", "cwd"}]
    result = projection.assemble_stamps(rows, before, "a")
    assert result == {"sessions": [dict(id="s", profile="a", cwd="/work", git_branch=None,
                                        git_repo_root=None, outcome=None, muted=None,
                                        new_annotation={"nested": []})]}
    result["sessions"][0]["new_annotation"]["nested"].append(1)
    assert rows[0]["new_annotation"] == {"nested": []}


@pytest.mark.parametrize("row_count,key_count", [(1, 0), (0, 1), (2, 1), (1, 2)])
def test_stamps_reject_positional_length_mismatch(row_count, key_count):
    rows = [{"id": str(i)} for i in range(row_count)]
    with pytest.raises(ValueError, match="zip"):
        projection.assemble_stamps(rows, [{"id"}] * key_count, "default")


def install_annotations(monkeypatch, routes):
    calls = []
    for name in ("kanban", "goal", "cron", "background", "turn", "subagent", "review"):
        def annotate(rows, name=name, **kwargs):
            calls.append(name)
            if name == "kanban":
                assert kwargs == {"display": True}
            for row in rows:
                row[name + "_stamp"] = True
            return rows
        monkeypatch.setattr(routes, "annotate_review_wait" if name == "review" else
                            "annotate_" + name + "_activity", annotate)
    return calls


def test_legacy_list_uses_projection_and_preserves_annotation_seams(monkeypatch):
    from hermes_cli.web_routers import profiles as routes
    calls = install_annotations(monkeypatch, routes)
    monkeypatch.setattr(routes, "_profile_targets", lambda _: [("a", None)])
    monkeypatch.setattr(routes, "_read_serial_profile_db", lambda *args: (
        [dict(id="s", started_at=1, system_prompt="private")], 12))
    monkeypatch.setattr(routes, "stamp_automated", lambda rows, *args, **kwargs: rows)
    monkeypatch.setattr(routes, "_corrupt_profile_stores", lambda _: {})
    result = routes.get_profiles_sessions(limit=1, offset=0)
    assert calls == ["kanban", "goal", "cron", "background", "turn", "subagent", "review"]
    assert result["total"] == 12
    assert "system_prompt" not in result["sessions"][0]
    assert result["sessions"][0]["review_stamp"] is True


def test_legacy_stamps_annotation_order(monkeypatch):
    from hermes_cli.web_routers import profiles as routes
    calls = install_annotations(monkeypatch, routes)
    monkeypatch.setattr(routes, "_cron_profile_home", lambda _: ("a", None))
    monkeypatch.setattr(routes, "_read_profile_db", lambda *args: [dict(id="s", started_at=1)])
    monkeypatch.setattr(routes, "stamp_automated", lambda rows, *args, **kwargs: rows)
    import hermes_state_outcome
    monkeypatch.setattr(hermes_state_outcome, "stamp_outcome_rows", lambda rows: rows)
    result = routes.get_profiles_session_stamps(ids="s", profile="a")
    assert calls == ["subagent", "kanban", "goal", "cron", "background", "turn", "review"]
    assert result["sessions"][0]["review_stamp"] is True
    assert "started_at" not in result["sessions"][0]


def test_legacy_sidebar_annotation_order_and_totals(monkeypatch, tmp_path):
    from hermes_cli.web_routers import profiles as routes
    calls = install_annotations(monkeypatch, routes)
    monkeypatch.setattr(routes, "_profile_targets", lambda _: [("a", tmp_path)])
    monkeypatch.setattr(routes, "_profile_state_db", lambda _: SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr(routes, "_sidebar_db_fingerprint", lambda _: None)
    monkeypatch.setattr(routes, "subagent_listing_scope", lambda *args, **kwargs: (False, None))
    monkeypatch.setattr(routes, "_sidebar_profile_cache_get", lambda _: {
        **{key: [dict(id=key, started_at=1, system_prompt="private")]
           for key in projection.SIDEBAR_SLICES}, "usage": {"tokens": 9}})
    monkeypatch.setattr(routes, "automated_session_sources", lambda _: set())
    monkeypatch.setattr(routes, "stamp_automated", lambda rows, *args, **kwargs: rows)
    monkeypatch.setattr(routes, "_corrupt_profile_stores", lambda _: {})
    # Bypass response cache only; exercise the real synchronous route body.
    import inspect
    result = inspect.unwrap(routes.get_profiles_sessions_sidebar)()
    assert calls == ["subagent", "kanban", "goal", "cron", "background", "turn", "review"] * 3
    assert result["messaging"]["total"] == 1
    assert result["recents"]["profiles_usage"] == {"a": {"tokens": 9}}
    assert "system_prompt" not in result["recents"]["sessions"][0]
