"""Request-local list batching keeps exact merge inputs and fresh reads."""
import contextlib

from hermes_cli.profile_session_list_slices import read_list_slices
from tests.hermes_cli import test_profiles_sidebar_scope as scope_helpers

profiles_on_disk = scope_helpers.profiles_on_disk
client = scope_helpers.client


def test_exact_slices_share_one_snapshot_and_keep_counts():
    active = []
    calls = []

    @contextlib.contextmanager
    def snapshot():
        active.append(True)
        try:
            yield
        finally:
            active.pop()

    def reader(**kwargs):
        assert active == [True]
        calls.append(kwargs)
        return {"sessions": [], "total": 731, "profile_totals": {"work": 731},
                "errors": [{"profile": "other", "error": "unavailable"}]}

    for revision in range(2):
        result = read_list_slices(reader, snapshot, profile=" all ", recents_limit=50,
                                 cron_limit=50, messaging_limit=100,
                                 recents_exclude="cron", messaging_exclude="tui,cli")
        assert len(calls) == (revision + 1) * 3  # no notification required to reread
        assert result["list_slices_version"] == 1
        for key in ("recents", "cron", "messaging"):
            assert result[key]["total"] == 731
            assert result[key]["profile_totals"] == {"work": 731}
            assert result[key]["errors"]
    assert not active
    common = dict(profile="all", offset=0, min_messages=1, archived="exclude", order="recent")
    assert calls[:3] == [dict(common, limit=50, exclude_sources="cron"),
                         dict(common, limit=50, source="cron"),
                         dict(common, limit=100, exclude_sources="tui,cli")]


def test_http_sidebar_explicit_mode_uses_exact_lists(monkeypatch):
    from hermes_cli.web_routers import profiles

    calls = []

    def read(**kwargs):
        calls.append(kwargs)
        return {"sessions": [], "total": 42, "profile_totals": {"default": 42}}

    monkeypatch.setattr(profiles, "get_profiles_sessions", read)
    monkeypatch.setattr(profiles, "_SIDEBAR_CACHE_TTL_SECONDS", 60)
    result = profiles.get_profiles_sessions_sidebar(list_slices=True)
    assert result["list_slices_version"] == 1
    assert len(calls) == 3
    profiles.get_profiles_sessions_sidebar(list_slices=True)
    assert len(calls) == 6  # ordinary sidebar TTL must not cache exact-list mode
    assert result["recents"]["total"] == 42


def test_real_http_lists_match_and_sql_write_without_notification_is_fresh(client, profiles_on_disk, monkeypatch):
    import sqlite3
    from hermes_cli.web_routers import profiles

    monkeypatch.setattr(profiles, "_SIDEBAR_CACHE_TTL_SECONDS", 60)
    for name, home in profiles_on_disk.items():
        for source in ("cli", "cron", "telegram"):
            scope_helpers._seed_session(home, f"{name}-{source}", source=source)
    query = dict(list_slices="true", recents_profile="all", recents_limit=1,
                 cron_limit=1, messaging_limit=1, recents_exclude="cron,telegram",
                 messaging_exclude="cli,cron")
    response = client.get("/api/profiles/sessions/sidebar", params=query)
    assert response.status_code == 200, response.text
    batch = response.json()
    assert batch["list_slices_version"] == 1
    for key, filters in (("recents", {"exclude_sources": "cron,telegram"}),
                         ("cron", {"source": "cron"}),
                         ("messaging", {"exclude_sources": "cli,cron"})):
        single = client.get("/api/profiles/sessions", params=dict(
            limit=1, offset=0, min_messages=1, archived="exclude", order="recent",
            profile="all", **filters)).json()
        assert batch[key]["total"] == single["total"] == 2
        assert batch[key]["profile_totals"] == single["profile_totals"]
        assert [r["id"] for r in batch[key]["sessions"]] == [r["id"] for r in single["sessions"]]
    # Bypass every notification producer: independent SQLite commit only.
    with sqlite3.connect(profiles_on_disk["worker"] / "state.db") as db:
        db.execute("UPDATE sessions SET source='cron' WHERE id='worker-cli'")
    updated = client.get("/api/profiles/sessions/sidebar", params=query).json()
    assert updated["recents"]["total"] == 1
    assert updated["cron"]["total"] == 3
    assert updated["messaging"]["total"] == 2


def test_real_annotation_reader_is_captured_once_per_batch(monkeypatch):
    from hermes_cli import kanban_session_activity as activity

    captures = []
    original = activity._annotation_snapshot

    def capture(*args, **kwargs):
        captures.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(activity, "_annotation_snapshot", capture)

    def reader(**kwargs):
        rows = [{"id": "batch-proof", "profile": "default"}]
        activity.annotate_kanban_activity(rows, display=True)
        return {"sessions": rows}

    for _ in range(2):
        before = len(captures)
        read_list_slices(reader, activity.one_annotation_snapshot, profile="all",
                         recents_limit=1, cron_limit=1, messaging_limit=1,
                         recents_exclude=None, messaging_exclude=None)
        assert len(captures) - before == 1
