"""Process-table coverage for legacy workers and owner receipt clients."""
from types import SimpleNamespace

from tests.e2e.core.upgrade.handoff import _scenario as S


def test_sampler_counts_receipt_clients_and_legacy_workers_without_descendant_duplicates():
    card = "t_0123abc"
    def proc(pid, ppid, argv, *, state="S", task=card):
        return {"pid": pid, "ppid": ppid, "cmdline": argv, "state": state,
                "env": {"HERMES_KANBAN_TASK": task}}

    client = ["python", "-m", "hermes_cli.kanban_worker_client"]
    rows = [proc(10, 1, client), proc(11, 10, client),
            proc(12, 1, ["python", "other"]),
            proc(13, 1, client, state="Z"), proc(14, 1, client, task="")]
    sampler = S.WorkerSampler(SimpleNamespace(host=SimpleNamespace(procs=lambda: rows)))
    sampler._sample()
    assert sampler.peak[card] == 1
    rows.append(proc(20, 1, client))
    sampler._sample()
    assert sampler.peak[card] == 2
    assert sampler.evidence[card][1] == [10, 20]
    rows[:] = [proc(30, 1, ["hermes", "chat", "-q", "work kanban task t_abcdef"])]
    sampler._sample()
    assert sampler.peak["t_abcdef"] == 1


def test_process_probe_carries_task_identity_but_not_secrets(monkeypatch):
    from tests.e2e.core.upgrade.handoff import _ns_agent as agent

    monkeypatch.setattr(agent.os, "listdir", lambda path: ["42"])
    monkeypatch.setattr(agent, "_stat", lambda pid: ["S", "1"] + ["0"] * 18)
    payloads = {"environ": b"HERMES_KANBAN_TASK=t_0123abc\0API_KEY=secret\0",
                "cmdline": b"python\0-m\0hermes_cli.kanban_worker_client\0"}
    monkeypatch.setattr(agent, "_read", lambda path: payloads[path.rsplit("/", 1)[1]])
    rows = agent._procs()
    assert rows[0]["env"] == {"HERMES_KANBAN_TASK": "t_0123abc"}
    sampler = S.WorkerSampler(SimpleNamespace(host=SimpleNamespace(procs=lambda: rows)))
    sampler._sample()
    assert sampler.peak["t_0123abc"] == 1
