"""[daveey] The core-update pause request: hermes-sync's drain window asks auto-continuable Desktop turns to
pause at their next step boundary. The request is ``<root>/core-update/pause-request`` beside the admission
hold, and it only counts while that hold stands and before its ``expires_at`` (scoped to one window)."""

from __future__ import annotations

import json
import time

from hermes_cli import admission_fence as af


def _hold(root):
    af.set_hold(root)


def test_no_request_file_means_no_pause(tmp_path):
    _hold(tmp_path)
    assert af.pause_requested(tmp_path) is False


def test_request_under_the_hold_is_active(tmp_path):
    _hold(tmp_path)
    af.request_pause(tmp_path, expires_at=time.time() + 60)
    assert af.pause_requested(tmp_path) is True
    body = json.loads((tmp_path / "core-update" / "pause-request").read_text())
    assert body["version"] == 1 and body["expires_at"] > time.time()


def test_bom_prefixed_request_under_hold_is_active(tmp_path):
    _hold(tmp_path)
    request = tmp_path / "core-update" / "pause-request"
    body = json.dumps({"version": 1, "expires_at": time.time() + 60})
    request.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    assert af.pause_requested(tmp_path) is True


def test_request_without_the_hold_is_ignored(tmp_path):
    """A stale file a crashed drainer left behind must not pause turns once its window is over."""
    af.request_pause(tmp_path, expires_at=time.time() + 60)
    assert af.pause_requested(tmp_path) is False


def test_expired_request_is_ignored(tmp_path):
    _hold(tmp_path)
    af.request_pause(tmp_path, expires_at=time.time() - 1)
    assert af.pause_requested(tmp_path) is False


def test_unreadable_request_is_ignored(tmp_path):
    _hold(tmp_path)
    (tmp_path / "core-update" / "pause-request").write_text("{not json")
    assert af.pause_requested(tmp_path) is False


def test_clear_removes_the_request(tmp_path):
    _hold(tmp_path)
    af.request_pause(tmp_path, expires_at=time.time() + 60)
    af.clear_pause_request(tmp_path)
    assert af.pause_requested(tmp_path) is False
    af.clear_pause_request(tmp_path)  # idempotent


# ── [daveey] "Force update": the drainer stamps ``"force": true`` on the pause request ────────────────


def test_forced_request_under_the_hold_is_forced(tmp_path):
    _hold(tmp_path)
    af.request_pause(tmp_path, expires_at=time.time() + 60, force=True)
    assert af.pause_requested(tmp_path) is True and af.pause_forced(tmp_path) is True
    body = json.loads((tmp_path / "core-update" / "pause-request").read_text())
    assert body["force"] is True


def test_unforced_request_is_not_forced(tmp_path):
    _hold(tmp_path)
    af.request_pause(tmp_path, expires_at=time.time() + 60)
    assert af.pause_requested(tmp_path) is True and af.pause_forced(tmp_path) is False
    assert "force" not in json.loads((tmp_path / "core-update" / "pause-request").read_text())


def test_force_must_be_literally_true(tmp_path):
    _hold(tmp_path)
    (tmp_path / "core-update" / "pause-request").write_text(
        json.dumps({"version": 1, "expires_at": time.time() + 60, "force": "yes"}))
    assert af.pause_requested(tmp_path) is True and af.pause_forced(tmp_path) is False


def test_expired_forced_request_is_not_forced(tmp_path):
    _hold(tmp_path)
    af.request_pause(tmp_path, expires_at=time.time() - 1, force=True)
    assert af.pause_forced(tmp_path) is False


def test_forced_request_without_the_hold_is_not_forced(tmp_path):
    af.request_pause(tmp_path, expires_at=time.time() + 60, force=True)
    assert af.pause_forced(tmp_path) is False


def test_forced_check_never_raises_on_garbage(tmp_path):
    _hold(tmp_path)
    (tmp_path / "core-update" / "pause-request").write_text("[1, 2]")
    assert af.pause_forced(tmp_path) is False
    assert af.pause_forced(tmp_path / "missing") is False
