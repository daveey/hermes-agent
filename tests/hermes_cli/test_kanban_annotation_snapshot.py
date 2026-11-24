"""A slow projection must not be repeated within one session annotation."""

from hermes_cli import kanban_session_activity as activity


def test_slow_annotation_uses_one_projection(monkeypatch):
    activity._reset_cache_for_tests()
    clock = [100.0]
    calls = []
    monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(activity, "_activity_fingerprint", lambda **_kw: ())
    monkeypatch.setattr(activity, "_origin_scope", lambda: ("owner", "default"))
    monkeypatch.setattr(activity, "_extra_waiting", dict)
    monkeypatch.setattr(activity, "attention_task_counts", dict)

    def local(active, blocked, unseen):
        active["session"] = 1
        return {("session", "task")}

    def remote():
        calls.append(True)
        clock[0] += activity._CACHE_TTL_S + 1
        return []

    monkeypatch.setattr(activity, "_read_local_pairs_into", local)
    monkeypatch.setattr(activity, "_remote_links", remote)
    try:
        row = activity.annotate_kanban_activity([{"id": "session"}])[0]
        assert row["kanban_active"] == 1
        assert row["kanban_waiting_on"] == ["task"]
        assert len(calls) == 1
    finally:
        activity._reset_cache_for_tests()
