"""Tests for the shared session-listing helpers (hermes_cli/session_listing.py)."""

import pytest

from hermes_cli.session_listing import (
    format_gateway_session_listing,
    query_session_listing,
)






class TestQuerySessionListingSearch:
    @pytest.fixture
    def db(self, tmp_path):
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("sess_an94", "telegram", user_id="1", chat_id="2")
        db.set_session_title("sess_an94", "AN-94 Prestige Barrel Build #2")
        db.create_session("sess_winton", "whatsapp", user_id="1", chat_id="2")
        db.set_session_title("sess_winton", "Winton Email Sheet Update #3")
        db.create_session("sess_untitled", "telegram", user_id="1", chat_id="2")
        yield db
        db.close()

    def _ids(self, db, **kw):
        return [r["id"] for r in query_session_listing(db, **kw)]



    def test_source_scoping(self, db):
        assert self._ids(db, source="telegram", search_query="winton") == []
        assert self._ids(db, source="whatsapp", search_query="winton") == ["sess_winton"]


    def test_search_matches_compression_root_title(self, tmp_path):
        """Searching an old (compressed-away) title surfaces the live tip."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=tmp_path / "chain.db")
        db.create_session("root_1", "telegram", user_id="1", chat_id="2")
        db.set_session_title("root_1", "Old Chat")
        db.end_session("root_1", end_reason="compression")
        db.create_session(
            "tip_1", "telegram", user_id="1", chat_id="2", parent_session_id="root_1"
        )
        db.set_session_title("tip_1", "AN-94 Build")
        try:
            for query in ("old chat", "root_1", "an94"):
                rows = query_session_listing(db, source="telegram", search_query=query)
                assert [r["id"] for r in rows] == ["tip_1"], query
        finally:
            db.close()

    def test_plain_listing_still_hides_unnamed(self, db):
        assert self._ids(db, source="telegram") == ["sess_an94"]

    def test_current_session_is_hidden_by_default(self, db):
        rows = query_session_listing(db, source="telegram", current_session_id="sess_an94")
        assert [r["id"] for r in rows] == []

    def test_current_session_can_be_listed_with_marker(self, db):
        rows = query_session_listing(
            db,
            source="telegram",
            current_session_id="sess_an94",
            include_current_session=True,
        )

        assert [r["id"] for r in rows] == ["sess_an94"]
        assert rows[0]["is_current_session"] is True


class TestFormatGatewaySessionListing:


    def test_notice_on_empty_listing(self):
        listing = format_gateway_session_listing([], notice="_scoped_")
        assert "_scoped_" in listing



class TestQuerySessionListingLaneScope:
    @pytest.fixture
    def db(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        lane_key = "agent:main:telegram:dm:lane"
        db.create_session(
            "lane_current", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane",
        )
        db.set_session_title("lane_current", "Current lane")
        db.create_session(
            "lane_named", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane",
        )
        db.set_session_title("lane_named", "Needle lane")
        db.create_session(
            "lane_unnamed", "telegram", session_key=lane_key,
            user_id="lane-user", chat_id="lane",
        )
        for i in range(60):
            db.create_session(
                f"foreign_{i}", "telegram",
                session_key=f"agent:main:telegram:dm:foreign-{i}",
                user_id=f"foreign-user-{i}", chat_id=f"foreign-{i}",
            )
            db.set_session_title(f"foreign_{i}", f"Needle foreign {i}")
        yield db, lane_key
        db.close()

    def test_exact_lane_precedes_limit_and_current_session_exclusion(self, db):
        session_db, lane_key = db

        rows = query_session_listing(
            session_db,
            source="telegram",
            session_key=lane_key,
            current_session_id="lane_current",
            limit=1,
        )

        assert [row["id"] for row in rows] == ["lane_named"]

    def test_exact_lane_preserves_full_and_search_modes(self, db):
        session_db, lane_key = db

        full_rows = query_session_listing(
            session_db,
            source="telegram",
            session_key=lane_key,
            include_unnamed=True,
            limit=10,
        )
        search_rows = query_session_listing(
            session_db,
            source="telegram",
            session_key=lane_key,
            search_query="needle",
            limit=10,
        )

        assert {row["id"] for row in full_rows} == {
            "lane_current", "lane_named", "lane_unnamed",
        }
        assert [row["id"] for row in search_rows] == ["lane_named"]

    def test_omitted_session_key_keeps_source_scope(self, db):
        session_db, _lane_key = db

        rows = query_session_listing(
            session_db,
            source="telegram",
            search_query="needle foreign 59",
            limit=10,
        )

        assert [row["id"] for row in rows] == ["foreign_59"]


class TestAutomatedSources:
    """[daveey] ``sessions.automated_sources`` stamps ``automated`` on listing rows."""

    def _home(self, tmp_path, name, cfg):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(cfg, encoding="utf-8")
        return home

    def _stamp(self, home):
        from hermes_cli.session_listing import stamp_automated
        rows = [{"id": "a", "source": "cli"}, {"id": "b", "source": "slack"}, {"id": "c", "source": None}]
        return {r["id"]: r["automated"] for r in stamp_automated(rows, home)}

    def test_listed_source_is_automated_unlisted_is_not(self, tmp_path):
        home = self._home(tmp_path, "p", "sessions:\n  automated_sources: [slack]\n")
        assert self._stamp(home) == {"a": False, "b": True, "c": False}

    def test_star_marks_every_row(self, tmp_path):
        home = self._home(tmp_path, "p", "sessions:\n  automated_sources: ['*']\n")
        assert self._stamp(home) == {"a": True, "b": True, "c": True}

    def test_unset_marks_nothing(self, tmp_path):
        home = self._home(tmp_path, "p", "{}\n")
        assert self._stamp(home) == {"a": False, "b": False, "c": False}

    def test_each_store_reads_its_own_home(self, tmp_path):
        bot = self._home(tmp_path, "bot", "sessions:\n  automated_sources: ['*']\n")
        human = self._home(tmp_path, "human", "{}\n")
        assert set(self._stamp(bot).values()) == {True}
        assert set(self._stamp(human).values()) == {False}


class TestInteractiveSources:
    """[daveey] ``sessions.interactive_sources`` is an allowlist: unlisted sources are automated."""

    _home = TestAutomatedSources._home

    def _stamp(self, home):
        from hermes_cli.session_listing import stamp_automated
        rows = [{"id": "d", "source": "desktop"}, {"id": "r", "source": "selfheal-replay"},
                {"id": "s", "source": "scratch"}, {"id": "n", "source": None},
                {"id": "D", "source": " Desktop "}]
        return {r["id"]: r["automated"] for r in stamp_automated(rows, home)}

    def test_unlisted_source_is_automated_listed_is_not(self, tmp_path):
        home = self._home(tmp_path, "p", "sessions:\n  interactive_sources: [desktop, cli]\n")
        assert self._stamp(home) == {"d": False, "r": True, "s": True, "n": False, "D": False}

    def test_empty_allowlist_keeps_old_behaviour(self, tmp_path):
        home = self._home(tmp_path, "p", "sessions:\n  interactive_sources: []\n  automated_sources: [scratch]\n")
        assert self._stamp(home) == {"d": False, "r": False, "s": True, "n": False, "D": False}

    def test_automated_sources_still_wins_over_allowlist(self, tmp_path):
        home = self._home(tmp_path, "p", "sessions:\n  interactive_sources: [desktop]\n  automated_sources: [desktop]\n")
        assert self._stamp(home)["d"] is True

    def test_each_store_reads_its_own_home(self, tmp_path):
        strict = self._home(tmp_path, "strict", "sessions:\n  interactive_sources: [desktop]\n")
        loose = self._home(tmp_path, "loose", "{}\n")
        assert self._stamp(strict)["r"] is True
        assert self._stamp(loose)["r"] is False

    def test_policy_is_truthy_with_only_an_allowlist(self, tmp_path):
        from hermes_cli.session_listing import automated_session_sources, is_automated_source
        pol = automated_session_sources(self._home(tmp_path, "p", "sessions:\n  interactive_sources: [desktop]\n"))
        assert pol and list(pol) == [] and pol.interactive == ["desktop"]
        assert is_automated_source("kanban", pol) and not is_automated_source("desktop", pol)
        assert not is_automated_source("kanban", ["slack"])  # plain list callers: unchanged


class TestSidebarProfiles:
    """[daveey] Root-home ``sessions.sidebar_profiles``: unlisted profiles default to automated."""

    def _tree(self, tmp_path, root_cfg, profiles):
        root = tmp_path / ".hermes"
        (root / "profiles").mkdir(parents=True)
        (root / "config.yaml").write_text(root_cfg, encoding="utf-8")
        homes = {"default": root}
        for name, cfg in profiles.items():
            home = root / "profiles" / name
            home.mkdir()
            (home / "config.yaml").write_text(cfg, encoding="utf-8")
            homes[name] = home
        return homes

    def _stamp(self, home):
        from hermes_cli.session_listing import stamp_automated
        rows = [{"id": "c", "source": "cli"}, {"id": "d", "source": "desktop"}]
        return {r["id"]: r["automated"] for r in stamp_automated(rows, home)}

    def test_unlisted_profile_is_automated(self, tmp_path):
        homes = self._tree(tmp_path, "sessions:\n  sidebar_profiles: [default]\n", {"newbot": "{}\n"})
        assert self._stamp(homes["newbot"]) == {"c": True, "d": True}

    def test_listed_profile_is_not(self, tmp_path):
        homes = self._tree(tmp_path, "sessions:\n  sidebar_profiles: [default, Work]\n", {"work": "{}\n"})
        assert self._stamp(homes["default"]) == {"c": False, "d": False}
        assert self._stamp(homes["work"]) == {"c": False, "d": False}

    def test_explicit_profile_setting_wins(self, tmp_path):
        homes = self._tree(tmp_path, "sessions:\n  sidebar_profiles: [default]\n", {
            "allow": "sessions:\n  interactive_sources: [desktop]\n",
            "deny": "sessions:\n  automated_sources: [cli]\n",
            "optout": "sessions:\n  automated_sources: []\n"})
        assert self._stamp(homes["allow"]) == {"c": True, "d": False}
        assert self._stamp(homes["deny"]) == {"c": True, "d": False}
        assert self._stamp(homes["optout"]) == {"c": False, "d": False}

    def test_unset_keeps_old_behaviour(self, tmp_path):
        homes = self._tree(tmp_path, "{}\n", {"newbot": "{}\n"})
        assert self._stamp(homes["newbot"]) == {"c": False, "d": False}
        homes2 = self._tree(tmp_path / "x", "sessions:\n  sidebar_profiles: []\n", {"newbot": "{}\n"})
        assert self._stamp(homes2["newbot"]) == {"c": False, "d": False}

    def test_read_from_root_never_the_profile(self, tmp_path):
        # A profile listing itself in its OWN config does not opt it in; the root decides.
        homes = self._tree(tmp_path, "sessions:\n  sidebar_profiles: [default]\n",
                           {"sneaky": "sessions:\n  sidebar_profiles: [sneaky]\n"})
        assert self._stamp(homes["sneaky"]) == {"c": True, "d": True}
        # ...and a profile-level list is ignored when the root sets none.
        homes2 = self._tree(tmp_path / "y", "{}\n", {"bot": "sessions:\n  sidebar_profiles: [default]\n"})
        assert self._stamp(homes2["bot"]) == {"c": False, "d": False}
