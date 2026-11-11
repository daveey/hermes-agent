"""Carried briefs exempt exactly verified input files, not gateway state."""
import hashlib
import json
import time

import pytest

from hermes_cli import kanban_carried_evidence as ce
from hermes_cli import kanban_placement as placement


@pytest.fixture
def carried(tmp_path, monkeypatch):
    # Production path shape, isolated filesystem via Path translation.
    home = tmp_path / "home"
    file = home / "kanban/evidence/plan/BRIEF.md"
    file.parent.mkdir(parents=True)
    file.write_text("frozen plan\n")
    name = "/home/example/.hermes/kanban/evidence/plan/BRIEF.md"
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    receipt = {"version": 1, "verified_at": time.time(), "machines": ["worker-1"], "files": {name: digest}}
    manifest = home / "kanban/carried-evidence.json"
    manifest.write_text(json.dumps(receipt))
    monkeypatch.setattr(ce, "get_hermes_home", lambda: home)
    real_path = ce.Path
    monkeypatch.setattr(ce, "Path", lambda value: file if value == name else real_path(value))
    cfg = {"worker_pools": {"hub": ["worker-1"]}}
    body = f"Read {name}, SHA256 {digest}. Implement the approved plan."
    return name, file, manifest, receipt, cfg, body


def test_portable_verified_brief(carried):
    *_, cfg, body = carried
    assert placement.portable_reason({"title": "Implement plan", "body": body}, cfg) is None


@pytest.mark.parametrize("fault", ["missing", "stale", "future", "nan", "uncovered", "bad_shape", "hash", "unpin", "suffix", "symlink", "oversize"])
def test_fail_closed(carried, fault):
    name, file, manifest, receipt, cfg, body = carried
    def symlink():
        target = file.with_name("target")
        file.rename(target)
        file.symlink_to(target)

    mutations = {
        "missing": manifest.unlink,
        "stale": lambda: receipt.update(verified_at=time.time() - 7200),
        "future": lambda: receipt.update(verified_at=time.time() + 100),
        "nan": lambda: receipt.update(verified_at=float("nan")),
        "uncovered": lambda: cfg["worker_pools"]["hub"].append("worker-2"),
        "bad_shape": lambda: receipt.update(files=[]),
        "hash": lambda: file.write_text("changed"),
        "unpin": lambda: None,
        "suffix": lambda: None,
        "symlink": symlink,
        "oversize": lambda: file.write_bytes(b"x" * (ce.MAX_BYTES + 1)),
    }
    mutations[fault]()
    if fault == "unpin":
        body = body.replace(receipt["files"][name], "not pinned")
    if fault == "suffix":
        body = body.replace(name, name + "/child")
    if fault != "missing":
        manifest.write_text(json.dumps(receipt))
    assert placement.portable_reason({"body": body}, cfg) == "home gateway marker"


@pytest.mark.parametrize("extra", ["Read /home/example/.hermes/kanban.db", "Run ssh host", "Edit /home/example/.hermes/kanban/evidence/other.md", "Read /home/example/.hermes/kanban/workspaces/other/report.md"])
def test_other_host_markers_preserved(carried, extra):
    *_, cfg, body = carried
    assert placement.portable_reason({"body": body + "\n" + extra}, cfg) == "home gateway marker"


def test_no_destinations_no_exemption(carried):
    *_, body = carried
    assert ce.without_carried_inputs(body, {}) == body
