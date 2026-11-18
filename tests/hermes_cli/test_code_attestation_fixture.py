"""Copied attestation repositories must have no automatic background writers."""
import hashlib
import json
import shutil
import subprocess

from tests.hermes_cli import test_code_attestation_route as route

core = route.core


def test_committed_core_copy_has_no_automatic_housekeeping(core, tmp_path, monkeypatch):
    dest = tmp_path / "copy"
    shutil.copytree(core, dest)
    # Git auto-GC samples bucket 17. Exceed its threshold without hash luck or
    # thousands of objects, while retaining a real commit and object database.
    route._git(dest, "config", "gc.auto", "1")
    found = 0
    for index in range(10000):
        data = f"maintenance regression {index}\n".encode()
        oid = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if oid.startswith("17"):
            (dest / f"maintenance-{found}").write_bytes(data)
            found += 1
            if found == 3:
                break
    assert found == 3
    route._git(dest, "add", "-A")
    trace = tmp_path / "git-trace.jsonl"
    real_run = subprocess.run

    def traced_run(command, **kwargs):
        # The fixture intentionally sanitizes env; trace the actual child after
        # that boundary, rather than relying on inherited GIT_* configuration.
        kwargs["env"] = {**kwargs["env"], "GIT_TRACE2_EVENT": str(trace)}
        return real_run(command, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(route.subprocess, "run", traced_run)
        route._git(dest, "commit", "-qm", "exercise automatic housekeeping")
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    children = [event["argv"] for event in events if event.get("event") == "child_start"]
    assert not any("maintenance" in argv or "gc" in argv for argv in children), children
    copied = tmp_path / "after-commit"
    shutil.copytree(dest, copied)
    assert route._git(copied, "rev-parse", "HEAD") == route._git(dest, "rev-parse", "HEAD")
    route._git(copied, "fsck", "--full")
    assert route._git(copied, "status", "--porcelain") == ""
