"""Kanban admission serves a recorded plugin discovery only when discovery cannot change the answer.

Each case runs ``validate_assignment`` in a fresh interpreter (the admission helper's cold
path) against a real isolated profile with real user plugins, and compares it with a full
discovery. [daveey]
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

PROBE = textwrap.dedent("""
    import json, sys
    from hermes_cli import plugins as P
    calls = []
    _orig = P.PluginManager._discover_and_load_inner
    def _spy(self, *a, **k):
        calls.append(1)
        return _orig(self, *a, **k)
    P.PluginManager._discover_and_load_inner = _spy
    import providers
    from hermes_cli.kanban_capabilities import CapabilityMismatchError, validate_assignment
    req = json.loads(sys.argv[2])
    try:
        out = {"granted": validate_assignment(sys.argv[1], requires=req)}
    except CapabilityMismatchError as exc:
        out = {"missing": exc.missing}
    out["discoveries"] = len(calls)
    out["providers_discovered"] = bool(providers._discovered)
    out["auth_imported"] = "hermes_cli.auth" in sys.modules
    print("RESULT " + json.dumps(out))
""")


def _plugin(home: Path, name: str, body: str) -> Path:
    d = home / "plugins" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text(f"name: {name}\nversion: 0.1.0\ndescription: test plugin\n")
    (d / "__init__.py").write_text(textwrap.dedent(body))
    return d


TOOL = """
    def _schema(name):
        return {"name": name, "description": "t", "parameters": {"type": "object", "properties": {}}}

    def register(ctx):
        ctx.register_tool(name=%r, toolset=%r, schema=_schema(%r), handler=lambda args, **kw: "{}"%s)
"""


def _tool_plugin(home, name, tool, toolset, override=False):
    return _plugin(home, name, TOOL % (tool, toolset, tool, ", override=True" if override else ""))


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "hermes"
    h.mkdir()
    return h


def _config(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(textwrap.dedent(text))


def _run(home: Path, requires=("terminal", "file"), assignee="default", *, cwd=None, extra_path=None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "PYTEST"))}
    # The runner may provide dependencies through sys.path rather than site-packages.
    pythonpath = os.pathsep.join([str(REPO), *([str(extra_path)] if extra_path else []),
                                  *(os.path.abspath(p) for p in sys.path if p)])
    env.update(HERMES_HOME=str(home), HOME=str(home.parent), PYTHONPATH=pythonpath, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", PROBE, assignee, json.dumps(list(requires))], cwd=str(cwd or home.parent),
                          env=env, capture_output=True, text=True, timeout=120, check=False)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-4000:]
    return json.loads(line[len("RESULT "):])


def _verdict(r: dict) -> dict:
    return {k: r[k] for k in ("granted", "missing") if k in r}


ENABLED = "plugins:\n  enabled: [%s]\n"
OVERRIDE = "plugins:\n  enabled: [%s]\n  entries:\n    %s:\n      allow_tool_override: true\n"


def test_second_cold_process_serves_the_record_without_discovery(home):
    _tool_plugin(home, "extra", "extra_ping", "extra")
    _config(home, "platform_toolsets:\n  cli: [terminal, file, extra]\n" + ENABLED % "extra")
    first = _run(home)
    assert first["discoveries"] == 1 and first["granted"] == ["terminal", "file"]
    blob = json.loads((home / "cache" / "plugin_toolset_keys.json").read_text())
    assert "extra" in blob["toolset_keys"] and blob["builtin_collision"] is None and blob["fingerprint"]
    second = _run(home)
    assert _verdict(second) == _verdict(first)
    assert second["discoveries"] == 0
    # The config-module import no longer discovers providers either (it ran via plugins before).
    assert second["providers_discovered"] is False


def test_served_default_composite_does_not_import_auth_or_providers(home):
    """A profile with no saved cli toolset list resolves through the default composite, which probes
    xAI credentials (importing hermes_cli.auth, ~3.7 s cold on the mini). A served record needs
    only terminal/file membership, so it must not pay that; a stale record falls back to the full
    discovery (and its credential probe)."""
    _tool_plugin(home, "extra", "extra_ping", "extra")
    _config(home, ENABLED % "extra")
    first = _run(home)
    assert first["discoveries"] == 1 and first["granted"] == ["terminal", "file"]
    second = _run(home)
    assert _verdict(second) == _verdict(first)
    assert second["discoveries"] == 0
    assert second["auth_imported"] is False and second["providers_discovered"] is False
    # Editing a plugin makes the record stale: full discovery again, same verdict.
    (home / "plugins" / "extra" / "__init__.py").write_text(
        (home / "plugins" / "extra" / "__init__.py").read_text() + "\n# edited\n")
    third = _run(home)
    assert third["discoveries"] == 1 and _verdict(third) == _verdict(first)


@pytest.mark.parametrize("cli", [
    None,                                   # platform default composite
    "[hermes-cli]",                         # explicit composite
    "[terminal]",                           # file missing -> refusal on both paths
    "[file, extra]",                        # terminal missing
    "[]",                                   # explicit empty
    "[terminal, file, no_mcp]",
])
@pytest.mark.parametrize("disabled", [None, "[debugging]", "[extra]", "[terminal]"])
def test_served_answer_equals_full_discovery(home, cli, disabled):
    _tool_plugin(home, "extra", "extra_ping", "extra")
    text = ENABLED % "extra"
    if cli is not None:
        text += f"platform_toolsets:\n  cli: {cli}\n"
    if disabled is not None:
        text += f"agent:\n  disabled_toolsets: {disabled}\n"
    _config(home, text)
    full = _run(home)
    served = _run(home)
    assert full["discoveries"] == 1
    assert _verdict(served) == _verdict(full)
    if "granted" in full and disabled is None:
        assert served["discoveries"] == 0
    else:
        # A refusal is never taken from the record (the full discovery reconfirms it), and a
        # disabled_toolsets list needs model_tools, whose first import is a process's discovery.
        assert served["discoveries"] == 1


def test_plugin_overriding_a_builtin_tool_is_never_served(home):
    # A plugin that takes over the `terminal` tool into its own toolset: disabling that toolset
    # strips terminal at runtime, which only a real discovery can see.
    _tool_plugin(home, "evil", "terminal", "evil", override=True)
    _config(home, OVERRIDE % ("evil", "evil") + "agent:\n  disabled_toolsets: [evil]\n")
    first = _run(home)
    blob = json.loads((home / "cache" / "plugin_toolset_keys.json").read_text())
    assert blob["builtin_collision"] and "terminal" in blob["builtin_collision"]
    second = _run(home)
    assert _verdict(second) == _verdict(first)
    assert second["discoveries"] == 1


def test_plugin_tool_in_a_builtin_toolset_is_never_served(home):
    _tool_plugin(home, "squat", "squat_tool", "terminal")
    _config(home, ENABLED % "squat")
    _run(home)
    blob = json.loads((home / "cache" / "plugin_toolset_keys.json").read_text())
    assert "built-in toolset 'terminal'" in blob["builtin_collision"]
    assert _run(home)["discoveries"] == 1


def test_editing_a_plugin_invalidates_the_record(home):
    plugin = _tool_plugin(home, "extra", "extra_ping", "extra")
    _config(home, ENABLED % "extra")
    assert _run(home)["granted"] == ["terminal", "file"]
    assert _run(home)["discoveries"] == 0
    # The plugin now puts a tool into the built-in `terminal` toolset: the record is stale, the
    # next process discovers for real and records the collision, so it is never served again.
    (plugin / "__init__.py").write_text(textwrap.dedent(TOOL % ("extra_ping", "terminal", "extra_ping", "")))
    after = _run(home)
    assert after["discoveries"] == 1 and after["granted"] == ["terminal", "file"]
    blob = json.loads((home / "cache" / "plugin_toolset_keys.json").read_text())
    assert "built-in toolset 'terminal'" in blob["builtin_collision"]
    assert _run(home)["discoveries"] == 1


def test_new_plugin_dir_and_config_edit_invalidate_the_record(home):
    _config(home, "platform_toolsets:\n  cli: [terminal, file]\n")
    _run(home)
    assert _run(home)["discoveries"] == 0
    _tool_plugin(home, "late", "late_ping", "late")
    assert _run(home)["discoveries"] == 1
    # A concurrent importer can create bytecode after discovery recorded its inputs.
    # That moves the plugin directory's mtime, but must not invalidate the new record.
    plugin = home / "plugins" / "late"
    (plugin / "__pycache__").mkdir()
    st = plugin.stat()
    os.utime(plugin, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert _run(home)["discoveries"] == 0
    _config(home, "platform_toolsets:\n  cli: [terminal, file]\n" + ENABLED % "late")
    assert _run(home)["discoveries"] == 1


def test_metadata_only_sync_pass_keeps_the_record_served(home):
    """A sync pass that rewrites nothing (macOS openrsync -a --checksum) re-applies mode and times,
    moving only ctime: the record must stay served. [daveey]"""
    _tool_plugin(home, "extra", "extra_ping", "extra")
    _config(home, "platform_toolsets:\n  cli: [terminal, file, extra]\n" + ENABLED % "extra")
    first = _run(home)
    assert first["discoveries"] == 1
    files = [p for p in [home / "config.yaml", *(home / "plugins").rglob("*")] if p.is_file()]
    for p in files:
        st = p.stat()
        os.chmod(p, st.st_mode)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    second = _run(home)
    assert _verdict(second) == _verdict(first)
    assert second["discoveries"] == 0


@pytest.mark.parametrize("blob", ["", "{not json", "[]", json.dumps({"toolset_keys": [], "portable_mcp": []}),
                                  json.dumps({"version": 1, "fingerprint": "0" * 64, "builtin_collision": None,
                                              "toolset_keys": [], "portable_mcp": []})])
def test_missing_corrupt_old_or_stale_record_runs_full_discovery(home, blob):
    _config(home, "platform_toolsets:\n  cli: [terminal, file]\n")
    (home / "cache").mkdir()
    (home / "cache" / "plugin_toolset_keys.json").write_text(blob)
    r = _run(home)
    assert r["discoveries"] == 1 and r["granted"] == ["terminal", "file"]


def test_served_refusal_falls_back_to_full_resolution(monkeypatch, tmp_path):
    """In-process: when the served answer lacks a needed toolset, the full path decides."""
    from hermes_cli import kanban_capabilities as kc, plugins_snapshot
    import hermes_cli.tools_config as tc

    monkeypatch.setattr(plugins_snapshot, "load", lambda home: plugins_snapshot.Snapshot(frozenset(), frozenset()))
    seen = []

    def fake(config, platform, *, include_default_mcp_servers=True):
        served = plugins_snapshot.active() is not None
        seen.append(served)
        return set() if served else {"terminal", "file"}

    monkeypatch.setattr(tc, "_get_platform_tools", fake)
    assert kc._cli_toolsets({}, tmp_path, {"terminal"}) == {"terminal", "file"}
    assert seen == [True, False]
    seen.clear()
    assert kc._cli_toolsets({}, tmp_path, None) == {"terminal", "file"}  # no need -> never served
    assert seen == [False]


def test_record_is_shared_by_callers_with_other_cwd_and_script_paths(home, tmp_path):
    """The admission helper (its own script dir on sys.path, another cwd) and a profile session
    must share one record, or a record written by one is never served to the other."""
    _config(home, "platform_toolsets:\n  cli: [terminal, file]\n")
    assert _run(home)["discoveries"] == 1
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = _run(home, cwd=elsewhere, extra_path=elsewhere)
    assert r["discoveries"] == 0 and r["granted"] == ["terminal", "file"]


def test_profile_scoped_caller_reads_the_profile_record(home):
    """A helper whose HERMES_HOME is the root validates a named profile inside its home scope."""
    prof = home / "profiles" / "worker"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text("platform_toolsets:\n  cli: [terminal, file]\n")
    first = _run(home, assignee="worker")
    assert first["discoveries"] == 1 and (prof / "cache" / "plugin_toolset_keys.json").exists()
    assert _run(home, assignee="worker")["discoveries"] == 0
    assert _run(prof)["discoveries"] == 0  # the profile's own process (HERMES_HOME=profile) shares it


def test_fingerprint_ignores_bytecode_cache_creation_but_sees_plugin_changes(home, monkeypatch):
    """Another process byte-compiling a plugin (or a first __pycache__ in a site dir) moves the
    directory mtime without changing what discovery reads; that must not stale the record."""
    from hermes_cli import plugins_snapshot as S

    plugin = _tool_plugin(home, "extra", "extra_ping", "extra")
    site = home.parent / "site-packages"
    (site / "pkg-1.0.dist-info").mkdir(parents=True)
    monkeypatch.setattr(sys, "path", [*sys.path, str(site)])
    before = S.fingerprint(home)
    assert before
    (plugin / "__pycache__").mkdir()
    (plugin / "__pycache__" / "__init__.cpython-312.pyc").write_bytes(b"x")
    (site / "__pycache__").mkdir()
    os.utime(plugin, ns=(1, 1))
    os.utime(home / "plugins", ns=(1, 1))
    os.utime(site, ns=(1, 1))
    assert S.fingerprint(home) == before
    (plugin / "extra_more.py").write_text("X = 1\n")
    changed = S.fingerprint(home)
    assert changed != before
    (plugin / "extra_more.py").unlink()
    (site / "other-2.0.dist-info").mkdir()
    assert S.fingerprint(home) not in (before, changed)
