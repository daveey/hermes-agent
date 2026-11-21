"""Fixture-only explicit audit injection; production workers never inherit PYTHONPATH."""
from pathlib import Path


def install(site):
    from gateway import capsule_contract
    original = capsule_contract.worker_command
    root = str(Path(__file__).resolve().parents[3])

    def witnessed(policy):
        command = original(policy)
        # Keep worker flags and the original command tail for the audit identity.
        # Load only this test's witness explicitly, without changing the exec env.
        code = ("import runpy,sys; "
                f"sys.path.insert(0, {root!r}); runpy.run_path({str(site)!r}); "
                "sys.argv=sys.argv[2:]; runpy.run_module('agent.managed_worker', run_name='__main__')")
        return [command[0], '-c', code, *command[1:]]

    capsule_contract.worker_command = witnessed
