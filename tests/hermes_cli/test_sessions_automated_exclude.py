"""``automated=exclude`` on the session lists: automated rows leave the pages and the total in SQL.

2026-11-17: the Desktop Archived view paged 16,899 archived rows, 15,432 of them bot runs that
its own filter then hid. The list has to be able to leave them out before paging.
"""

import pytest


@pytest.fixture
def homes(tmp_path, monkeypatch, _isolate_hermes_home):
    """A default home (humans + one automated source) and a wholly automated ``bot`` profile."""
    from hermes_cli import profiles
    from hermes_constants import get_hermes_home

    default_home = get_hermes_home()
    profiles_root = default_home / "profiles"
    bot_home = profiles_root / "bot"
    for home in (default_home, bot_home):
        home.mkdir(parents=True, exist_ok=True)
    (default_home / "config.yaml").write_text(
        "sessions:\n  automated_sources: [kanban]\n", encoding="utf-8")
    (bot_home / "config.yaml").write_text(
        "sessions:\n  automated_sources: ['*']\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    return {"default": default_home, "bot": bot_home}


@pytest.fixture
def client(monkeypatch, homes):
    from starlette.testclient import TestClient

    import hermes_state
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", homes["default"] / "state.db")
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def _seed(home, session_id, source, *, archived=True):
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id=session_id, source=source)
        db.append_message(session_id=session_id, role="user", content="hi")
        if archived:
            db.set_session_archived(session_id, True)
    finally:
        db.close()


def _listed(response):
    assert response.status_code == 200, response.text
    body = response.json()
    return sorted(row["id"] for row in body["sessions"]), body["total"]


@pytest.fixture
def seeded(homes):
    _seed(homes["default"], "mine-1", "desktop")
    _seed(homes["default"], "mine-2", "cli")
    _seed(homes["default"], "card-run", "kanban")
    _seed(homes["bot"], "bot-1", "desktop")
    _seed(homes["bot"], "bot-2", "desktop")
    return homes


def test_all_profiles_archived_list_leaves_automated_rows_out_of_rows_and_total(client, seeded):
    route = "/api/profiles/sessions?profile=all&archived=only&limit=50&order=recent"

    assert _listed(client.get(route)) == (["bot-1", "bot-2", "card-run", "mine-1", "mine-2"], 5)

    response = client.get(route + "&automated=exclude")

    assert _listed(response) == (["mine-1", "mine-2"], 2)
    assert response.json()["profile_totals"] == {"default": 2, "bot": 0}


def test_automated_rows_do_not_take_page_positions(client, seeded):
    """The point of filtering in SQL: page 1 of size 2 is two human rows, not whatever is newest."""
    route = "/api/profiles/sessions?profile=all&archived=only&limit=2&order=recent&automated=exclude"

    assert _listed(client.get(route)) == (["mine-1", "mine-2"], 2)


def test_single_store_list_honours_the_same_option(client, seeded):
    assert _listed(client.get("/api/sessions?archived=only&limit=50")) == (["card-run", "mine-1", "mine-2"], 3)
    assert _listed(client.get("/api/sessions?archived=only&limit=50&automated=exclude")) == (["mine-1", "mine-2"], 2)
    # A wholly automated store answers with nothing rather than with its rows.
    assert _listed(client.get("/api/sessions?archived=only&limit=50&profile=bot")) == (["bot-1", "bot-2"], 2)
    assert _listed(client.get("/api/sessions?archived=only&limit=50&profile=bot&automated=exclude")) == ([], 0)


def test_interactive_allowlist_excludes_every_other_source_the_store_holds(client, homes):
    (homes["default"] / "config.yaml").write_text(
        "sessions:\n  interactive_sources: [desktop]\n", encoding="utf-8")
    _seed(homes["default"], "mine-1", "desktop")
    _seed(homes["default"], "probe", "oneshot")
    _seed(homes["default"], "card-run", "kanban")

    for route in ("/api/sessions?archived=only&limit=50", "/api/profiles/sessions?profile=default&archived=only&limit=50"):
        assert _listed(client.get(route + "&automated=exclude")) == (["mine-1"], 1)


def test_caller_exclusions_and_the_option_combine(client, seeded):
    route = "/api/profiles/sessions?profile=all&archived=only&limit=50&automated=exclude&exclude_sources=cli"

    assert _listed(client.get(route)) == (["mine-1"], 1)


def test_live_lists_take_the_option_too(client, homes):
    _seed(homes["default"], "live-mine", "desktop", archived=False)
    _seed(homes["default"], "live-card", "kanban", archived=False)

    assert _listed(client.get("/api/sessions?limit=50&automated=exclude")) == (["live-mine"], 1)


@pytest.mark.parametrize("route", ["/api/sessions", "/api/profiles/sessions"])
def test_unknown_value_is_refused(client, seeded, route):
    assert client.get(f"{route}?automated=maybe").status_code == 400


def test_no_policy_means_nothing_is_excluded(client, homes):
    (homes["default"] / "config.yaml").write_text("{}\n", encoding="utf-8")
    _seed(homes["default"], "mine-1", "desktop")
    _seed(homes["default"], "card-run", "kanban")

    assert _listed(client.get("/api/sessions?archived=only&limit=50&automated=exclude")) == (["card-run", "mine-1"], 2)


def test_a_policy_change_takes_effect_without_a_database_write(client, seeded):
    """The all-profiles scan is cached until state.db changes; the policy is in config.yaml."""
    route = "/api/profiles/sessions?profile=all&archived=only&limit=50&order=recent&automated=exclude"
    assert _listed(client.get(route)) == (["mine-1", "mine-2"], 2)

    for home in seeded.values():
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")

    assert _listed(client.get(route)) == (["bot-1", "bot-2", "card-run", "mine-1", "mine-2"], 5)


def test_a_stored_spelling_in_another_case_is_excluded_like_it_is_stamped(client, homes):
    _seed(homes["default"], "mine-1", "desktop")
    _seed(homes["default"], "card-run", "Kanban")

    plain = client.get("/api/sessions?archived=only&limit=50").json()
    assert {row["id"]: row["automated"] for row in plain["sessions"]} == {"mine-1": False, "card-run": True}
    assert _listed(client.get("/api/sessions?archived=only&limit=50&automated=exclude")) == (["mine-1"], 1)


def test_a_pinned_automated_row_is_left_out_too(client, homes):
    """Stated behaviour, not an accident: the pin back-fill obeys the same filter."""
    from hermes_state import SessionDB

    _seed(homes["default"], "mine-1", "desktop")
    _seed(homes["default"], "card-run", "kanban")
    db = SessionDB(db_path=homes["default"] / "state.db")
    try:
        assert db.set_session_pinned("card-run", True)
    finally:
        db.close()

    assert "card-run" in _listed(client.get("/api/sessions?archived=only&limit=50"))[0]
    assert _listed(client.get("/api/sessions?archived=only&limit=50&automated=exclude")) == (["mine-1"], 1)
