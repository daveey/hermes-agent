"""A live sole consumer must return undelivered custody within its bound."""
import contextlib

import pytest
from types import SimpleNamespace

from tests.tui_gateway import test_kanban_notice_live_holder as helpers
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway import server

sdb = helpers.sdb
gate = helpers.gate


@pytest.mark.parametrize("entry", ["_notif_poll_kanban", "_notif_poll_kanban_scoped"])
def test_wall_clock_jump_does_not_expire_buffer(monkeypatch, gate, sdb, entry):
    now = _clock(monkeypatch)
    tid = helpers._task()
    helpers._complete(tid)
    session = helpers._session(running=True)
    server._notif_poll_kanban_scoped("ui", session)
    now[0] += 86400
    getattr(server, entry)("ui", session)
    assert helpers._sub(tid)["inflight_from"] is not None
    assert len(session["_kanban_pending"]) == 1
    assert gate["submitted"] == []
    now[1] += kbn.NOTIFY_CLAIM_STALE_S + 1
    getattr(server, entry)("ui", session)
    assert helpers._sub(tid)["inflight_from"] is None
    assert session["_kanban_pending"] == []


def test_sweeper_reclaim_of_buffered_range_is_delivered_once(monkeypatch, gate, sdb):
    now = _clock(monkeypatch)
    tid = helpers._task()
    helpers._complete(tid)
    session = helpers._session(running=True)
    server._notif_poll_kanban_scoped("ui", session)
    original = session["_kanban_pending"][0]
    now[1] += kbn.NOTIFY_CLAIM_STALE_S + 1
    with contextlib.closing(kbc.connect()) as conn:
        assert kbn.recover_orphaned_notify_claims(conn) == 1
    # Reclaimed copies can coexist with an earlier in-memory copy until persistence.
    texts, _claims = server._notif_collect_kanban(session)
    assert len(texts) == 1
    verdict, fresh, _kwargs = server._kb_persist_batch(session, [original, *texts], False)
    assert verdict == "written"
    assert len(fresh) == 1
    assert server._kb_persist_batch(session, [original, *texts], False)[0] == "delivered"



def _clock(monkeypatch):
    import time
    now = [time.time(), time.monotonic()]
    monkeypatch.setattr(kbn, "time", SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[1]))
    monkeypatch.setattr(server, "time", SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[1]))
    return now


def test_sole_live_claimer_recovers_without_new_events(monkeypatch, gate, sdb):
    now = _clock(monkeypatch)
    tid = helpers._task()
    helpers._complete(tid)
    assert helpers._claim(tid)
    assert helpers._sub(tid)["inflight_from"] is not None
    now[0] += kbn.NOTIFY_CLAIM_STALE_S + 1
    now[1] += kbn.NOTIFY_CLAIM_STALE_S + 1
    # Same process, no contender, no new event to prod the cursor.
    with contextlib.closing(kbc.connect()) as conn:
        assert kbn.recover_orphaned_notify_claims(conn, platform="tui", chat_id=helpers.KEY) == 1
    assert helpers._sub(tid)["inflight_from"] is None
    session = helpers._session()
    server._notif_poll_kanban_scoped("ui", session)
    server._notif_poll_kanban_scoped("ui", session)
    assert len(gate["submitted"]) == 1
    assert helpers._sub(tid)["inflight_from"] is None


def test_buffered_notice_returns_claim_while_admission_remains_closed(monkeypatch, gate, sdb, tmp_path):
    now = _clock(monkeypatch)
    tid = helpers._task()
    helpers._complete(tid)
    session = helpers._session(running=True)
    server._notif_poll_kanban_scoped("ui", session)
    assert len(session["_kanban_pending"]) == 1
    assert helpers._sub(tid)["inflight_from"] is not None
    # The real incident's next idle boundary was inside an update-admission hold.
    session["running"] = False
    from hermes_cli import admission_fence
    from hermes_cli.backend_retirement import last_refusal
    session["profile_home"] = str(tmp_path)
    admission_fence.set_hold(tmp_path)
    server._notif_poll_kanban_scoped("ui", session)
    assert last_refusal() == "held"
    assert helpers._sub(tid)["inflight_from"] is not None
    now[0] += kbn.NOTIFY_CLAIM_STALE_S + 1
    now[1] += kbn.NOTIFY_CLAIM_STALE_S + 1
    server._notif_poll_kanban_scoped("ui", session)
    assert gate["submitted"] == []
    assert helpers._sub(tid)["inflight_from"] is None, "admission denial cannot retain custody indefinitely"
    admission_fence.clear_hold(tmp_path)
    server._notif_poll_kanban_scoped("ui", session)
    server._notif_poll_kanban_scoped("ui", session)
    assert len(gate["submitted"]) == 1
    assert helpers._sub(tid)["inflight_from"] is None


@pytest.mark.parametrize("foreign", [False, True])
def test_expiry_never_rewinds_a_fresh_reclaim_of_the_same_range(monkeypatch, gate, sdb, foreign):
    now = _clock(monkeypatch)
    tid = helpers._task()
    helpers._complete(tid)
    session = helpers._session(running=True)
    server._notif_poll_kanban_scoped("ui", session)
    assert len(session["_kanban_pending"]) == 1
    now[1] += kbn.NOTIFY_CLAIM_STALE_S + 1
    now[0] += kbn.NOTIFY_CLAIM_STALE_S + 1
    # The sweeper hands A's range back; B then reclaims the very same range, freshly.
    with contextlib.closing(kbc.connect()) as conn:
        assert kbn.recover_orphaned_notify_claims(conn) == 1
    if foreign:
        helpers._as_other_live_process(monkeypatch)
    assert helpers._claim(tid)
    fresh = helpers._sub(tid)
    assert fresh["inflight_from"] is not None
    # A's buffer is now expired; its release must discard A's stale copy only.
    server._notif_poll_kanban_scoped("ui", session)
    assert session["_kanban_pending"] == [] and session["_kanban_claims"] == []
    after = helpers._sub(tid)
    assert after == fresh, "A's expiry rewound B's fresh claim"


def test_expiry_after_sweeper_already_cleared_is_a_noop(monkeypatch, gate, sdb):
    now = _clock(monkeypatch)
    tid = helpers._task()
    helpers._complete(tid)
    session = helpers._session(running=True)
    server._notif_poll_kanban_scoped("ui", session)
    now[1] += kbn.NOTIFY_CLAIM_STALE_S + 1
    now[0] += kbn.NOTIFY_CLAIM_STALE_S + 1
    with contextlib.closing(kbc.connect()) as conn:
        assert kbn.recover_orphaned_notify_claims(conn) == 1
    cleared = helpers._sub(tid)
    server._notif_poll_kanban_scoped("ui", session)
    assert session["_kanban_pending"] == [] and helpers._sub(tid) == cleared


# --- unit coverage for kanban_claim_custody branches (mutation survivors) ---
from tui_gateway import kanban_claim_custody as custody


def _claim(**kw):
    return {"board": "b", "task_id": "t", "platform": "tui", "chat_id": "c", "thread_id": "",
            "old": 0, "new": 1, "texts": ["x"], **kw}


def _spy(monkeypatch):
    calls = {"expired": [], "release": [], "rewind": [], "remote": []}
    monkeypatch.setattr(kbc, "connect", lambda board=None: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(kbn, "release_expired_notify_claim", lambda conn, **k: calls["expired"].append(k))
    monkeypatch.setattr(kbn, "release_notify_claim", lambda conn, **k: calls["release"].append(k))
    monkeypatch.setattr(kbn, "rewind_notify_cursor", lambda conn, **k: calls["rewind"].append(k))
    from tui_gateway import kanban_remote_notices as krn
    monkeypatch.setattr(krn, "release", lambda r: calls["remote"].append(r))
    return calls


def test_release_expired_boundary_and_stamps_missing_start():
    S = kbn.NOTIFY_CLAIM_STALE_S
    session = {"_kanban_claims": [_claim(claimed_mono=0.0, durable=False, texts=[])]}
    assert not custody.release_expired(session, S - 0.001)
    assert custody.release_expired(session, S)
    assert session["_kanban_claims"] == []
    fresh = {"_kanban_claims": [_claim()]}
    assert not custody.release_expired(fresh, 500.0)
    assert fresh["_kanban_claims"][0]["claimed_mono"] == 500.0  # stamped once, never renewed
    assert not custody.release_expired(fresh, 500.0 + S - 1)
    assert custody.release_expired(fresh, 500.0 + S)


def test_release_expired_never_expires_durable_claim():
    session = {"_kanban_claims": [_claim(claimed_mono=0.0, durable=True)]}
    assert not custody.release_expired(session, kbn.NOTIFY_CLAIM_STALE_S * 10)
    assert session["_kanban_claims"]


def test_expiry_releases_only_nondurable_text_bearing_local_claims_remote_via_remote(monkeypatch):
    calls = _spy(monkeypatch)
    session = {"_kanban_claims": [
        _claim(claimed_mono=0.0, task_id="local"),
        _claim(claimed_mono=0.0, task_id="durable", durable=True),
        _claim(claimed_mono=0.0, task_id="empty", texts=[]),
        _claim(claimed_mono=0.0, task_id="remote", remote={"id": 1}),
    ], "_kanban_pending": ["p"]}
    assert custody.release_expired(session, kbn.NOTIFY_CLAIM_STALE_S)
    assert [k["task_id"] for k in calls["expired"]] == ["local"]
    assert calls["remote"] == [{"id": 1}]
    assert calls["release"] == [] and calls["rewind"] == []
    assert session["_kanban_claims"] == [_claim(claimed_mono=0.0, task_id="durable", durable=True)]
    assert session["_kanban_pending"] == []


def test_fresh_texts_keeps_keyless_text():
    class T(str):
        pass
    keyless = T("plain")
    assert custody.fresh_texts([keyless, keyless], set()) == [keyless, keyless]


def test_poller_stop_rewinds_cursor_then_releases_marker(monkeypatch):
    calls = _spy(monkeypatch)
    session = {"_kanban_claims": [_claim(), _claim(durable=True, task_id="d", texts=[])],
               "_kanban_pending": ["p"]}
    custody.release_claims(session, lambda *a: None, __import__("logging").getLogger("t"))
    assert [(k["claimed_cursor"], k["old_cursor"]) for k in calls["rewind"]] == [(1, 0)]
    assert {k["task_id"] for k in calls["release"]} == {"t", "d"}
    assert session["_kanban_claims"] == [] and session["_kanban_pending"] == []
