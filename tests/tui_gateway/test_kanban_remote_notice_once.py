"""One transcript row per (chat, remote Kanban notice), whichever sweeper reaches it (t_01a1db370b459fab).

Hub 2026-11-14 17:30Z, chat 20261112_052539_8d8534: the askeve-reviewer gateway (its own profile home, so its own
claim ledger) swept the default chat's notice, could not resume the chat ("missing") and wrote the passive row; 13 s
later the Desktop backend claimed the same notice in the default ledger, woke a turn, and that turn wrote a second
row with the same notice_id. Its own-claim ack ("in the transcript; acked") was satisfied by the passive row.
"""

import contextlib
import json
import sqlite3
import time

import pytest

from hermes_state import SessionDB
from tui_gateway import kanban_remote_notices as krn
from tui_gateway.kanban_remote_notices import sweep_remote_notices

CHAT = "20261112_052539_8d8534"


def _hub(tmp_path, now):
    task = {"id": "t_dup", "status": "done", "title": "review", "board": "default", "assignee": "default",
            "completed_at": now - 5,
            "completion_notice": {"event_id": 662789, "payload": {"summary": "VERDICT: APPROVE"}},
            "sessions": [{"gw": "macbook", "profile": "default", "session": CHAT, "rel": "shepherd"}]}
    path = tmp_path / "hub.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta(k TEXT, v TEXT)")
        conn.execute("INSERT INTO meta VALUES ('gateway', 'macbook')")
        conn.execute("CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT, seq INTEGER)")
        conn.execute("INSERT INTO replica VALUES (?,?,?,?,1)", ("hub", "tasks", "default/default/t_dup", json.dumps(task)))
    return path


class _Waker:
    """``_RemoteWaker``'s contract: ``started`` admits a turn (its row lands later, on the turn thread), anything
    else writes the passive row through ``persist()``."""

    supports_defer = False

    def __init__(self, verdict, pending=None):
        self.grace_s, self.verdict, self.pending, self.calls = 0.0, verdict, pending, 0

    def deliver(self, tip, text, metadata, task, now, *, persist, started):
        self.calls += 1
        if self.verdict != "started":
            persist()
            return self.verdict
        if self.pending is not None:  # the turn thread writes its row after admission
            self.pending.append((tip, text, metadata))
        started()
        return "started"


def _turn_row(db, tip, text, metadata):
    db.append_message(tip, "user", text, api_content=text + "\n\n[ask-gate] live state",
                      display_kind="kanban_notice", display_metadata={**metadata, "woken": True})


def _rows(db):
    out = {}
    for msg in db.get_messages(CHAT):
        meta = msg.get("display_metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        out.setdefault(meta.get("notice_id"), []).append(msg)
    return out


@contextlib.contextmanager
def _chat(tmp_path):
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            yield (db, sid) if sid == CHAT else (None, None)

        yield db, opener


def _profile_home(root, monkeypatch, name):
    home = root if name == "default" else root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_every_profile_process_on_an_install_shares_one_claim_ledger(tmp_path, monkeypatch):
    """The askeve gateways run as their own profiles; their claims must be the Desktop's claims too."""
    root = tmp_path / ".hermes"
    _profile_home(root, monkeypatch, "default")
    default = krn.ledger_path()
    _profile_home(root, monkeypatch, "askeve-reviewer")
    assert krn.ledger_path() == default


def test_woken_turn_row_is_the_only_row_when_another_profile_sweeps_meanwhile(tmp_path, monkeypatch):
    """Regression (acceptance 1): the Desktop's claim is in flight (turn admitted, row not yet written) when a
    sweeper running as another profile reaches the notice. That sweeper must see the claim and write nothing; the
    claim is then acked by the turn's own row."""
    now = time.time()
    hub = _hub(tmp_path, now)
    root = tmp_path / ".hermes"
    krn._reset_for_tests()
    pending: list = []
    with _chat(tmp_path) as (db, opener):
        _profile_home(root, monkeypatch, "default")
        desktop = _Waker("started", pending)
        sweep_remote_notices(replica_path=hub, gateway="macbook", session_db=opener, now=now, waker=desktop)
        assert desktop.calls == 1 and len(pending) == 1

        _profile_home(root, monkeypatch, "askeve-reviewer")  # another gateway, same install, same chat store
        askeve = _Waker("missing")  # it cannot resume a default-profile chat: it would write the passive row
        sweep_remote_notices(replica_path=hub, gateway="macbook", session_db=opener, now=now + 13, waker=askeve)
        assert askeve.calls == 0
        assert _rows(db) == {}

        _turn_row(db, *pending.pop())  # the woken turn persists its user row
        _profile_home(root, monkeypatch, "default")
        sweep_remote_notices(replica_path=hub, gateway="macbook", session_db=opener, now=now + 14, waker=desktop)
        rows = _rows(db)
        assert len(rows) == 1
        (only,) = rows.values()
        assert len(only) == 1 and only[0]["display_metadata"].get("woken") is True and only[0].get("api_content")
        with sqlite3.connect(krn.ledger_path()) as conn:
            assert conn.execute("SELECT delivered, holder_pid FROM notices").fetchall() == [(krn.DELIVERED, None)]


def test_a_notice_already_in_the_transcript_never_wakes_a_second_turn(tmp_path):
    """Incident order: another ledger's sweeper wrote the passive row first (its wake could not start). A later
    sweep with no claim of its own must ack it from the transcript, not wake a turn that writes it again."""
    now = time.time()
    hub = _hub(tmp_path, now)
    krn._reset_for_tests()
    with _chat(tmp_path) as (db, opener):
        askeve = _Waker("missing")
        sweep_remote_notices(replica_path=hub, gateway="macbook", session_db=opener, ledger=tmp_path / "askeve.db",
                             now=now, waker=askeve)
        assert askeve.calls == 1 and len(_rows(db)) == 1
        krn._reset_for_tests()  # the Desktop is another process: no shared memo
        pending: list = []
        desktop = _Waker("started", pending)
        sweep_remote_notices(replica_path=hub, gateway="macbook", session_db=opener, ledger=tmp_path / "desktop.db",
                             now=now + 13, waker=desktop)
        for args in pending:
            _turn_row(db, *args)
        assert desktop.calls == 0
        (only,) = _rows(db).values()
        assert len(only) == 1


def test_a_wake_that_cannot_start_leaves_exactly_one_passive_row(tmp_path, monkeypatch):
    """Acceptance 2 (guard on current behaviour): no turn starts, so the passive row is the only row, also when a
    second profile's sweeper passes over the chat afterwards."""
    now = time.time()
    hub = _hub(tmp_path, now)
    root = tmp_path / ".hermes"
    krn._reset_for_tests()
    with _chat(tmp_path) as (db, opener):
        for name in ("default", "askeve-reviewer"):
            _profile_home(root, monkeypatch, name)
            sweep_remote_notices(replica_path=hub, gateway="macbook", session_db=opener, now=now,
                                 waker=_Waker("missing"))
        (only,) = _rows(db).values()
        assert len(only) == 1 and not only[0].get("api_content") and "woken" not in only[0]["display_metadata"]


def test_alternation_repair_keeps_one_copy_of_a_notice_and_its_api_content():
    """Acceptance 3: two adjacent user rows of one notice collapse to the woken turn's row, not a concatenation
    that repeats the text and drops the ask-gate api_content (hub rows 1288375/1288376 -> 1288499)."""
    from agent.agent_runtime_helpers import repair_message_sequence

    meta = {"notice_id": "remote:worker-6:default/default/t_dup:662789:default:" + CHAT}
    text = "✔ [default] @default Kanban t_dup done — review"
    passive = {"role": "user", "content": text, "display_kind": "kanban_notice", "display_metadata": dict(meta),
               "_row_id": 1288375}
    woken = {"role": "user", "content": text, "api_content": text + "\n\n[ask-gate] live state",
             "display_kind": "kanban_notice", "display_metadata": {**meta, "woken": True}, "_row_id": 1288376}
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}, passive, woken]
    assert repair_message_sequence(None, messages) == 1
    assert len(messages) == 3
    kept = messages[-1]
    assert kept["content"] == text and kept["api_content"].endswith("[ask-gate] live state")
    assert kept["_row_id"] == 1288376 and 1288375 in kept.get("_absorbed_row_ids", [])

    # Neither row has api_content: the first is kept, still one copy.
    a, b = dict(passive), {**passive, "_row_id": 7}
    messages = [{"role": "assistant", "content": "ok"}, a, b]
    assert repair_message_sequence(None, messages) == 1
    assert messages[-1]["content"] == text and messages[-1]["_row_id"] == 1288375

    # Different notices still merge as before (no user text lost).
    other = {**woken, "display_metadata": {"notice_id": "remote:other"}}
    messages = [{"role": "assistant", "content": "ok"}, dict(passive), other]
    repair_message_sequence(None, messages)
    assert messages[-1]["content"].count(text) == 2


@pytest.mark.parametrize("woken_first", [False, True])
def test_notice_dedupe_never_discards_a_previously_folded_other_notice(woken_first):
    from agent.agent_runtime_helpers import repair_message_sequence

    a = {"role": "user", "content": "notice A", "display_kind": "kanban_notice",
         "display_metadata": {"notice_id": "A"}, "_row_id": 10}
    b = {"role": "user", "content": "notice B", "display_kind": "kanban_notice",
         "display_metadata": {"notice_id": "B"}, "_row_id": 11}
    woken = {**a, "_row_id": 12, "api_content": "notice A\n[ask-gate] live state"}
    messages = [{"role": "assistant", "content": "ok"},
                *([woken, b, a] if woken_first else [a, b, woken])]
    repair_message_sequence(None, messages)
    assert "notice B" in messages[-1]["content"]
    assert "notice A" in messages[-1]["content"]
    assert {10, 11, 12} <= {messages[-1]["_row_id"], *messages[-1].get("_absorbed_row_ids", [])}


@pytest.mark.parametrize("marker", ["_merged_turn_prefix", "_merged_durable_rows", "_absorbed_row_ids"])
def test_notice_dedupe_refuses_premerged_rows(marker):
    from agent.agent_runtime_helpers_user_rows import keep_one_notice_row
    from agent.conversation_compression_archive import MERGED_DURABLE_ROWS
    from agent.message_metadata import MERGED_TURN_PREFIX

    key = {"_merged_turn_prefix": MERGED_TURN_PREFIX, "_merged_durable_rows": MERGED_DURABLE_ROWS,
           "_absorbed_row_ids": "_absorbed_row_ids"}[marker]
    passive = {"role": "user", "content": "A and other input", "display_kind": "kanban_notice",
               "display_metadata": {"notice_id": "A"}, key: [1] if marker == "_absorbed_row_ids" else 1}
    woken = {"role": "user", "content": "A", "api_content": "A live state",
             "display_kind": "kanban_notice", "display_metadata": {"notice_id": "A"}}
    assert keep_one_notice_row(passive, woken) is None
    assert keep_one_notice_row(woken, passive) is None
