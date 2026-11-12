"""Browser sign-in/portal openers reach a person's browser through the clean helper. [daveey]

main() advertises HERMES_AGENT/AI_AGENT in os.environ, so an in-process ``webbrowser.open`` hands a
freshly started browser those markers and the dashboard comment endpoint would record a person's
notes as an agent's. Every caller therefore goes through ``hermes_cli.top_level_env``
(``open_url_for_person_checked`` when it reports success, ``open_url_for_person`` otherwise); the
raw stdlib opener is a tripwire here, and a failed open keeps each caller's fallback behaviour.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from hermes_cli import top_level_env

URL = "https://example.test/authorize?x=1"


class _Stop(Exception):
    """Ends a flow right after its browser open (the callback wait is not under test)."""


class _Opener:
    def __init__(self):
        self.raw: list = []
        self.checked: list = []
        self.unchecked: list = []
        self.result = True


@pytest.fixture
def opener(monkeypatch):
    import webbrowser

    o = _Opener()

    def raw(url=None, *args, **kwargs):
        o.raw.append(url)
        return True

    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, raw, raising=False)

    def checked(url, *args, **kwargs):
        o.checked.append((url, kwargs))
        if isinstance(o.result, BaseException):
            raise o.result
        return o.result

    def unchecked(url, *args, **kwargs):
        o.unchecked.append((url, kwargs))

    monkeypatch.setattr(top_level_env, "open_url_for_person_checked", checked)
    monkeypatch.setattr(top_level_env, "open_url_for_person", unchecked)
    return o


def _stop(*_a, **_k):
    raise _Stop


def _loopback_stubs(monkeypatch, module):
    monkeypatch.setattr(module, "_can_open_graphical_browser", lambda: True)
    monkeypatch.setattr(module, "_bind_loopback_callback_server",
                        lambda *a, **k: SimpleNamespace(server_address=("127.0.0.1", 4242)))
    monkeypatch.setattr(module, "_serve_loopback_callback", _stop)


# ── loopback PKCE sign-ins ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("result, expected", [(True, "Browser opened for OpenAI"), (False, "Could not open the browser")])
def test_codex_browser_login_opens_via_checked_helper(monkeypatch, opener, capsys, result, expected):
    from hermes_cli import auth_codex_browser as mod
    _loopback_stubs(monkeypatch, mod)
    opener.result = result
    with pytest.raises(_Stop):
        mod._codex_browser_login()
    assert opener.raw == [] and len(opener.checked) == 1
    assert opener.checked[0][0].startswith(mod.CODEX_OAUTH_AUTHORIZE_URL)
    assert expected in capsys.readouterr().out


def test_codex_browser_login_open_error_keeps_manual_fallback(monkeypatch, opener, capsys):
    from hermes_cli import auth_codex_browser as mod
    _loopback_stubs(monkeypatch, mod)
    opener.result = RuntimeError("no browser")
    with pytest.raises(_Stop):
        mod._codex_browser_login()
    assert "Could not open the browser" in capsys.readouterr().out and opener.raw == []


def test_codex_browser_login_respects_no_browser(monkeypatch, opener):
    from hermes_cli import auth_codex_browser as mod
    _loopback_stubs(monkeypatch, mod)
    with pytest.raises(_Stop):
        mod._codex_browser_login(open_browser=False)
    assert opener.raw == [] and opener.checked == [] and opener.unchecked == []


@pytest.mark.parametrize("result, expected", [(True, "Browser opened for OpenRouter"), (False, "Could not open the browser")])
def test_openrouter_loopback_opens_via_checked_helper(monkeypatch, opener, capsys, result, expected):
    from hermes_cli import auth_openrouter as mod
    _loopback_stubs(monkeypatch, mod)
    opener.result = result
    with pytest.raises(_Stop):
        mod._openrouter_loopback_code({"code_challenge": "c"}, open_browser=True, timeout_seconds=1)
    assert opener.raw == [] and len(opener.checked) == 1
    assert opener.checked[0][0].startswith(mod.OPENROUTER_AUTH_URL)
    assert expected in capsys.readouterr().out


def test_openrouter_loopback_headless_does_not_open(monkeypatch, opener):
    from hermes_cli import auth_openrouter as mod
    _loopback_stubs(monkeypatch, mod)
    monkeypatch.setattr(mod, "_can_open_graphical_browser", lambda: False)
    with pytest.raises(_Stop):
        mod._openrouter_loopback_code({}, open_browser=True, timeout_seconds=1)
    assert opener.raw == [] and opener.checked == []


def test_generic_pkce_plugin_login_opens_via_clean_helper(monkeypatch, opener):
    from hermes_cli import auth_device_flow as flow
    from hermes_cli import auth_oauth_pkce_plugin as mod
    monkeypatch.setattr(mod, "validate_config", lambda *a, **k: None)
    monkeypatch.setattr(flow, "_can_open_graphical_browser", lambda: True)
    monkeypatch.setattr(flow, "_bind_loopback_callback_server",
                        lambda *a, **k: SimpleNamespace(server_address=("127.0.0.1", 4242)))
    monkeypatch.setattr(flow, "_serve_loopback_callback", _stop)
    cfg = SimpleNamespace(redirect_path="/cb", redirect_port=0, label="Voice", extra_authorize_params={},
                          client_id="cid", scopes=("a",), audience=None, authorize_url="https://idp.test/authorize",
                          timeout_seconds=1)
    with pytest.raises(_Stop):
        mod.login("voice", cfg)
    assert opener.raw == [] and opener.checked == []
    assert [u.startswith("https://idp.test/authorize?") for u, _ in opener.unchecked] == [True]


def test_generic_pkce_plugin_open_failure_is_not_fatal(monkeypatch, opener):
    """A raising opener keeps the loopback wait going (the URL is already printed)."""
    from hermes_cli import auth_device_flow as flow
    from hermes_cli import auth_oauth_pkce_plugin as mod
    monkeypatch.setattr(mod, "validate_config", lambda *a, **k: None)
    monkeypatch.setattr(flow, "_can_open_graphical_browser", lambda: True)
    monkeypatch.setattr(flow, "_bind_loopback_callback_server",
                        lambda *a, **k: SimpleNamespace(server_address=("127.0.0.1", 4242)))
    monkeypatch.setattr(flow, "_serve_loopback_callback", _stop)
    monkeypatch.setattr(top_level_env, "open_url_for_person", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    cfg = SimpleNamespace(redirect_path="/cb", redirect_port=0, label="Voice", extra_authorize_params={},
                          client_id="cid", scopes=(), audience=None, authorize_url="https://idp.test/authorize",
                          timeout_seconds=1)
    with pytest.raises(_Stop):  # reached the callback wait, i.e. the open error was swallowed
        mod.login("voice", cfg)
    assert opener.raw == []


# ── device-code / challenge / portal / billing / modal / MCP / Anthropic / Photon ─────────────

@pytest.mark.parametrize("result, expected", [(True, "Opened browser"), (False, "Could not open browser")])
def test_device_code_instructions_report_checked_outcome(opener, capsys, result, expected):
    from hermes_cli.auth_device_flow import _print_device_code_instructions
    opener.result = result
    _print_device_code_instructions(URL, "ABCD", open_browser=True)
    assert opener.raw == [] and [u for u, _ in opener.checked] == [URL]
    assert expected in capsys.readouterr().out


def test_device_code_instructions_swallow_flag_controls_open_errors(opener, capsys):
    from hermes_cli.auth_device_flow import _print_device_code_instructions
    opener.result = OSError("spawn")
    _print_device_code_instructions(URL, "ABCD", open_browser=True, swallow_open_errors=True)
    assert "Could not open browser" in capsys.readouterr().out
    with pytest.raises(OSError):
        _print_device_code_instructions(URL, "ABCD", open_browser=True)
    assert opener.raw == []


def test_device_code_instructions_no_browser_opens_nothing(opener):
    from hermes_cli.auth_device_flow import _print_device_code_instructions
    _print_device_code_instructions(URL, "ABCD", open_browser=False)
    assert opener.raw == [] and opener.checked == []


@pytest.mark.parametrize("result", [True, False])
def test_anon_challenge_tracks_the_checked_result(monkeypatch, opener, capsys, result):
    from hermes_cli import anon_challenge, auth_device_flow
    monkeypatch.setattr(anon_challenge, "client_surface", lambda: "cli")
    monkeypatch.setattr(auth_device_flow, "_is_remote_session", lambda: False)
    monkeypatch.setattr(auth_device_flow, "_can_open_graphical_browser", lambda: True)
    monkeypatch.setattr(anon_challenge, "_opened_urls", set())
    opener.result = result
    challenge = anon_challenge.BrowserChallenge(url=URL, required=True, expires_in=60, interval=1, message="Verify")
    anon_challenge._present_in_terminal(challenge)
    assert opener.raw == [] and [u for u, _ in opener.checked] == [URL]
    assert (URL in anon_challenge._opened_urls) is result
    assert ("(Opened in your browser.)" in capsys.readouterr().err) is result


@pytest.mark.parametrize("result, code", [(True, 0), (False, 1)])
def test_portal_open_returns_the_checked_result(opener, capsys, result, code):
    from hermes_cli import portal_cli
    opener.result = result
    assert portal_cli._cmd_open(None) == code
    assert opener.raw == [] and [u for u, _ in opener.checked] == [portal_cli.SUBSCRIPTION_URL]
    assert ("Could not launch a browser" in capsys.readouterr().out) is (not result)


@pytest.mark.parametrize("result", [True, False, OSError("spawn")])
def test_billing_open_returns_the_checked_result(monkeypatch, opener, result):
    from hermes_cli import auth
    from hermes_cli.cli_billing_mixin import CLIBillingMixin
    monkeypatch.setattr(auth, "_can_open_graphical_browser", lambda: True)
    monkeypatch.setattr(auth, "_is_remote_session", lambda: False)
    opener.result = result
    assert CLIBillingMixin._open_url_in_browser(SimpleNamespace(), URL) is (result is True)
    assert opener.raw == [] and [u for u, _ in opener.checked] == [URL]


def test_billing_open_keeps_remote_and_headless_refusal(monkeypatch, opener):
    from hermes_cli import auth
    from hermes_cli.cli_billing_mixin import CLIBillingMixin
    monkeypatch.setattr(auth, "_can_open_graphical_browser", lambda: True)
    monkeypatch.setattr(auth, "_is_remote_session", lambda: True)
    assert CLIBillingMixin._open_url_in_browser(SimpleNamespace(), URL) is False
    assert opener.raw == [] and opener.checked == []


def test_modal_connection_open_uses_clean_helper(opener):
    from hermes_cli.cli_modal_mixin import CLIModalMixin
    state = {"phase": "url", "target": {"connect_url": URL}}
    CLIModalMixin._connection_open_url(SimpleNamespace(_connection_state=state))
    assert opener.raw == [] and [u for u, _ in opener.unchecked] == [URL]


@pytest.mark.parametrize("result, expected", [(True, "Browser opened automatically"), (False, "Could not open browser")])
def test_mcp_oauth_announce_reports_checked_outcome(monkeypatch, opener, capsys, result, expected):
    from tools import mcp_oauth
    monkeypatch.setattr(mcp_oauth, "_can_open_browser", lambda: True)
    opener.result = result
    mcp_oauth._announce_authorization_url(URL, 1234, None)
    assert opener.raw == [] and [u for u, _ in opener.checked] == [URL]
    assert expected in capsys.readouterr().err


def test_mcp_oauth_announce_headless_does_not_open(monkeypatch, opener, capsys):
    from tools import mcp_oauth
    monkeypatch.setattr(mcp_oauth, "_can_open_browser", lambda: False)
    mcp_oauth._announce_authorization_url(URL, 1234, None)
    assert opener.raw == [] and opener.checked == []
    assert "Headless environment detected" in capsys.readouterr().err


def test_anthropic_oauth_login_opens_via_clean_helper(monkeypatch, opener):
    from agent import anthropic_credentials
    from hermes_cli import auth
    monkeypatch.setattr(auth, "_can_open_graphical_browser", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a: (_ for _ in ()).throw(EOFError))
    assert anthropic_credentials.run_hermes_oauth_login_pure() is None
    assert opener.raw == [] and opener.checked == []
    assert [u.startswith("https://claude.ai/oauth/authorize?") for u, _ in opener.unchecked] == [True]


def test_anthropic_oauth_login_headless_does_not_open(monkeypatch, opener):
    from agent import anthropic_credentials
    from hermes_cli import auth
    monkeypatch.setattr(auth, "_can_open_graphical_browser", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *_a: (_ for _ in ()).throw(EOFError))
    assert anthropic_credentials.run_hermes_oauth_login_pure() is None
    assert opener.raw == [] and opener.unchecked == []


def test_photon_device_login_opens_a_new_window_via_clean_helper(monkeypatch, opener):
    from plugins.platforms.photon import auth
    code = SimpleNamespace(device_code="d", user_code="U", verification_uri="https://p.test/v",
                           verification_uri_complete="https://p.test/v?c=U", expires_in=60, interval=1)
    monkeypatch.setattr(auth, "request_device_code", lambda **k: code)
    monkeypatch.setattr(auth, "poll_for_token", _stop)
    with pytest.raises(_Stop):
        auth.login_device_flow(open_browser=True)
    assert opener.raw == [] and opener.checked == []
    assert opener.unchecked == [("https://p.test/v?c=U", {"new": 2})]


# ── the helper itself: ``new`` reaches the child interpreter's webbrowser.open ─────────────────

def test_url_opener_command_forwards_new_window_flag():
    argv = top_level_env.url_opener_command("http://x/", new=2)
    assert argv[-2:] == ["http://x/", "2"]
    assert top_level_env.url_opener_command("http://x/")[-2:] == ["http://x/", "0"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX BROWSER script")
def test_child_opener_passes_new_to_webbrowser(monkeypatch, tmp_path):
    """The child must call ``webbrowser.open(url, new=N)``; an observer controller records ``new``."""
    import subprocess
    dump = tmp_path / "new.txt"
    code = ("import sys, webbrowser\n"
            "class C(webbrowser.BaseBrowser):\n"
            "    def open(self, url, new=0, autoraise=True):\n"
            f"        open({str(dump)!r}, 'w').write(f'{{url}} {{new}}'); return True\n"
            "webbrowser.register('probe', None, C(), preferred=True)\n")
    argv = top_level_env.url_opener_command("http://x/", new=2)
    # same child body, preceded by the probe registration
    body = argv[argv.index("-c") + 1]
    assert subprocess.run([sys.executable, "-I", "-c", code + body, "http://x/", "2"], check=False).returncode == 0
    assert dump.read_text() == "http://x/ 2"


@pytest.mark.parametrize("fn", ["open_url_for_person", "open_url_for_person_checked"])
def test_helpers_pass_new_to_the_spawn(monkeypatch, fn):
    seen = []

    class _P:
        def wait(self, timeout):
            return 0

    monkeypatch.setattr(top_level_env, "_spawn_clean_url_opener", lambda url, new=0: seen.append((url, new)) or _P())
    # conftest replaced the module attributes with recorders; resolve the real functions.
    real = _REAL[fn]
    real("http://x/", new=2)
    assert seen == [("http://x/", 2)]


_REAL = {"open_url_for_person": top_level_env.open_url_for_person,
         "open_url_for_person_checked": top_level_env.open_url_for_person_checked}
