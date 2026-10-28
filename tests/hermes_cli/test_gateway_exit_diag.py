"""``logs/gateway-exit-diag.log`` records carry a ``level`` so log shippers grade them by intent.

A supervised gateway restarts by exiting 75 (SIGUSR1 drain, ``hermes update``, ``/restart``); its
``asyncio.run.SystemExit`` record used to carry only a traceback, which log shippers that grade
JSON lines by keywords filed as an error on every routine restart (Baby Bear mini, BB-59).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
from hermes_cli.gateway_exit_diag import make_exit_diag


def _rows() -> list[dict]:
    path = Path(os.environ["HERMES_HOME"]) / "logs" / "gateway-exit-diag.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("code", [None, 0, GATEWAY_SERVICE_RESTART_EXIT_CODE])
def test_planned_system_exit_is_info(code):
    make_exit_diag()("asyncio.run.SystemExit", code=code, traceback="Traceback (most recent call last): ...")
    assert _rows()[-1]["level"] == "info"


@pytest.mark.parametrize("code", [1, 2, "fatal"])
def test_failing_system_exit_is_error(code):
    make_exit_diag()("asyncio.run.SystemExit", code=code, traceback="Traceback (most recent call last): ...")
    assert _rows()[-1]["level"] == "error"


@pytest.mark.parametrize(
    ("tag", "level"),
    [
        ("gateway.start", "info"),
        ("asyncio.run.returned", "info"),
        ("gateway.exit_clean", "info"),
        ("atexit.hook", "info"),
        ("gateway.exit_nonzero", "warning"),
        ("gateway.exit_signal", "info"),
        ("asyncio.run.exception", "error"),
    ],
)
def test_lifecycle_records_carry_their_level(tag, level):
    make_exit_diag()(tag)
    assert _rows()[-1]["level"] == level


def test_opt_out_writes_nothing(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_EXIT_DIAG", "0")
    make_exit_diag()("gateway.start")
    assert not (Path(os.environ["HERMES_HOME"]) / "logs" / "gateway-exit-diag.log").exists()
