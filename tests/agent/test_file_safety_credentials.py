"""Tests for HERMES_HOME credential-file read blocking in file_safety.

Regression for https://github.com/NousResearch/hermes-agent/issues/17656 —
``read_file`` was previously only sandboxed against ``HERMES_HOME`` itself,
which left ``auth.json`` and ``.anthropic_oauth.json`` (plaintext provider
keys + OAuth tokens) readable by the agent. A prompt-injection reaching
``read_file`` could exfiltrate active credentials.

These tests verify that ``get_read_block_error`` returns a denial message
for the credential stores while leaving arbitrary ``HERMES_HOME`` files
readable, and that the existing ``skills/.hub`` deny still applies.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Point ``_hermes_home_path()`` at a tmp dir for isolated checks."""
    import agent.file_safety as fs

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: home)
    return home


def _create(home: Path, rel: str | Path) -> Path:
    """Create the file (with parents) so realpath() resolves it."""
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("dummy", encoding="utf-8")
    return p










def test_arbitrary_hermes_home_file_not_blocked(fake_home):
    """Non-credential files inside HERMES_HOME stay readable."""
    from agent.file_safety import get_read_block_error

    safe = _create(fake_home, "session_log.txt")
    assert get_read_block_error(str(safe)) is None


def test_subdirectory_named_auth_json_not_blocked(fake_home):
    """Only the top-level auth.json is the credential store; a file with the
    same name in a subdirectory (e.g., a skill mock) must remain readable."""
    from agent.file_safety import get_read_block_error

    nested = _create(fake_home, Path("skills") / "my-skill" / "auth.json")
    assert get_read_block_error(str(nested)) is None












def test_search_tool_blocks_direct_auth_json_path(fake_home, monkeypatch):
    """Searching a credential file directly must not invoke the search backend."""
    import json

    import tools.file_tools as ft
    from tools import terminal_tool

    auth = _create(fake_home, "auth.json")
    auth.write_text("SEARCH_DIRECT_AUTH_SECRET", encoding="utf-8")

    def fail_if_called(task_id="default"):
        raise AssertionError("search backend should not run for blocked path")

    monkeypatch.setattr(ft, "_get_file_ops", fail_if_called)

    out = json.loads(
        ft.search_tool(
            pattern="SEARCH_DIRECT_AUTH_SECRET",
            path=str(auth),
            task_id="search-direct-auth-json",
        )
    )
    raw = json.dumps(out)
    assert "error" in out
    assert "credential store" in out["error"]
    assert "SEARCH_DIRECT_AUTH_SECRET" not in raw


def test_search_tool_filters_credential_results(fake_home, tmp_path, monkeypatch):
    """Directory searches omit credential and MCP-token result entries."""
    import json

    from tools.file_operations import SearchResult
    from tools.file_operations_common import SearchMatch
    import tools.file_tools as ft
    from tools import terminal_tool

    auth = _create(fake_home, "auth.json")
    token = _create(fake_home, Path("mcp-tokens") / "provider.json")
    safe = _create(fake_home, "notes.txt")

    class FakeFileOps:
        def search(self, **kwargs):
            return SearchResult(
                matches=[
                    SearchMatch(
                        path=str(auth),
                        line_number=1,
                        content="SEARCH_AUTH_SECRET",
                    ),
                    SearchMatch(
                        path=str(token),
                        line_number=1,
                        content="SEARCH_MCP_SECRET",
                    ),
                    SearchMatch(
                        path=str(safe),
                        line_number=1,
                        content="public note",
                    ),
                ],
                files=[str(auth), str(token), str(safe)],
                total_count=5,
                truncated=True,
            )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ft, "_get_file_ops", lambda task_id="default": FakeFileOps())
    monkeypatch.setattr(
        terminal_tool, "_session_cwd", {}
    )

    search_response = ft.search_tool(
        pattern="SEARCH",
        path=str(fake_home),
        task_id="search-filter-credentials",
    )
    out = json.loads(search_response)
    raw = json.dumps(out)
    returned_paths = {
        match["path"] for match in out.get("matches", [])
    } | set(out.get("files", []))

    assert "SEARCH_AUTH_SECRET" not in raw
    assert "SEARCH_MCP_SECRET" not in raw
    assert str(auth) not in returned_paths
    assert str(token) not in returned_paths
    assert "public note" in raw
    assert str(safe) in returned_paths
    assert out["_omitted"].startswith("4 result(s) omitted")
    assert out["total_count"] == 5
    assert out["truncated"] is True
    assert out["_hint"].startswith("Results truncated. Use offset=")


# ---------------------------------------------------------------------------
# Widening: .env, webhook_subscriptions.json, mcp-tokens/
# ---------------------------------------------------------------------------




def test_webhook_subscriptions_blocked(fake_home):
    """webhook_subscriptions.json holds per-route HMAC secrets — blocked."""
    from agent.file_safety import get_read_block_error

    subs = _create(fake_home, "webhook_subscriptions.json")
    err = get_read_block_error(str(subs))
    assert err is not None
    assert "credential store" in err








def test_identically_named_hermes_files_outside_home_not_blocked(
    fake_home, tmp_path
):
    """Hermes-specific filenames (``auth.json``, ``mcp-tokens/``, ``google_oauth.json``)
    outside HERMES_HOME must remain readable — the gate is per-location for
    those, not per-filename. ``.env`` is the exception: it's blocked anywhere
    on disk (see test_project_local_env_blocked) because the basename always
    means \"secret-bearing environment file\" regardless of directory."""
    from agent.file_safety import get_read_block_error

    project = tmp_path / "myproject"
    project.mkdir()
    # auth.json outside HERMES_HOME — readable (per-location gate).
    p = project / "auth.json"
    p.write_text("not secret here", encoding="utf-8")
    assert get_read_block_error(str(p)) is None, (
        "auth.json outside HERMES_HOME should NOT be blocked"
    )

    google_oauth = project / "auth" / "google_oauth.json"
    google_oauth.parent.mkdir()
    google_oauth.write_text("not really a token", encoding="utf-8")
    assert get_read_block_error(str(google_oauth)) is None

    tokens = project / "mcp-tokens"
    tokens.mkdir()
    tok_file = tokens / "token.json"
    tok_file.write_text("not really a token", encoding="utf-8")
    assert get_read_block_error(str(tok_file)) is None


def test_non_secret_auth_subtree_file_not_blocked(fake_home):
    """Only the known Google OAuth token path is blocked, not all auth/*."""
    from agent.file_safety import get_read_block_error

    note = _create(fake_home, Path("auth") / "notes.json")
    assert get_read_block_error(str(note)) is None


def test_config_yaml_not_blocked(fake_home):
    """config.yaml is NOT a credential file — agent should still be
    able to read it for debugging.  (Writes are denied separately by
    is_write_denied; reads stay allowed.)"""
    from agent.file_safety import get_read_block_error

    cfg = _create(fake_home, "config.yaml")
    assert get_read_block_error(str(cfg)) is None


def test_profile_mode_blocks_root_credentials(tmp_path, monkeypatch):
    """Under a profile, HERMES_HOME = <root>/profiles/<name>, but
    <root>/auth.json must ALSO be blocked — credentials at root are
    inherited by every profile."""
    import agent.file_safety as fs

    root = tmp_path / "hermes"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: profile)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: root)

    from agent.file_safety import get_read_block_error

    # Profile-local credential store: blocked
    profile_auth = profile / "auth.json"
    profile_auth.write_text("x")
    assert "credential store" in (get_read_block_error(str(profile_auth)) or "")

    # Root-level credential store: ALSO blocked (this is the widening)
    root_auth = root / "auth.json"
    root_auth.write_text("x")
    assert "credential store" in (get_read_block_error(str(root_auth)) or "")

    # Root-level .env: blocked too
    root_env = root / ".env"
    root_env.write_text("x")
    assert "credential store" in (get_read_block_error(str(root_env)) or "")

    # Root-level Google OAuth token store: blocked too
    root_google_oauth = root / "auth" / "google_oauth.json"
    root_google_oauth.parent.mkdir(parents=True, exist_ok=True)
    root_google_oauth.write_text("x")
    assert "credential store" in (
        get_read_block_error(str(root_google_oauth)) or ""
    )

    # Root-level mcp-tokens: blocked
    root_tok = root / "mcp-tokens" / "gh.json"
    root_tok.parent.mkdir(parents=True, exist_ok=True)
    root_tok.write_text("x")
    assert "MCP token" in (get_read_block_error(str(root_tok)) or "")


@pytest.mark.parametrize(
    "rel", ["webapp/session-token", "webapp/native-sign-in-tokens.json", "webapp/sign-in-registration.json"])
def test_the_webapps_sign_in_files_are_neither_read_nor_written(tmp_path, monkeypatch, rel):
    """``webapp/`` holds the Webapp's browser credential (a bare token no redaction pattern masks; it opens
    host shells and files on the loopback Webapp until ``hermes webapp --sign-out``), the record of the
    provider tokens its listener gave native apps and the Portal callback its Web Access registered. The
    agent's file tools neither read nor write anything there, in the profile's home or at the root: a
    write could plant a credential or a grant that the gateway then honours."""
    import agent.file_safety as fs
    from agent.file_safety import get_read_block_error, is_write_denied

    root = tmp_path / "hermes"
    profile = root / "profiles" / "coder"
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: profile)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: root)
    for home in (profile, root):
        stored = _create(home, rel)
        assert get_read_block_error(str(stored)) is not None, f"read_file hands the agent {stored}"
        assert get_read_block_error(str(stored.parent)) is not None
        assert is_write_denied(str(stored)), f"write_file may replace {stored}"
        assert is_write_denied(str(stored.parent / "planted.json"))


@pytest.mark.parametrize("rel", [
    "AUTH.JSON", "Auth.Json", ".ENV", ".Anthropic_OAuth.json",
    "SKILLS/.HUB/cache/SKILL.md", "skills/.Hub/x.md",
    "MCP-TOKENS/gh.json", "Vault/vault.key",
])
def test_read_deny_list_is_case_insensitive(fake_home, rel):
    """On a case-insensitive FS (macOS, Windows) ``resolve()`` keeps the caller's spelling, so
    ``~/.hermes/AUTH.JSON`` opens ``auth.json``; an exact-case compare let it through."""
    from agent.file_safety import get_read_block_error

    path = _create(fake_home, rel)
    assert get_read_block_error(str(path)) is not None, rel


def test_case_insensitive_home_dir_is_blocked(tmp_path, monkeypatch):
    """``~/.Config/Token-Broker/x`` is ``~/.config/token-broker/x`` on macOS."""
    import agent.file_safety as fs

    home = tmp_path / "user"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(fs, "_guard_homes", lambda _p="": {str(home)})
    key = home / ".Config" / "Token-Broker" / "token.json"
    key.parent.mkdir(parents=True)
    key.write_text("k")
    assert fs.get_read_block_error(str(key)) is not None


def test_read_deny_candidate_names_cover_every_name_rule(fake_home):
    """``read_deny_candidate_names`` is the walkers' pre-filter: every basename a deny rule
    matches on (exact stores, denied dirs, env files, ``.hub``) must be in it, casefolded,
    or a sandbox walker would skip the full guard for a denied path."""
    import agent.file_safety as fs

    names = [os.path.basename(n) for n in fs._CREDENTIAL_FILE_NAMES]
    names += [sub for sub, _d, _f in fs._READ_DENIED_DIRS]
    names += [rel.rsplit("/", 1)[-1] for rel in fs.HOME_READ_DENIED_DIRS]
    names += [*fs._BLOCKED_PROJECT_ENV_BASENAMES, ".hub"]
    candidates = fs.read_deny_candidate_names()
    for name in names:
        assert name.casefold() in candidates, name
        assert name.upper().casefold() in candidates, name
    for ordinary in ("SKILL.md", "references", "scripts", "run.sh", ".env.example"):
        assert ordinary.casefold() not in candidates, ordinary


@pytest.mark.require_symlinks
def test_read_deny_candidate_names_include_resolved_symlink_targets(fake_home, tmp_path):
    """The guard denies the resolved target of a symlinked store, so its basename is a candidate."""
    import agent.file_safety as fs

    target = tmp_path / "elsewhere" / "creds.json"
    target.parent.mkdir()
    target.write_text("{}")
    (fake_home / "auth.json").symlink_to(target)
    assert fs.get_read_block_error(str(target)) is not None
    assert "creds.json" in fs.read_deny_candidate_names()


@pytest.mark.require_symlinks
def test_hub_cache_is_denied_through_a_symlinked_skills_dir(fake_home, tmp_path):
    """``HERMES_HOME/skills`` is often a symlink into a repo; the resolved ``.hub`` path is still denied."""
    from agent.file_safety import get_read_block_error

    repo_skills = tmp_path / "repo" / "skills"
    (repo_skills / ".hub").mkdir(parents=True)
    cached = repo_skills / ".hub" / "SKILL.md"
    cached.write_text("x")
    (fake_home / "skills").symlink_to(repo_skills)
    assert get_read_block_error(str(cached)) is not None
