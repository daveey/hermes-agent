"""Dashboard server import/auth/start seam, held under early capture custody."""


def run_server(args, *, headless, owner, prepare_auth, bootstrap, mcp_after_bind):
    from hermes_cli.web_server import start_server

    # TTY-only interactive auth setup; noninteractive callers retain fail-closed
    # start_server behavior. Plugins were already discovered before early capture.
    prepare_auth(args)
    if headless:
        from hermes_cli.observability.shared_metrics_process import begin_process
        begin_process('serve')
    start_server(
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        allow_public=getattr(args, 'insecure', False),
        initial_profile=getattr(args, 'open_profile', '') or '',
        headless=headless,
        isolated=getattr(args, 'isolated', False),
        **bootstrap,
        start_mcp_discovery_after_bind=mcp_after_bind,
        early_sessions_owner=owner,
    )
