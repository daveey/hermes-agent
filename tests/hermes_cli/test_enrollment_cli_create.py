"""Real main() CLI create regression harness for enrollment's ambiguous outcomes.

Run: bash scripts/run_tests.sh tests/hermes_cli/test_enrollment_cli_create.py -v

Scaler-equivalent canary/file_card argv (hermes_worker_scaler.HubCards), but all
state is disposable and registry input is fixture-owned. No scaler lifecycle,
service enrollment, Board HTTP synchronization, live board, or model is exercised.
These tests prove timeout/retry semantics, NOT that PR3945's stamp is safe or
that connect-only latency/count tests establish production create fairness.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys

import pytest

from tests.waiting import wait_until

REPO = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).with_name("enrollment_cli_child.py")
pytestmark = pytest.mark.platforms("posix")


def create_argv(kind, key):
    # Same argument ordering and options as scaler HubCards.canary/file_card.
    # No model pin: this is its supported unpinned route (route=None).
    argv = ["kanban", "--board", "default", "create", f"worker-scaler {kind}: isolated enrollment",
            "--body", f"Isolated enrollment CLI regression ({kind}); no worker executes this card.",
            "--assignee", "default"]
    if kind == "canary":
        argv += ["--runs-on", "machine:enrollment-cli-worker"]
    argv += ["--requires", "terminal"]
    if kind == "canary":
        argv += ["--max-runtime", "600"]
    return argv + ["--idempotency-key", key, "--json"]


class Sandbox:
    def __init__(self, root):
        self.root = root
        self.home = root / "home"
        self.hermes = self.home / ".hermes"
        self.hermes.mkdir(parents=True)
        self.temp = root / "tmp"
        self.temp.mkdir()
        self.policy = self.temp / "policy"
        self.policy.mkdir()
        self.db = self.hermes / "kanban.db"
        (self.hermes / "config.yaml").write_text(
            "terminal:\n  backend: local\nplatform_toolsets:\n  cli: [terminal, file, kanban]\n"
            "kanban:\n  sync:\n    enabled: true\n  capabilities:\n    builtin: false\n")
        mail = self.home / ".config/hermes-mail"
        mail.mkdir(parents=True)
        (mail / "config.json").write_text(json.dumps({"gateway": "enrollment-cli-origin"}))
        # Allowlist, not a blacklist: no agent, worker, hub, auth, proxy, network,
        # terminal overlay, profile activation, or user plugin environment survives.
        self.env = {"HOME": str(self.home), "HERMES_HOME": str(self.hermes),
                    "HERMES_TEST_POLICY_ROOT": str(self.policy),
                    # This is an already-provisioned CLI test, not an updater or
                    # default-plugin installation test. Use production opt-outs:
                    # do not sync tools/plugins or borrow the real account's PM store.
                    "HERMES_DISABLE_LAZY_INSTALLS": "1", "HERMES_PM_ACCOUNT_SEED": "0",
                    "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "XDG_CACHE_HOME": str(self.home / ".cache"),
                    "XDG_STATE_HOME": str(self.home / ".local/state"),
                    "TMPDIR": str(self.temp), "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
                    "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONHASHSEED": "0", "LANG": "C.UTF-8", "TZ": "UTC"}
        self.children = []
        seed = self.start("seed", "seed", [])
        try:
            out, err = seed.communicate(timeout=30)
        except BaseException:
            self.stop(seed)
            raise
        assert seed.returncode == 0, (out, err)
        # Seed is not the subject; make the first subject enter the real probe.
        self.db.with_name(self.db.name + ".integrity-ok").unlink(missing_ok=True)

    def start(self, mode, name, argv):
        marker = self.root / name
        p = subprocess.Popen([sys.executable, str(CHILD), mode, str(marker), *argv],
                             cwd=REPO, env=self.env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True)
        self.children.append(p)
        return p

    def stop(self, p):
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
        return p.communicate(timeout=10)

    def rows(self):
        with sqlite3.connect(f"file:{self.db}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]

    def events(self):
        with sqlite3.connect(f"file:{self.db}?mode=ro", uri=True) as conn:
            return conn.execute("SELECT task_id, kind, payload FROM task_events ORDER BY id").fetchall()

    def success(self, name, argv):
        p = self.start("normal", name, argv)
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, (out, err)
        result = json.loads(out)
        assert result["id"], (out, err)
        return result


@pytest.fixture
def sandbox(tmp_path):
    # Use pytest-owned disposable scratch. The canonical runner relocates this
    # to /var/tmp and home_io_guard forbids the real account's .hermes scratch.
    box = Sandbox(tmp_path)
    try:
        yield box
    finally:
        for p in box.children:
            box.stop(p)
        assert all(p.poll() is not None for p in box.children)
        forbidden = list(box.root.glob("*.forbidden"))
        assert not forbidden, json.dumps({p.name: p.read_text() for p in forbidden}, indent=2)


def await_marker(box, name, suffix, p):
    marker = box.root / name
    target = marker.with_suffix(suffix)
    assert wait_until(lambda: target.exists() or p.poll() is not None, timeout=30)
    if not target.exists():
        out, err = p.communicate(timeout=5)
        pytest.fail(f"CLI exited before {suffix}: rc={p.returncode}\n{out}\n{err}")
    return marker


def test_slow_integrity_completes_actual_cli_create_only_after_real_check(sandbox):
    box = sandbox
    argv = create_argv("canary", "worker-scaler:canary:i-isolated-slow")
    p = box.start("integrity-gate", "slow", argv)
    marker = await_marker(box, "slow", ".integrity-entered", p)
    assert box.rows() == []
    assert not marker.with_suffix(".integrity-done").exists()
    marker.with_suffix(".release").touch()
    out, err = p.communicate(timeout=30)
    assert p.returncode == 0, (out, err)
    assert json.loads(marker.with_suffix(".integrity-done").read_text()) == ["ok"]
    card = json.loads(out)
    rows = box.rows()
    assert len(rows) == 1 and rows[0]["id"] == card["id"]
    assert json.loads(rows[0]["runs_on"]) == ["machine:enrollment-cli-worker"]
    assert rows[0]["max_runtime_seconds"] == 600


def test_hung_integrity_timeout_has_no_commit_and_restart_retry_succeeds(sandbox):
    box = sandbox
    argv = create_argv("canary", "worker-scaler:canary:i-isolated-hung")
    p = box.start("integrity-gate", "hung", argv)
    await_marker(box, "hung", ".integrity-entered", p)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            p.communicate(timeout=2)
    finally:
        out, _err = box.stop(p)
    assert p.returncode < 0 and out == ""
    assert box.rows() == []
    card = box.success("hung-retry", argv)
    assert [r["id"] for r in box.rows()] == [card["id"]]
    assert box.success("hung-retry-again", argv)["id"] == card["id"]
    assert len(box.rows()) == 1


@pytest.mark.parametrize("kind,key", [
    ("canary", "worker-scaler:canary:i-isolated-output"),
    ("failure-report", "worker-scaler:stuck:enrollment-cli-worker:CANARY:i-isolated-output"),
])
def test_committed_create_blocked_output_timeout_restart_preserves_single_key(sandbox, kind, key):
    box = sandbox
    argv = create_argv(kind, key)
    p = box.start("output-hang", "response", argv)
    await_marker(box, "response", ".output-blocked", p)
    before = box.rows()
    events_before = box.events()
    # Independent read proves commit before response; not inferred from a seam.
    assert len(before) == 1 and before[0]["idempotency_key"] == key
    assert before[0]["session_id"] is None
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            p.communicate(timeout=2)
    finally:
        out, _err = box.stop(p)
    assert out == "" and p.returncode < 0
    assert box.rows() == before
    retry = box.success("response-retry", argv)
    assert retry["id"] == before[0]["id"]
    assert box.success("response-retry-again", argv)["id"] == retry["id"]
    assert box.rows() == before  # immutable original card, not just equal count
    assert box.events() == events_before  # no duplicate created/assignment events


def test_concurrent_real_cli_same_create_key_returns_one_committed_card(sandbox):
    box = sandbox
    key = "worker-scaler:canary:i-isolated-concurrent"
    argv = create_argv("canary", key)
    children = [box.start("normal", f"parallel-{i}", argv) for i in range(4)]
    ids = []
    for child in children:
        out, err = child.communicate(timeout=30)
        assert child.returncode == 0, (out, err)
        ids.append(json.loads(out)["id"])
    assert len(set(ids)) == 1
    rows = box.rows()
    assert len(rows) == 1 and rows[0]["id"] == ids[0]
    assert rows[0]["idempotency_key"] == key


@pytest.mark.parametrize("kind", ["canary", "failure-report"])
def test_actual_create_reuses_unchanged_verified_generation_within_budget(sandbox, kind):
    """Old full CLI enters the blocked probe and times out; unchanged verified DB needs no second scan.

    This is an injected scan-stall boundary on a quiescent board, not a large-DB
    benchmark or an assertion that a continuously written board obtains cache hits.
    """
    box = sandbox
    warm_argv = create_argv("canary", "worker-scaler:canary:i-warm")
    # The first observer's real probe closes the last SQLite connection and
    # checkpoints the warm create's WAL. That changes the main-file generation
    # during verification, so production correctly refuses to publish its stamp.
    # A second real observer verifies that now-quiescent generation. Do not fake
    # a stamp, hold a reader open, or reset timestamps to manufacture a cache hit.
    for i in range(3):  # create, checkpointing observer, verifying observer
        p = box.start("cached", f"warm-{i}", warm_argv)
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, (out, err)
        if i == 0:
            warm_argv = ["kanban", "--board", "default", "show", json.loads(out)["id"], "--json"]
    # Both observers must have exercised the real probe: the first cannot stamp
    # a checkpoint-changing generation; the second establishes the cache premise.
    assert (box.root / "warm-1.integrity-done").exists()
    assert (box.root / "warm-2.integrity-done").exists()
    argv = create_argv(kind, f"worker-scaler:{kind}:i-budget")
    p = box.start("cached-gate", "budget", argv)
    try:
        out, err = p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        box.stop(p)
        traces = {p.name: [json.loads(line) for line in p.read_text().splitlines()]
                  for p in sorted(box.root.glob("*.generations"))}
        pytest.fail("actual create repeated the full probe; generation/stamp observations:\n"
                    + json.dumps(traces, indent=2))
    assert p.returncode == 0, (out, err)
    assert not (box.root / "budget.integrity-entered").exists()
    assert json.loads(out)["id"] in {row["id"] for row in box.rows()}
    assert len(box.rows()) == 2
