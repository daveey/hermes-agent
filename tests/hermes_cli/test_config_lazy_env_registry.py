"""OPTIONAL_ENV_VARS' import-time additions run on first use, with the eager import's results. [daveey]

Importing ``hermes_cli.config`` used to discover every provider plugin and read every platform
manifest; the kanban admission helper paid that in each cold process without reading the dict.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _py(code: str, tmp_path, prelude: str = "", extra_env: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "PYTEST"))}
    home = tmp_path / "hermes"
    home.mkdir(exist_ok=True)
    # The runner may provide dependencies through sys.path rather than site-packages.
    env.update(HERMES_HOME=str(home), HOME=str(tmp_path),
               PYTHONPATH=os.pathsep.join([str(REPO), *(os.path.abspath(p) for p in sys.path if p)]))
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, "-c", textwrap.dedent(prelude) + textwrap.dedent(code)], env=env, capture_output=True, text=True, check=False,
                       timeout=180, cwd=str(tmp_path))
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, p.stdout[-2000:] + p.stderr[-4000:]
    return json.loads(line[7:])


DUMP = """
    import json
    import hermes_cli.config as c
    def dump():
        return {"keys": list(c.OPTIONAL_ENV_VARS.keys()), "values": json.loads(json.dumps(dict(c.OPTIONAL_ENV_VARS.items()), default=str)),
                "core": sorted(c.CORE_DECLARED_ENV_NAMES),
                "bundled": None if c.BUNDLED_PLATFORM_SECRET_ENVS is None else sorted(c.BUNDLED_PLATFORM_SECRET_ENVS)}
"""


def test_importing_config_discovers_no_providers(tmp_path):
    r = _py("""
        import providers, hermes_cli.config as c
        print("RESULT " + __import__("json").dumps({"providers": bool(providers._discovered), "state": c._env_registry._state}))
    """, tmp_path)
    assert r == {"providers": False, "state": 0}


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs POSIX modes, non-root")
def test_importing_config_still_secures_the_home_once(tmp_path):
    """The eager import secured the home (load_config inside provider discovery); a later
    permission change must survive the first config read, as it did then.

    HERMES_HOME_MODE is explicit because the secure-dir policy skips an implicit chmod inside a
    container (CI runners are detected as one); an explicit mode applies on hosts and containers."""
    r = _py("""
        import json, os, stat
        from pathlib import Path
        home = Path(os.environ["HERMES_HOME"])
        home.chmod(0o755)
        import hermes_cli.config as c
        secured = stat.S_IMODE(home.stat().st_mode)
        home.chmod(0o555)
        try:
            c.load_config_readonly()
            after = stat.S_IMODE(home.stat().st_mode)
        finally:
            home.chmod(0o755)
        print("RESULT " + json.dumps({"secured": oct(secured), "after": oct(after)}))
    """, tmp_path, extra_env={"HERMES_HOME_MODE": "0700"})
    assert r == {"secured": "0o700", "after": "0o555"}


@pytest.mark.parametrize("touch", [
    "list(c.OPTIONAL_ENV_VARS)",
    "len(c.OPTIONAL_ENV_VARS)",
    "'OPENROUTER_API_KEY' in c.OPTIONAL_ENV_VARS",
    "c.OPTIONAL_ENV_VARS.get('OPENROUTER_API_KEY')",
    "dict(c.OPTIONAL_ENV_VARS)",
    "{**c.OPTIONAL_ENV_VARS}",
    "json.dumps(c.OPTIONAL_ENV_VARS, default=str)",
    "c.CORE_DECLARED_ENV_NAMES",
    "c.BUNDLED_PLATFORM_SECRET_ENVS",
    "__import__('hermes_cli.config_defaults', fromlist=['x']).OPTIONAL_ENV_VARS.items()",
    "c.get_missing_env_vars()",
])
def test_every_access_style_sees_the_complete_registry(tmp_path, touch):
    r = _py(f"""
        first = {touch}
        d = dump()
        c._env_registry._state == 2 or print("NOT RUN")
        print("RESULT " + json.dumps({{"dump": d, "state": c._env_registry._state}}))
    """, tmp_path, DUMP)
    ref = _py("""
        c._env_registry.ensure()
        print("RESULT " + json.dumps({"dump": dump(), "state": c._env_registry._state}))
    """, tmp_path, DUMP)
    assert r["state"] == 2 and r["dump"] == ref["dump"]


def test_core_snapshot_is_taken_between_provider_and_platform_additions(tmp_path):
    r = _py("""
        import hermes_cli.config_defaults as d
        base = list(dict.keys(d.OPTIONAL_ENV_VARS))     # the static table, read without the hook
        out = dump()
        print("RESULT " + json.dumps({"base": base, **out}))
    """, tmp_path, DUMP)
    keys, core, base = r["keys"], set(r["core"]), r["base"]
    assert keys[: len(base)] == base                     # additions append, in order
    provider_added = core - set(base)
    platform_added = set(keys) - core
    assert provider_added and platform_added             # both stages contributed
    assert core == set(keys[: len(core)])                # the snapshot is a prefix: providers first
    assert set(r["bundled"] or ()) <= set(keys) and not set(r["bundled"] or ()) & {k.upper() for k in core}


def test_reload_reruns_the_additions_as_the_eager_import_did(tmp_path):
    # config_defaults is not reloaded, so the eager import re-ran its additions on an already full
    # dict: the snapshot then held every key and no bundled platform name stayed outside it.
    r = _py("""
        import importlib
        before = dump()
        importlib.reload(c)
        after = dump()
        print("RESULT " + json.dumps({"before": before, "after": after, "state": c._env_registry._state}))
    """, tmp_path, DUMP)
    assert r["state"] == 2 and r["after"]["keys"] == r["before"]["keys"]
    assert r["after"]["core"] == sorted(r["before"]["keys"]) and r["after"]["bundled"] == []


def test_concurrent_first_touches_run_the_additions_once(tmp_path):
    r = _py("""
        import json, threading, time
        import hermes_cli.config as c
        calls = []
        real = c._inject_profile_env_vars
        def slow():
            calls.append(1); time.sleep(0.3); real()
        c._inject_profile_env_vars = slow
        sizes = []
        ts = [threading.Thread(target=lambda: sizes.append(len(c.OPTIONAL_ENV_VARS))) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        print("RESULT " + json.dumps({"calls": len(calls), "sizes": sorted(set(sizes)), "core": len(c.CORE_DECLARED_ENV_NAMES)}))
    """, tmp_path)
    assert r["calls"] == 1 and len(r["sizes"]) == 1 and r["core"] <= r["sizes"][0]


def test_first_touch_under_another_profile_reads_the_import_home(tmp_path):
    """The eager import read the plugins of the home bound at import; a first touch from a turn
    scoped to another profile must not pull that profile's plugins in process-wide."""
    other = tmp_path / "other"
    manifest = other / "plugins" / "platforms" / "zzplat"
    manifest.mkdir(parents=True)
    (manifest / "plugin.yaml").write_text(
        "name: zzplat\nkind: platform\nversion: 0.1.0\nrequires_env:\n  - ZZPLAT_BOT_TOKEN\n")
    r = _py(f"""
        import json
        import hermes_cli.config as c
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        tok = set_hermes_home_override({str(other)!r})
        try:
            seen = "ZZPLAT_BOT_TOKEN" in c.OPTIONAL_ENV_VARS
        finally:
            reset_hermes_home_override(tok)
        print("RESULT " + json.dumps({{"seen": seen}}))
    """, tmp_path)
    assert r["seen"] is False
    # control: the same manifest under the import home is picked up
    import shutil
    shutil.copytree(other / "plugins", tmp_path / "hermes" / "plugins")
    r = _py("""
        import json
        import hermes_cli.config as c
        print("RESULT " + json.dumps({"seen": "ZZPLAT_BOT_TOKEN" in c.OPTIONAL_ENV_VARS}))
    """, tmp_path)
    assert r["seen"] is True


def test_a_failing_stage_is_retried_not_served_half_built(tmp_path):
    r = _py("""
        import json
        import hermes_cli.config as c
        real = c._inject_platform_plugin_env_vars
        calls = []
        def boom():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("manifest read failed")
            return real()
        c._inject_platform_plugin_env_vars = boom
        try:
            len(c.OPTIONAL_ENV_VARS)
            first = "no error"
        except RuntimeError as exc:
            first = str(exc)
        state_after_failure = c._env_registry._state
        n = len(c.OPTIONAL_ENV_VARS)
        print("RESULT " + json.dumps({"first": first, "state": state_after_failure, "calls": len(calls),
                                      "done": c._env_registry.done(), "n": n}))
    """, tmp_path)
    assert r["first"] == "manifest read failed" and r["state"] == 0
    assert r["calls"] == 2 and r["done"] is True
