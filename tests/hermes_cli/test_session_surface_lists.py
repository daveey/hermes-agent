"""Row-aware automation filtering agrees between project rows and paged REST lists."""
import pytest

from tests.hermes_cli import test_sessions_automated_exclude as helpers

homes = helpers.homes
client = helpers.client


@pytest.mark.parametrize("route", ["/api/sessions", "/api/profiles/sessions?profile=default"])
def test_local_provenance_filters_before_paging_and_count(client, homes, route):
    from hermes_state import SessionDB

    home = homes["default"]
    (home / "config.yaml").write_text("sessions:\n  interactive_sources: [desktop]\n")
    for sid, source, created in [("gui", "gui", "desktop"), ("human", "local", "desktop"),
                                  ("bot", "local", "bot_room"), ("eval", "local", "eval"),
                                  ("finite", "oneshot", "oneshot"), ("unset", "local", None)]:
        helpers._seed(home, sid, source)
        db = SessionDB(db_path=home / "state.db")
        try:
            db._execute_write(lambda conn: conn.execute(
                "UPDATE sessions SET created_source=? WHERE id=?", (created, sid)))
        finally:
            db.close()
    sep = "&" if "?" in route else "?"
    base = route + sep + "archived=only&order=recent"
    plain = client.get(base + "&limit=50").json()["sessions"]
    assert {r["id"] for r in plain if not r["automated"]} == {"gui", "human"}
    assert helpers._listed(client.get(base + "&automated=exclude&limit=2")) == (["gui", "human"], 2)
    assert helpers._listed(client.get(base + "&automated=exclude&limit=2&offset=2")) == ([], 2)
    assert helpers._listed(client.get(base + "&automated=exclude&limit=2&exclude_sources=local")) == (["gui"], 1)


@pytest.mark.parametrize("source,created", [("gui", "desktop"), ("local", "desktop")])
def test_project_projection_uses_creation_provenance(source, created):
    from hermes_cli.session_listing import AutomatedSourcePolicy
    from tui_gateway.methods_projects import _project_tree_row

    row = _project_tree_row({"source": source, "created_source": created}, AutomatedSourcePolicy([], ["desktop"]))
    assert row["automated"] is False
    assert row["source"] == source  # routing is not rewritten


@pytest.mark.parametrize("source", [""])
def test_pair_filter_preserves_legacy_source(tmp_path, source):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("legacy", source="cli")
        db._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET source=? WHERE id='legacy'", (source,)))
        scope = {"exclude_source_pairs": [("local", "bot_room")]}
        assert [row["id"] for row in db.list_sessions_rich(**scope)] == ["legacy"]
        assert db.session_count(**scope) == 1
    finally:
        db.close()
