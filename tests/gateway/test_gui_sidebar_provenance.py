"""Exercise authenticated canonical session.create, resume and real compression publication."""
import pytest


@pytest.mark.asyncio
async def test_gui_creation_resume_and_compression_remain_interactive(tmp_path, monkeypatch):
    from gateway import session_policy
    from gateway.session_policy import restore_policy
    from hermes_state_local import local_receipt
    from hermes_cli.session_listing import automated_session_sources, stamp_automated
    from tests.gateway.test_local_create_env_refs import _owner
    from tui_gateway.methods_projects import _project_tree_row

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model: {provider: custom, default: m, base_url: 'http://example.invalid/v1'}\n"
        "platform_toolsets: {desktop: []}\n"
        "sessions: {interactive_sources: [desktop]}\n")
    owner, connection = await _owner(home, monkeypatch)
    monkeypatch.setattr(session_policy, "policy_for_source", lambda runner, source: restore_policy(
        local_receipt(owner.db, source.chat_id)["policy"]))
    try:
        snapshot = await connection.create(None, {
            "request_id": "human-gui", "source": "gui", "cwd": str(home), "toolsets": []})
        sid = snapshot["session_id"]
        assert local_receipt(owner.db, sid)["policy"]["source"] == "gui"
        assert owner.db.get_session(sid)["created_source"] == "desktop"
        policy = automated_session_sources(home)

        def visible(session_id):
            row = owner.db.get_session(session_id)
            assert row["created_source"] == "desktop"
            assert not stamp_automated([row], automated_sources=policy)[0]["automated"]
            assert not _project_tree_row(row, policy)["automated"]

        visible(sid)
        from gateway.session_a2a import storage_source
        refreshed = storage_source(owner.db, owner.sessions[sid].source, sid, "local")
        assert refreshed == "local"
        peer = owner.db.get_session(sid)
        owner.db.record_gateway_session_peer(sid, source=refreshed, **{
            key: peer.get(key) for key in ("session_key", "user_id", "chat_id", "chat_type", "thread_id")})
        assert owner.db.get_session(sid)["source"] == "local"
        visible(sid)
        from gateway.session_local_recovery import restore_local_session
        await connection.resume(restore_local_session(owner, sid), {})
        visible(sid)
        owner.db.append_message(sid, "user", "hello")
        owner.db.publish_compression_child(
            parent_session_id=sid, child_session_id="continuation", source="local",
            messages=[{"role": "user", "content": "summary"}], require_compression_lease=False)
        visible("continuation")
        rows = owner.db.list_sessions_rich(include_archived=True)
        assert any(row.get("_lineage_root_id", row["id"]) == sid or row["id"] == sid for row in rows)
        assert all(not row["automated"] for row in stamp_automated(rows, automated_sources=policy))
    finally:
        await connection.close()
        owner.db.close()
