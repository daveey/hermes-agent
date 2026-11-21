"""Subprocess-only fault seams for test_enrollment_cli_create.py.

The real main parser and create handler are never patched. Only external registry
input, the full-integrity timing seam, and stdout's write boundary are controlled.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def main():
    mode, marker, *argv = sys.argv[1:]
    marker = Path(marker)

    # Children do not inherit pytest's network fence. Refuse even loopback: this
    # suite needs no network. An audit hook cannot be silently unpatched by imports.
    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname"}:
            marker.with_suffix(".forbidden").write_text(f"{event} {args!r}")
            raise AssertionError(f"enrollment CLI attempted network: {event} {args!r}")
        if event == "subprocess.Popen":
            executable = str(args[0])
            # The real entrypoint records core provenance using this read-only
            # command. It must not be mistaken for a model/service subprocess.
            git_head = (Path(executable).name == "git" and list(args[1])[1:] in [
                ["-c", "gc.autoDetach=false", "-c", "maintenance.auto=false", "rev-parse", "HEAD"],
                ["-C", str(REPO), "rev-parse", "HEAD"],
                ["rev-parse", "HEAD"],
            ])
            if Path(executable).resolve() != Path(sys.executable).resolve() and not git_head:
                marker.with_suffix(".forbidden").write_text(f"{event} {args!r}")
                raise AssertionError(f"enrollment CLI attempted external command: {args!r}")
    sys.addaudithook(audit)

    from hermes_cli.worker_compute_capacity import policy_root
    assert policy_root() == Path(os.environ["HERMES_TEST_POLICY_ROOT"]), "policy isolation refused"

    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_worker_node as wn

    # Registry service is the only remote dependency replaced. The pin guard,
    # capability validator, SQL capture, parser, and create handler remain real.
    wn.set_registry_fetch(lambda: {"me": "enrollment-cli-origin", "machines": [
        {"name": "enrollment-cli-origin", "state": "active", "labels": ["linux"], "hold": False},
        {"name": "enrollment-cli-worker", "state": "active", "labels": ["linux"],
         "pickup": "all", "hold": False},
    ]})

    if mode == "seed":
        from hermes_cli import kanban_db_sync as sync
        with kbc.connect_closing() as conn:
            sync.set_identity(conn, "default", "enrollment-cli-origin")
            sync.set_mode(conn, "primary")
        return

    # Always run the real integrity check in this harness; a TTL stamp is NOT a
    # correctness oracle and must not hide our injected slow/hung integrity seam.
    if not mode.startswith("cached"):
        kbc._INTEGRITY_STAMP_TTL_SECONDS = 0
    real_check = kbc._run_integrity_check

    if mode.startswith("cached") and hasattr(kbc, "_integrity_generation"):
        # Observe the production detector without changing its answer, metadata,
        # stamp, or connection lifecycle. Cache misses must name the generation
        # mismatch rather than assume that a read-only command is byte-quiescent.
        real_generation = kbc._integrity_generation

        def observed_generation(path):
            generation = real_generation(path)
            try:
                stamp = json.loads(kbc._integrity_stamp_path(path).read_text())
            except (OSError, ValueError):
                stamp = None
            with marker.with_suffix(".generations").open("a") as log:
                log.write(json.dumps({"generation": generation, "stamp": stamp}) + "\n")
            return generation

        kbc._integrity_generation = observed_generation

    def checked(conn):
        marker.with_suffix(".integrity-entered").write_text(str(os.getpid()))
        if mode in {"integrity-gate", "cached-gate"}:
            from tests.waiting import wait_until
            assert wait_until(lambda: marker.with_suffix(".release").exists(), timeout=600)
        result = real_check(conn)
        marker.with_suffix(".integrity-done").write_text(json.dumps(result))
        return result
    kbc._run_integrity_check = checked

    if mode == "output-hang":
        real_stdout = sys.stdout

        class BlockedOutput:
            def write(self, value):
                if value.lstrip().startswith("{"):
                    marker.with_suffix(".output-blocked").write_text(str(os.getpid()))
                    # health: allow HX013 -- a post-commit response hang is the fault under test; parent kills and reaps this owned child
                    threading.Event().wait(600)
                return real_stdout.write(value)

            def __getattr__(self, name):
                return getattr(real_stdout, name)
        sys.stdout = BlockedOutput()

    sys.argv = ["hermes", *argv]
    from hermes_cli.main import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
