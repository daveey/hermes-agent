"""Another gateway's Desktop ``local-<sha256>`` chat owns the cards its own gateway links to it. [daveey]

Hub evidence 2026-11-12: MacBook Desktop chat ``local-d98f25de…`` adopted t_01a1c7c4e00f5d69 through the courier,
but its worker-created child t_01a1cc0b3e8bee1c resolved only to a hub chat: on the hub every foreign local hash
was a machine session (``kanban_session_provenance.machine_sessions``), whatever this board said about it.

Proof is an exact gateway/profile/session unreleased ``created`` link on the card or an ancestor. Only the chat's
gateway can read its state.db, and its reconcile writes that link for an origin card only for an interactive chat
(``kanban_chat_links._ledger_links``) and releases links to its machine sessions. A courier binding or ``reason:
origin`` sub alone proves nothing: ``hermes-hub create-task``/``adopt-task`` default the origin to the caller's
``$HERMES_SESSION_ID``, a worker's own hash inside a Kanban worker (review t_01a1cf8c7b6cae6f). Hub 2026-11-12:
17 MacBook local-hash refs hold a courier binding, 4 of them a MacBook-written ``created`` link.
"""
import json
import time

import pytest

from hermes_cli import kanban_creator_chats as creator_chats
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli.kanban_session_provenance import proven_foreign_chats
from tests.tools.test_kanban_worker_created_chats import CHAT, WORKER, make_home

MAC = "local-" + "9c" * 32
REF = f"macbook/default/{MAC}"
ORIGIN = {"gateway": "macbook", "profile": "default", "session": MAC}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.kanban_origin.gateway_names", lambda: ("hub", {"hub", "macbook", "mini"}))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    notify._source_cache.clear()
    return home


def _intent(request_id, origin=ORIGIN, task_id=None):
    owner = {"gateway": "hub", "profile": "default", "board": "default"}
    return {"request_id": request_id, "origin": origin,
            "owner": {**owner, "task_id": task_id} if task_id else owner}


def _bound(conn, how, origin=ORIGIN):
    """A card the courier bound to ``origin`` (creation or adoption of an exact existing card)."""
    if how == "create":
        # A foreign local-<hash> origin is not renewable authority by itself (nothing proves it yet), so the
        # courier create is anchored by an authentic SessionDB desktop chat (CHAT, registered by make_home).
        # That anchor is then removed: these tests probe what the resolver proves for the foreign ref alone.
        tid = kb.create_task(conn, title="mac card", session_id=CHAT, origin_intent=_intent("req-create-0001", origin))
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_chat_links WHERE task_id=? AND chat_session=?", (tid, CHAT))
            conn.execute("DELETE FROM kanban_notify_subs WHERE task_id=? AND chat_id=?", (tid, CHAT))
            conn.execute("UPDATE tasks SET session_id=NULL WHERE id=?", (tid,))
        return tid
    from hermes_cli.kanban_origin_creation import adopt_with_origin
    tid = kb.create_task(conn, title="mac card")
    return adopt_with_origin(conn, _intent("req-adopt-00001", origin, tid), board="default")


def _raw_sub(conn, task_id, chat_id, reason=None, created_at=None):
    conn.execute(
        "INSERT INTO kanban_notify_subs (task_id,platform,chat_id,thread_id,delivery_mode,delivery_metadata,"
        "created_at,last_event_id) VALUES (?, 'hub', ?, '', 'notify', ?, ?, 0)",
        (task_id, chat_id, json.dumps({"reason": reason}) if reason else None,
         int(created_at if created_at is not None else time.time())))


def _raw_link(conn, task_id, gateway, profile, session, kind="created", released=0):
    from hermes_cli.kanban_chat_links import link_uid
    conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,"
                 "created_at) VALUES (?,?,?,?,?,?,?,1)",
                 (link_uid(task_id, gateway, profile, session, kind), task_id, gateway, profile, session, kind,
                  released))


def _mac_link(conn, task_id, origin=ORIGIN):
    """The MacBook reconcile's synced ``created`` link for its interactive chat's origin card (``_ledger_links``)."""
    _raw_link(conn, task_id, origin["gateway"], origin["profile"], origin["session"])


def _owners(conn, task_id):
    return sorted(f"{o['chat_gateway']}/{o['chat_profile']}/{o['chat_session']}"
                  for o in creator_chats.resolve_chats(conn, task_id))


def _links(conn, task_id):
    return sorted(f"{r[0]}/{r[1]}/{r[2]}" for r in conn.execute(
        "SELECT chat_gateway, chat_profile, chat_session FROM task_chat_links "
        "WHERE task_id=? AND kind='created' AND released=0", (task_id,)))


def _hub_subs(conn, task_id):
    return sorted(s["chat_id"] for s in notify.list_notify_subs(conn, task_id) if s["platform"] == "hub")


# (a) a worker-created child of a courier-bound card belongs to the MacBook Desktop chat.
@pytest.mark.parametrize("how", ["create", "adopt"])
def test_worker_created_child_of_bound_card_gets_foreign_desktop_owner(home, how):
    with kbc.connect_closing() as conn:
        parent = _bound(conn, how)
        _mac_link(conn, parent)
        assert _owners(conn, parent) == [REF]
        child = kb.create_task(conn, title="worker child", creator_task_id=parent)
        assert _links(conn, child) == [REF]
        assert _hub_subs(conn, child) == [REF]
        grandchild = kb.create_task(conn, title="worker grandchild", creator_task_id=child)
        assert _links(conn, grandchild) == [REF]


# (b) a dependency child inherits the parent's stamped ``reason: origin`` hub sub.
def test_dependency_child_inherits_origin_sub_of_bound_card(home):
    with kbc.connect_closing() as conn:
        parent = _bound(conn, "create")
        _mac_link(conn, parent)
        assert notify.stamp_legacy_hub_sub_provenance(conn) == 1
        child = kb.create_task(conn, title="dep child", parents=[parent])
        subs = [s for s in notify.list_notify_subs(conn, child) if s["platform"] == "hub"]
        meta = [s["delivery_metadata"] for s in subs]
        meta = [m if isinstance(m, dict) else json.loads(m) for m in meta]
        assert [(s["chat_id"], m["reason"]) for s, m in zip(subs, meta)] == [(REF, "origin")]


def test_created_link_without_binding_also_proves(home):
    """A worker replica has no courier table and no subs, only the synced ``created`` link."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="plain")
        _mac_link(conn, parent)
        assert proven_foreign_chats(conn, [parent]) == {REF}
        child = kb.create_task(conn, title="worker child", creator_task_id=parent)
        assert _links(conn, child) == [REF]
        dep = kb.create_task(conn, title="dep child", parents=[parent])
        assert _hub_subs(conn, dep) == []  # the link names no hub sub of the parent to inherit


def test_proven_hub_sub_is_accepted_and_unproven_refused(home):
    with kbc.connect_closing() as conn:
        parent = _bound(conn, "create")
        _mac_link(conn, parent)
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id=?", (parent,))
        child = kb.create_task(conn, title="dep", parents=[parent])
        other = kb.create_task(conn, title="unrelated")
        assert _hub_subs(conn, parent) == _hub_subs(conn, child) == []
        for tid in (parent, child, other):
            notify.add_notify_sub(conn, task_id=tid, platform="hub", chat_id=REF,
                                  delivery_metadata={"reason": "shepherd"})
        # Proof names a hub destination; a qualified ref is never a local ``tui`` chat.
        notify.add_notify_sub(conn, task_id=child, platform="tui", chat_id=REF)
        assert _hub_subs(conn, parent) == _hub_subs(conn, child) == [REF]
        assert _hub_subs(conn, other) == []
        assert [s for s in notify.list_notify_subs(conn, child) if s["platform"] == "tui"] == []


@pytest.mark.parametrize("edge", ["creator", "parent"])
def test_explicit_foreign_origin_with_ancestor_proof_owns_child(home, edge):
    with kbc.connect_closing() as conn:
        parent = _bound(conn, "create")
        _mac_link(conn, parent)
        child = kb.create_task(conn, title="explicit", chat_origin=f"@session:default@macbook/{MAC}",
                               creator_task_id=parent if edge == "creator" else None,
                               parents=[parent] if edge == "parent" else ())
        assert any(e["kind"] == "creator_chats_resolved"
                   and json.loads(e["payload"]).get("explicit_origin") == f"@session:default@macbook/{MAC}"
                   for e in conn.execute("SELECT kind, payload FROM task_events WHERE task_id=?", (child,)))
        assert _links(conn, child) == [REF]
        stray = kb.create_task(conn, title="no proof", chat_origin=f"@session:default@macbook/{MAC}")
        assert _links(conn, stray) == []


# (c) a courier binding or origin sub alone, observer rows, a released link or a bare sub prove nothing.
@pytest.mark.parametrize("how", ["create", "adopt"])
def test_worker_receipt_bound_by_courier_owns_nothing(home, how):
    """Review t_01a1cf8c7b6cae6f: ``hermes-hub create-task`` in a MacBook worker shell binds the worker's own hash.
    The MacBook never writes a ``created`` link for it, so the binding and its (stamped) origin sub stay unproven."""
    with kbc.connect_closing() as conn:
        parent = _bound(conn, how)
        notify.stamp_legacy_hub_sub_provenance(conn)
        assert _hub_subs(conn, parent) == [REF]  # the courier's own sub, unchanged
        assert proven_foreign_chats(conn, [parent]) == frozenset()
        assert _owners(conn, parent) == []
        child = kb.create_task(conn, title="worker child", creator_task_id=parent)
        dep = kb.create_task(conn, title="dep child", parents=[parent])
        assert _links(conn, child) == [] and _hub_subs(conn, child) == []
        assert _links(conn, dep) == [] and _hub_subs(conn, dep) == []
        notify.add_notify_sub(conn, task_id=dep, platform="hub", chat_id=REF)
        assert _hub_subs(conn, dep) == []


def test_released_links_stop_owning_new_descendants(home):
    """The chat gateway releases its links to a machine session (``_owner_written_releases``); later descendants
    then get nothing, though the courier binding and origin sub on the root are never released."""
    with kbc.connect_closing() as conn:
        parent = _bound(conn, "create")
        _mac_link(conn, parent)
        notify.stamp_legacy_hub_sub_provenance(conn)
        child = kb.create_task(conn, title="worker child", creator_task_id=parent)
        assert _links(conn, child) == [REF]
        conn.execute("UPDATE task_chat_links SET released=1 WHERE chat_session=?", (MAC,))
        grandchild = kb.create_task(conn, title="grandchild", creator_task_id=child)
        dep = kb.create_task(conn, title="dep grandchild", parents=[child])
        dep_root = kb.create_task(conn, title="dep child of root", parents=[parent])
        assert _links(conn, grandchild) == [] and _hub_subs(conn, grandchild) == []
        assert _hub_subs(conn, dep) == [] and _hub_subs(conn, dep_root) == []


_UNPROVEN_ROWS = {
    "shepherd_sub": lambda conn, tid, at: _raw_sub(conn, tid, REF, "shepherd"),
    "touch_sub": lambda conn, tid, at: _raw_sub(conn, tid, REF, "touch"),
    "origin_sub": lambda conn, tid, at: _raw_sub(conn, tid, REF, "origin"),
    "shepherd_link": lambda conn, tid, at: _raw_link(conn, tid, "macbook", "default", MAC, kind="shepherd"),
    "released_link": lambda conn, tid, at: _raw_link(conn, tid, "macbook", "default", MAC, released=1),
    # The courier's sub shape, written at creation, with no binding behind it.
    "unstamped_sub": lambda conn, tid, at: _raw_sub(conn, tid, REF, created_at=at),
    "nothing": lambda conn, tid, at: None,
}


@pytest.mark.parametrize("rows", sorted(_UNPROVEN_ROWS))
def test_foreign_hash_without_owning_proof_stays_excluded(home, rows):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="plain")
        created = conn.execute("SELECT created_at FROM tasks WHERE id=?", (parent,)).fetchone()[0]
        _UNPROVEN_ROWS[rows](conn, parent, created)
        assert proven_foreign_chats(conn, [parent]) == frozenset()
        assert _owners(conn, parent) == []
        child = kb.create_task(conn, title="worker child", creator_task_id=parent)
        dep = kb.create_task(conn, title="dep child", parents=[parent])
        assert _links(conn, child) == [] and _hub_subs(conn, child) == []
        assert _links(conn, dep) == [] and _hub_subs(conn, dep) == []


# (d) a ref whose profile or gateway differs from the proven link is not proven by it.
@pytest.mark.parametrize("other", [f"macbook/research/{MAC}", f"mini/default/{MAC}",
                                   f"macbook/default/local-{'ee' * 32}"])
def test_link_proves_only_its_exact_ref(home, other):
    with kbc.connect_closing() as conn:
        parent = _bound(conn, "create")
        _mac_link(conn, parent)
        _raw_sub(conn, parent, other, "origin")
        gw, prof, sid = other.split("/")
        _raw_link(conn, parent, gw, prof, sid, kind="shepherd")
        assert proven_foreign_chats(conn, [parent]) == {REF}
        assert _owners(conn, parent) == [REF]
        child = kb.create_task(conn, title="worker child", creator_task_id=parent)
        assert _links(conn, child) == [REF] and _hub_subs(conn, child) == [REF]
        dep = kb.create_task(conn, title="dep child", parents=[parent])
        assert other not in _hub_subs(conn, dep)


def test_link_for_another_profile_does_not_prove_default(home):
    with kbc.connect_closing() as conn:
        research = {**ORIGIN, "profile": "research"}
        parent = _bound(conn, "create", origin=research)
        _mac_link(conn, parent, research)
        _raw_sub(conn, parent, REF, "origin")
        assert proven_foreign_chats(conn, [parent]) == {f"macbook/research/{MAC}"}
        child = kb.create_task(conn, title="worker child", creator_task_id=parent)
        assert REF not in _links(conn, child) and REF not in _hub_subs(conn, child)


def test_bindings_subs_and_unqualified_link_prove_nothing(home):
    with kbc.connect_closing() as conn:
        parent = _bound(conn, "create")
        other = kb.create_task(conn, title="other")
        # The courier binding itself, a link with an invalid empty profile (NULL means default),
        # origin subs (qualified, ``<gw>:<id>``, malformed) and a ``tui`` row.
        _raw_link(conn, other, "macbook", "", MAC)
        _raw_sub(conn, other, REF, "origin")
        _raw_sub(conn, other, f"macbook:{MAC}", "origin")
        _raw_sub(conn, other, f"macbook/default/x/{MAC}", "origin")
        conn.execute("INSERT INTO kanban_notify_subs (task_id,platform,chat_id,thread_id,delivery_metadata,"
                     "created_at,last_event_id) VALUES (?, 'tui', ?, '', ?, 1, 0)",
                     (other, REF, json.dumps({"reason": "origin"})))
        assert proven_foreign_chats(conn, [parent, other]) == frozenset()


@pytest.mark.parametrize("gateway,profile,expected", [
    ("macbook", None, {REF}),
    ("", "default", frozenset()),
])
def test_foreign_proof_normalizes_only_legacy_profile(home, gateway, profile, expected):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="legacy proof")
        _raw_link(conn, parent, gateway, profile, MAC)
        assert proven_foreign_chats(conn, [parent]) == expected


def test_foreign_non_hash_rows_are_not_lifted(home):
    """Proof lifts only the unknown-hash suppression: a foreign ``cron_`` id stays a machine session."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="plain")
        _raw_link(conn, parent, "macbook", "default", "cron_abc_123")
        assert proven_foreign_chats(conn, [parent]) == frozenset()
        assert _owners(conn, parent) == []


# (e) this gateway's own local hashes keep the state.db verdict, whatever the board says.
@pytest.mark.parametrize("session", [WORKER, "local-" + "77" * 32])
def test_local_machine_hash_stays_excluded_despite_board_rows(home, session):
    with kbc.connect_closing() as conn:
        parent = _bound(conn, "create", origin={"gateway": "hub", "profile": "default", "session": session})
        conn.execute("UPDATE kanban_notify_subs SET delivery_metadata=? WHERE task_id=? AND chat_id=?",
                     (json.dumps({"reason": "origin"}), parent, f"hub/default/{session}"))
        _raw_link(conn, parent, "hub", "default", session)
        assert proven_foreign_chats(conn, [parent]) == frozenset()
        assert notify.is_machine_session("hub", session, local_gateway="hub", profile="default")
        assert _owners(conn, parent) == []
        child = kb.create_task(conn, title="worker child", creator_task_id=parent)
        dep = kb.create_task(conn, title="dep child", parents=[parent])
        assert _links(conn, child) == [] and _hub_subs(conn, dep) == []


def test_unknown_local_gateway_proves_nothing(home, monkeypatch):
    monkeypatch.setattr("hermes_cli.kanban_origin.gateway_names", lambda: ("", set()))
    with kbc.connect_closing() as conn:
        parent = _bound(conn, "create")
        _mac_link(conn, parent)
        assert proven_foreign_chats(conn, [parent]) == frozenset()


def test_chat_gateway_releases_hub_written_link_to_its_worker_hash(home, monkeypatch):
    """The real release path (review t_01a1cf9aab0349a9): the hub wrote a ``created`` link to a MacBook
    ``local-<sha256>`` that the MacBook's state.db knows is a Kanban worker (here a seed from an older core).
    The MacBook's ``reconcile`` writes it back released, keeps its Desktop chat's link, and stays put on the next
    pass; after the sync the hub no longer proves the worker hash, so a later descendant is not linked to it."""
    from hermes_cli import kanban_chat_links as links
    from hermes_state import SessionDB
    worker = "local-" + "ab" * 32
    db = SessionDB(db_path=home / "state.db")
    db.create_session(worker, source="kanban")
    db.create_session(MAC, source="desktop")
    db._conn.execute("UPDATE sessions SET source='local', created_source='desktop' WHERE id=?", (MAC,))
    db._conn.commit()
    db.close()
    shadow = home / "kanban" / "sync-shadow" / "default.db"
    shadow.parent.mkdir(parents=True, exist_ok=True)
    kbc.init_db(shadow)
    with kbc.connect_closing(shadow) as rep:
        card = kb.create_task(rep, title="hub card")
        human = kb.create_task(rep, title="hub card of the Desktop chat")
        rep.execute("UPDATE tasks SET created_on='hub', status='running' WHERE id IN (?, ?)", (card, human))
        _raw_link(rep, card, "macbook", "default", worker)
        _mac_link(rep, human)
        rep.commit()
    uid = links.link_uid(card, "macbook", "default", worker, "created")
    monkeypatch.setattr("hermes_cli.kanban_origin.gateway_names", lambda: ("macbook", {"hub", "macbook", "mini"}))
    notify._source_cache.clear()
    with kbc.connect_closing() as conn:
        links.reconcile(conn, "default", "macbook", ledger=False)
        assert tuple(conn.execute("SELECT released FROM task_chat_links WHERE uid=?", (uid,)).fetchone() or ()) == (1,)
        assert conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='task_chat_links' AND row_uid=?",
                            (uid,)).fetchone()
        human_uid = links.link_uid(human, "macbook", "default", MAC, "created")
        assert conn.execute("SELECT 1 FROM task_chat_links WHERE uid=?", (human_uid,)).fetchone() is None
    with kbc.connect_closing(shadow) as rep:  # the Board's copy after the sync: same uid, released
        rep.execute("UPDATE task_chat_links SET released=1 WHERE uid=?", (uid,))
        rep.commit()
    with kbc.connect_closing() as conn:  # the next pass neither re-releases nor drops the release record
        assert links.reconcile(conn, "default", "macbook", ledger=False) == {
            "added": 0, "changed": 0, "released": 0, "retired": 0}
        assert tuple(conn.execute("SELECT released FROM task_chat_links WHERE uid=?", (uid,)).fetchone() or ()) == (1,)
    monkeypatch.setattr("hermes_cli.kanban_origin.gateway_names", lambda: ("hub", {"hub", "macbook", "mini"}))
    with kbc.connect_closing(shadow) as rep:
        assert proven_foreign_chats(rep, [card]) == frozenset()
        assert proven_foreign_chats(rep, [human]) == {REF}
        grandchild = kb.create_task(rep, title="grandchild", creator_task_id=card)
        dep = kb.create_task(rep, title="dep child", parents=[card])
        assert _links(rep, grandchild) == [] and _hub_subs(rep, dep) == []
        assert _links(rep, kb.create_task(rep, title="desktop child", creator_task_id=human)) == [REF]
