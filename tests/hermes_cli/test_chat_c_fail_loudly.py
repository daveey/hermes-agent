"""Tests for `chat -c <title>` failing loudly (stderr) and `--create-if-missing`.

Regression for #86794: a background/quiet `hermes chat -c "<title>" -q "..."`
against a not-yet-existing titled session silently no-oped — the error message
was written to stdout (which quiet/programmatic callers treat as the "final
response" channel) instead of stderr, and there was no way to create the
titled session from the same invocation.

Two behaviors are fixed here:

1. **fail loudly on stderr** — when no session matches `-c <title>`, the error
   goes to stderr (exit 1), so programmatic callers always see it.
2. **--create-if-missing** — same invocation with the flag creates a fresh
   session carrying the title and proceeds, giving plugins/scripts a
   deterministic "send to this named thread, making it if needed" primitive.
"""

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir so session creation stays isolated."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _create_titled_session(title):
    from hermes_cli.main import _create_titled_session as fn

    return fn(title)


class TestCreateIfMissingFlagParsing:
    def test_flag_parses_on_chat_subparser(self):
        """--create-if-missing is accepted by the real chat parser."""
        from hermes_cli._parser import build_top_level_parser

        parser, _subparsers, _chat_parser = build_top_level_parser()
        args = parser.parse_args(
            ["chat", "-c", "Bot Chat", "--create-if-missing", "-q", "hi"]
        )
        assert args.continue_last == "Bot Chat"
        assert getattr(args, "create_if_missing", False) is True

    def test_flag_absent_means_false(self):
        """Without the flag, create_if_missing stays unset (SUPPRESS default)."""
        from hermes_cli._parser import build_top_level_parser

        parser, _subparsers, _chat_parser = build_top_level_parser()
        args = parser.parse_args(["chat", "-c", "Bot Chat", "-q", "hi"])
        # SUPPRESS default: attribute absent, and cmd_chat's getattr(..., False)
        # must treat it as False.
        assert getattr(args, "create_if_missing", False) is False


class TestCreateTitledSession:
    def test_creates_session_with_title(self, isolated_home):
        sid = _create_titled_session("Bot Chat")
        assert sid, "should return a session id"

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            session = db.get_session(sid)
            assert session is not None
            assert db.get_session_title(sid) == "Bot Chat"
        finally:
            db.close()

    def test_created_session_resolvable_by_title(self, isolated_home):
        """After creation, resolve_session_by_title finds it (idempotent sends)."""
        sid = _create_titled_session("Bot Chat")
        assert sid

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            resolved = db.resolve_session_by_title("Bot Chat")
            assert resolved == sid
        finally:
            db.close()


class TestChatCFailLoudlyOnStderr:
    """Behavior-level: run the real cmd_chat path and inspect channels."""

    def test_missing_session_fails_on_stderr(self, isolated_home, monkeypatch):
        """-c <missing title> → exit 1, message on stderr, stdout untouched."""
        import sys

        import hermes_cli.main as main_mod

        stderr_lines = []

        class _Exit(Exception):
            pass

        def _fake_exit(code):
            raise _Exit(code)

        class _Stderr:
            def write(self, text):
                stderr_lines.append(text)
                return len(text)

        class _Stdout:
            def write(self, text):
                return len(text)

        monkeypatch.setattr(sys, "exit", _fake_exit)
        monkeypatch.setattr(sys, "stderr", _Stderr())
        monkeypatch.setattr(sys, "stdout", _Stdout())

        args = type(
            "Args",
            (),
            {
                "continue_last": "Bot Chat",
                "resume": None,
                "create_if_missing": False,
            },
        )()

        with pytest.raises(_Exit) as ei:
            main_mod._resolve_continue_arg(args, use_tui=False)

        assert ei.value.args[0] == 1
        assert any("No session found matching 'Bot Chat'" in l for l in stderr_lines)
        assert not stderr_lines[0].startswith("Use 'hermes sessions list'")

    def test_create_if_missing_sets_resume(self, isolated_home, monkeypatch):
        """--create-if-missing resolves to a new session id on args.resume."""
        import hermes_cli.main as main_mod

        args = type(
            "Args",
            (),
            {
                "continue_last": "Bot Chat",
                "resume": None,
                "create_if_missing": True,
            },
        )()

        main_mod._resolve_continue_arg(args, use_tui=False)

        assert args.resume, "resume should be set to the new session id"
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            assert db.get_session_title(args.resume) == "Bot Chat"
        finally:
            db.close()


class TestCreateIfMissingSource:
    """[daveey] `chat -c X --create-if-missing --source tool` labels the new row with that
    source (was hardcoded 'cli', so integration threads showed as user chats in the sidebar)."""

    @staticmethod
    def _args(**extra):
        base = {"continue_last": "Mac-ops C1 1.2", "resume": None, "create_if_missing": True}
        base.update(extra)
        return type("Args", (), base)()

    @staticmethod
    def _source(sid):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            return db.get_session(sid)["source"]
        finally:
            db.close()

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
        monkeypatch.delenv("HERMES_SESSION_SOURCE_EXPLICIT", raising=False)

    def test_source_flag_labels_created_row(self, isolated_home):
        import hermes_cli.main as main_mod

        args = self._args(source="tool")
        main_mod._resolve_continue_arg(args, use_tui=False)
        assert self._source(args.resume) == "tool"

    def test_real_parser_source_flag_reaches_created_row(self, isolated_home):
        import hermes_cli.main as main_mod
        from hermes_cli._parser import build_top_level_parser

        parser, _sub, _chat = build_top_level_parser()
        args = parser.parse_args(
            ["chat", "-c", "Mac-ops C1 1.2", "--create-if-missing", "--source", "tool", "-q", "hi"]
        )
        args.resume = getattr(args, "resume", None)
        main_mod._resolve_continue_arg(args, use_tui=False)
        assert self._source(args.resume) == "tool"

    def test_default_stays_cli(self, isolated_home):
        import hermes_cli.main as main_mod

        args = self._args()
        main_mod._resolve_continue_arg(args, use_tui=False)
        assert self._source(args.resume) == "cli"

    def test_inherited_automation_source_is_used(self, isolated_home, monkeypatch):
        import hermes_cli.main as main_mod

        monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")
        args = self._args()
        main_mod._resolve_continue_arg(args, use_tui=False)
        assert self._source(args.resume) == "kanban"

    def test_inherited_ui_source_is_not_used(self, isolated_home, monkeypatch):
        import hermes_cli.main as main_mod

        monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")
        args = self._args()
        main_mod._resolve_continue_arg(args, use_tui=False)
        assert self._source(args.resume) == "cli"

    def test_resume_keeps_tool_source(self, isolated_home):
        """Second send to the same title resumes the row; the agent's own create_session
        upsert (source from env, 'cli' fallback) never overwrites the 'tool' label."""
        import hermes_cli.main as main_mod
        from hermes_state import SessionDB

        first = self._args(source="tool")
        main_mod._resolve_continue_arg(first, use_tui=False)
        second = self._args(source="tool")
        main_mod._resolve_continue_arg(second, use_tui=False)
        assert second.resume == first.resume
        db = SessionDB()
        try:
            db.create_session(first.resume, source="cli")
        finally:
            db.close()
        assert self._source(first.resume) == "tool"
