"""An offline checkout that runs the REAL ``scripts/run_tests.sh`` chain.

``tests.pm.activation_support.isolated_checkout`` supplies the real PM package
and activation scripts. This module adds what the test runner needs on top:
the runner scripts and the one ``hermes_cli`` module they import, a git index
(so the runner's bytecode warm-up compiles tracked files and not ``sys.path``),
and an *offline provisioner* standing in for ``setup-hermes.sh``.

The provisioner is a fixture, not proof of a locked install. It publishes what
activation reads and nothing it would have to download: PM facts naming a
committed venv, a ``pm.testenv`` selection (``active.json`` ``gen-<32 hex>`` with
``pyvenv.cfg`` and an interpreter that direct-execs ``sys.executable``), and the
real ``record_activation_inputs`` stamps. Everything downstream is the real
code: ``run_tests.sh`` -> ``run-in-hermes-env`` -> ``_activation.sh`` ->
``python -m pm.environments`` -> the ``env -i`` runner -> a pytest child.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from tests.pm.activation_support import (
    REPO_ROOT,
    bash,
    bash_env,
    fake_store,
    isolated_checkout,
    posix,
    spawnable_python,
)

RUNNER_FILES = (
    "scripts/run_tests.sh",
    "scripts/run-in-hermes-env",
    "scripts/run_tests_parallel.py",
    "scripts/run_tests_parallel_rss.py",
    "scripts/run_tests_host_slots.py",
    "scripts/run_tests_launch.py",
    "scripts/run_tests_fork_child.py",
    "scripts/worker_test_budget.py",
    "scripts/ci/list_os_marked_tests.py",
    # worker_enabled() is called by the runner's -j parser and the slot code.
    "hermes_cli/worker_compute_capacity.py",
    "hermes_cli/config_backend.py",
    "utils.py",
    "hermes_yaml.py",
    "hermes_constants_scratch.py",
    "hermes_constants_scratch_stuck.py",
)

GENERATION = "gen-" + "0123456789abcdef" * 2
# Names the harness must never hand a child, and the canary's synthetic secrets.
SECRETS = {
    "OPENAI_API_KEY": "sk-synthetic-openai-harness94",
    "ANTHROPIC_API_KEY": "sk-ant-synthetic-harness94",
    "HERMES_TEST_SECRET_SENTINEL": "synthetic-sentinel-harness94",
}
# Inherited activation / interpreter / runner state a fixture run must not see.
SANITIZED = (
    "__HERMES_ACTIVATED", "__HERMES_TEST_PYTHON", "__HERMES_TESTS_REEXEC", "HERMES_PYTHON",
    "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "BASH_ENV", "ENV", "PYTHONSTARTUP",
    "MALLOC_ARENA_MAX", "UV_PYTHON_INSTALL_DIR", "HERMES_TEST_POLICY_ROOT",
    "HERMES_TEST_SLOT_HELD", "HERMES_TEST_SLOT_RECEIPT", "HERMES_TEST_SLOT_CONFIG",
    "HERMES_TEST_SLOT_MAX_AGE", "HERMES_TEST_XDIST_MAX", "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
    "PYTEST_CURRENT_TEST", "HERMES_TEST_WORKERS", "HERMES_TEST_PATHS", "HERMES_TEST_SLICE",
    "HERMES_TEST_FLAKY_REPORT", "HERMES_TEST_FILE_TIMEOUT", "HERMES_TEST_REQUIRE_EXTRAS",
)

PROVISIONER = textwrap.dedent('''\
    """Offline stand-in for setup-hermes.sh --runtime-only --test-environment."""
    import json, os, pathlib, stat, sys

    root = pathlib.Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    from pm.environments import (
        activation_input_mtimes, activation_inputs_dir, record_activation_inputs,
        runtime_facts_path, site_packages,
    )
    from pm.testenv import testenv_root

    config = json.loads((root / "provision.json").read_text(encoding="utf-8"))
    with (root / "setup-calls.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"argv": sys.argv[1:], "malloc": os.environ.get("MALLOC_ARENA_MAX")}) + "\\n")
    assert sys.argv[1:] == ["--runtime-only", "--test-environment"], sys.argv

    facts = runtime_facts_path(root)
    selected = facts.parent / "environments" / "app" / "venv"
    site_packages(selected).mkdir(parents=True, exist_ok=True)
    (selected / "pyvenv.cfg").write_text("home = fixture\\n", encoding="utf-8")
    facts.write_text(json.dumps({"packages": {"venv": {"environment": str(selected)}}}), encoding="utf-8")

    generation = testenv_root(root) / config["generation"] / "venv"
    interpreter = generation / "bin" / "python"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    (generation / "pyvenv.cfg").write_text("home = fixture\\n", encoding="utf-8")
    interpreter.write_text("#!/bin/sh\\nexec " + config["quoted_python"] + ' "$@"\\n', encoding="utf-8")
    interpreter.chmod(interpreter.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (testenv_root(root) / "active.json").write_text(
        json.dumps({"generation": config["generation"]}), encoding="utf-8")

    mtimes = {name: value + config["stamp_skew_ns"] for name, value in activation_input_mtimes(root).items()}
    record_activation_inputs(activation_inputs_dir(root), mtimes, root, test_environment=True)
''')


def runner_checkout(tmp_path: Path, *, stamp_skew_ns: int = 0) -> tuple[Path, dict, Path]:
    """Return ``(checkout, env, store)``: a checkout, its sanitised environment, its store."""
    store, _entry = fake_store(tmp_path)
    root = isolated_checkout(tmp_path)
    for relative in RUNNER_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, target)
    # pm/lock.json is the one activation input the checkout carries.
    assert (root / "pm" / "lock.json").is_file()

    python = spawnable_python()
    bootstrap = root / ".venv" / "bin" / "python"
    bootstrap.parent.mkdir(parents=True)
    log = tmp_path / "bootstrap-calls.log"
    bootstrap.write_text(
        f"#!/bin/sh\necho \"$$ $*\" >> {shlex.quote(str(log))}\nexec {shlex.quote(str(python))} \"$@\"\n",
        encoding="utf-8")
    bootstrap.chmod(0o755)

    (root / "provision.py").write_text(PROVISIONER, encoding="utf-8")
    set_provisioning(root, stamp_skew_ns=stamp_skew_ns)
    (root / "setup-hermes.sh").write_text(
        'test "$#" = 2 && test "$1" = --runtime-only && test "$2" = --test-environment || exit 2\n'
        f"cd {shlex.quote(str(root))} || exit 3\n"
        f"exec {shlex.quote(str(python))} provision.py \"$@\"\n", encoding="utf-8")

    env = sanitized_env(tmp_path, store)
    # Tracked files give the runner's `git ls-files` warm-up something finite to compile.
    git = shutil.which("git") or "git"
    subprocess.run([git, "init", "-q"], cwd=root, env=env, check=True, capture_output=True, timeout=60)
    subprocess.run([git, "add", "-A"], cwd=root, env=env, check=True, capture_output=True, timeout=60)
    return root, env, store


def set_provisioning(root: Path, *, stamp_skew_ns: int) -> None:
    """``stamp_skew_ns`` != 0 makes every sync record stamps that never match the inputs."""
    (root / "provision.json").write_text(json.dumps({
        "generation": GENERATION,
        "quoted_python": shlex.quote(sys.executable),
        "stamp_skew_ns": stamp_skew_ns,
    }), encoding="utf-8")


def sanitized_env(tmp_path: Path, store: Path) -> dict:
    env = bash_env(store)
    for key in SANITIZED:
        env.pop(key, None)
    for key in [k for k in env if k.startswith(("HERMES_E2E_", "HERMES_TEST_SSH_", "HERMES_TEST_DOCKER"))]:
        env.pop(key)
    slots = tmp_path / "host-slots"
    env.update(
        SECRETS,
        HERMES_PM_ACCOUNT_SEED="0",           # never borrow the account's tool store
        HERMES_RUNTIME_DIR=posix(store),      # an explicit store also disables borrowing
        HERMES_TEST_FILE_RETRIES="0",
        HERMES_TEST_FORKSERVER="0",
        HERMES_TEST_HOST_SLOTS="2",
        HERMES_TEST_SLOT_DIR=posix(slots),    # private: never wait on the outer run's slots
    )
    return env


def explicit_python(tmp_path: Path) -> Path:
    """An explicit ``HERMES_PYTHON`` that has pytest and records every launch."""
    wrapper = tmp_path / "explicit-python"
    log = tmp_path / "explicit-python-calls.log"
    wrapper.write_text(
        f"#!/bin/sh\necho \"$$ $*\" >> {shlex.quote(str(log))}\nexec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def provision(root: Path, env: dict) -> None:
    """Run the offline setup once, as a prior activation would have."""
    run = subprocess.run([bash(), str(root / "setup-hermes.sh"), "--runtime-only", "--test-environment"],
                         cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert run.returncode == 0, run.stdout + run.stderr


def composed_env(root: Path, env: dict) -> dict:
    """The environment ``activate`` would give a shell: PM's own ``python -m pm.environments`` output."""
    python = root / ".venv" / "bin" / "python"
    run = subprocess.run([str(python), "-m", "pm.environments", "--format", "json"],
                         cwd=root, env={**env, "PYTHONPATH": str(root)}, capture_output=True, text=True,
                         timeout=120, check=False)
    assert run.returncode == 0, run.stdout + run.stderr
    composed = json.loads(run.stdout)
    assert composed["__HERMES_ACTIVATED"] and composed["__HERMES_TEST_PYTHON"], composed
    return composed


def count_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


# Source of the child-side observation, shared so the test measures itself the same way.
OBSERVE = textwrap.dedent('''\
    def observe():
        import os, resource, sys
        from pathlib import Path
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8").strip()
        limit = None
        for line in cgroup.splitlines():
            if line.startswith("0::"):
                target = Path("/sys/fs/cgroup") / line[3:].lstrip("/") / "memory.max"
                limit = target.read_text(encoding="utf-8").strip() if target.is_file() else None
        return {"rlimit_as": list(resource.getrlimit(resource.RLIMIT_AS)), "cgroup_memory_max": limit,
                "executable": sys.executable}
''')


def write_canary(directory: Path, observed: Path) -> Path:
    """A test file OUTSIDE the repository, so no repo conftest touches it. It appends what its own
    process saw to ``observed`` (a path embedded in the source), one JSON line per execution."""
    directory.mkdir(parents=True, exist_ok=True)
    observed.parent.mkdir(parents=True, exist_ok=True)
    case = directory / "test_allocator_canary.py"
    case.write_text(
        "import json, os\nfrom pathlib import Path\n\n"
        + OBSERVE
        + "\n\ndef test_observe_child_environment():\n"
        f"    record = {{'env': dict(os.environ), **observe()}}\n"
        f"    with Path({str(observed)!r}).open('a', encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps(record) + '\\n')\n",
        encoding="utf-8")
    return case


def observed_records(observed: Path) -> list[dict]:
    if not observed.exists():
        return []
    return [json.loads(line) for line in observed.read_text(encoding="utf-8").splitlines()]


def own_observation() -> dict:
    scope: dict = {}
    exec(OBSERVE, scope)  # the same source the canary runs
    return scope["observe"]()


def runner_command(root: Path, case: Path) -> list[str]:
    return [bash(), str(root / "scripts" / "run_tests.sh"), "-j", "1", str(case)]

