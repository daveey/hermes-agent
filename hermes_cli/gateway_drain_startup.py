"""Cold gateway boundary: bootstrap observers, then drain capability, before work."""


def begin_gateway_process():
    from hermes_cli.observability.shared_metrics_process import begin_process
    from hermes_cli.worker_drain_capability import start_daemon_capability

    begin_process('gateway')
    start_daemon_capability()
