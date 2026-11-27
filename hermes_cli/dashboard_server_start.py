"""Dashboard server import/auth/start seam, held under early capture custody."""


def prepare_and_run_server(args, *, headless, prepare_runtime, prepare_auth, bootstrap):
    from contextlib import nullcontext
    from hermes_cli.serve_bootstrap_listener import BootstrapListener
    from hermes_cli.sessions_early_boot import early_sessions
    from hermes_cli.version_info import prewarm_version_info

    prewarm_version_info()  # git waits overlap runtime preparation; web_server joins the result
    listener = BootstrapListener(args.host, args.port) if headless else nullcontext(None)
    with listener as bootstrap_listener:
        with early_sessions(headless=headless, isolated=getattr(args, 'isolated', False)) as owner:
            mcp_after_bind = prepare_runtime(args, headless)
            if bootstrap_listener is not None:
                bootstrap['bootstrap_listener'] = bootstrap_listener
            run_server(args, headless=headless, owner=owner, prepare_auth=prepare_auth,
                       bootstrap=bootstrap, mcp_after_bind=mcp_after_bind)


def run_server(args, *, headless, owner, prepare_auth, bootstrap, mcp_after_bind):
    from hermes_cli.web_server import start_server

    # TTY-only interactive auth setup; noninteractive callers retain fail-closed
    # start_server behavior. Plugins were discovered while early capture ran.
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
