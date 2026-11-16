"""A direct restart drain, unlike an idle-only update, waits for the held Kanban turn.

Keep the >180s worker-owner regression separate from the default updater's safe
busy refusal. The fixture cap must exceed the hold; production budgets are unchanged.
"""
from __future__ import annotations

import threading
import time

from gateway.control_socket import pause_gateway_for_update
from tests.e2e.core.upgrade.handoff import _handoff as X
from tests.e2e.core.upgrade.handoff import _scenario as S
from tests.fakes.fake_llm_provider import FakeLLMServer


DRAIN_CAP_S = 300


def test_kanban_worker_outlasts_180_seconds_of_real_gateway_drain(root):
    model = S.Model("long-drain-no-cron")
    with FakeLLMServer(model) as srv, X.cell(
        "head", root, srv.base_url,
        extra={"agent": {"restart_after_turn_timeout": DRAIN_CAP_S},
               "kanban": {"dispatch_interval_seconds": 3}},
    ) as inst:
        before = X.start_gateway(inst)
        card = S.create_card(inst, "finish once across a long gateway drain")
        assert model.holding.wait(timeout=240) and model.hold == card, inst.diagnostics()
        releaser = threading.Thread(target=S._release_inside_the_drain, args=(inst, model), daemon=True)
        with S.WorkerSampler(inst) as sampler:
            releaser.start()
            started = time.monotonic()
            try:
                ack = pause_gateway_for_update(inst.hermes_home)
                assert ack and ack.get("pausing") and ack.get("pid") == before["pid"], ack
                X.wait_for(lambda: S.card_status(inst, card) == "done", timeout=DRAIN_CAP_S,
                           what="the held worker to complete during the drain")
                assert time.monotonic() - started >= S.HOLD_INTO_DRAIN_S, "the held turn ended before 210s"
                X.wait_for(lambda: not X.gateway_pids(inst), timeout=90, what="the drained gateway to exit")
            finally:
                model.release.set()
                releaser.join(timeout=5)
        log = (inst.hermes_home / "logs" / "gateway.log").read_text(encoding="utf-8")
        assert "Restart deferred wait complete" in log, inst.diagnostics()
        assert "Restart after-turn wait timed out" not in log, inst.diagnostics()
        assert not X.restart_wait_verdict(inst, cap_s=DRAIN_CAP_S), inst.diagnostics()
        runs = S._kanban(inst, "SELECT outcome FROM task_runs WHERE task_id = ? ORDER BY id", (card,))
        assert [r["outcome"] for r in runs] == ["completed"], (runs, S.worker_logs(inst))
        assert model.completes[card] == 1
        assert sampler.peak[card] == 1, sampler.evidence
