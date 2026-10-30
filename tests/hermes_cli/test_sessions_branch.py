"""`hermes sessions branch <id>`: a one-command copy of a chat for probes. [daveey]

Regression for self-heal agent-93a55211b1: a fix worker proved its fix by running
`hermes chat --resume <David's desktop chat> --oneshot -q "Reply exactly ROUTE_INSTALLED_..."`, because the only way
to copy a chat was the interactive `/branch`. The copy must carry the history, get a new id, stay out of the
user's session lists, and leave the original untouched.
"""
import json
import sys

import pytest


def _run(monkeypatch, capsys, db, *argv):
    import hermes_cli.main as main_mod
    import hermes_state

    real = hermes_state.SessionDB

    class _Same(real):  # keeps class attributes (MAX_TITLE_LENGTH) that SessionDB methods read
        def __new__(cls, *args, **kwargs):
            return db

    monkeypatch.setattr(hermes_state, "SessionDB", _Same)
    monkeypatch.setattr(db, "close", lambda: None)  # cmd_sessions closes its store; the test keeps reading it
    monkeypatch.setattr(sys, "argv", ["hermes", "sessions", *argv])
    code = 0
    try:
        main_mod.main()
    except SystemExit as exc:
        code = exc.code or 0
    return code, capsys.readouterr().out


@pytest.fixture
def db(tmp_path):
    from hermes_state import SessionDB

    d = SessionDB(tmp_path / "state.db")
    d.create_session("20261026_000514_23ef49", source="desktop", system_prompt="SYS")
    d.set_session_title("20261026_000514_23ef49", "add alpha to this slack workspace")
    for role, content in (("user", "start the slack test?"), ("assistant", "Start it, or skip it?"),
                          ("user", "i dont underestand my choice")):
        d.append_message("20261026_000514_23ef49", role=role, content=content)
    yield d


def test_branch_copies_history_to_a_new_hidden_session(monkeypatch, capsys, db):
    before = [m["content"] for m in db.get_messages_as_conversation("20261026_000514_23ef49")]
    code, out = _run(monkeypatch, capsys, db, "branch", "23ef49_doesnotexist")
    assert code == 1 and "not found" in out.lower()

    code, out = _run(monkeypatch, capsys, db, "branch", "20261026_000514_23ef49", "--json")
    assert code == 0, out
    payload = json.loads(out)
    new_id = payload["session_id"]
    assert new_id != "20261026_000514_23ef49"
    assert payload["copied_from"] == "20261026_000514_23ef49"
    assert payload["messages"] == 3
    assert f"hermes sessions delete --yes {new_id}" in payload["cleanup"]

    copy = db.get_session(new_id)
    assert copy["source"] == "oneshot"  # an internal listing source: never in the user's sidebar or /resume picker
    assert copy["parent_session_id"] is None  # not a /branch child of the user's chat (that would list it)
    assert json.loads(copy["model_config"])["_probe_copy_of"] == "20261026_000514_23ef49"
    assert copy["system_prompt"] == "SYS"  # the probe answers with the original chat's prompt
    assert [m["content"] for m in db.get_messages_as_conversation(new_id)] == before
    # the original is untouched: same rows, not ended
    assert [m["content"] for m in db.get_messages_as_conversation("20261026_000514_23ef49")] == before
    assert db.get_session("20261026_000514_23ef49")["ended_at"] is None


def test_branch_plain_output_prints_id_and_accepts_prefix(monkeypatch, capsys, db):
    code, out = _run(monkeypatch, capsys, db, "branch", "20261026_000514_23e", "--title", "probe of 23ef49")
    assert code == 0, out
    new_id = out.strip().splitlines()[0]
    assert db.get_session(new_id)["source"] == "oneshot"
    assert db.get_session_title(new_id) == "probe of 23ef49"
    assert len(db.get_messages_as_conversation(new_id)) == 3


def test_branch_source_override(monkeypatch, capsys, db):
    code, out = _run(monkeypatch, capsys, db, "branch", "20261026_000514_23ef49", "--source", "probe", "--json")
    assert code == 0, out
    assert db.get_session(json.loads(out)["session_id"])["source"] == "probe"
