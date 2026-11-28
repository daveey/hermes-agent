"""HTTP lifespan composition; standalone backend services remain opt-in owners."""

from contextlib import asynccontextmanager
import asyncio
import logging
import os
import threading

from fastapi import FastAPI
from hermes_cli.pty_session import run_reaper

logger = logging.getLogger("hermes_cli.web_server")


def _unlink_pty_markers(app: FastAPI) -> None:
    """Second pass after ``PTY_REGISTRY.close_all()``: drop channel markers left in app state,
    including stale paths from sessions reaped earlier (``PtySession.close()`` only removes
    markers owned by live registry sessions)."""
    from hermes_cli.web_server import _get_pty_active_session_files

    for marker in set(_get_pty_active_session_files(app).values()):
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


@asynccontextmanager
async def standalone_lifespan(app: "FastAPI"):
    from hermes_cli.web_server import (
        _eager_reconcile_own_session_db,
        _start_desktop_cron_ticker, _dashboard_selftest_loop,
        _auto_archive_ticker_loop, _terminate_desktop_managed_gateway, _release_ssh_runtime_marker,
    )
    from hermes_cli.web_server_chat import PTY_REGISTRY

    app.state.event_channels = {}  # dict[str, set]
    app.state.event_lock = asyncio.Lock()
    app.state.pty_active_session_files = {}  # dict[str, Path]
    # Serializes chat-argv resolution so concurrent /api/pty connections don't
    # overlap ``npm install`` / ``npm run build``. Locks live on app.state (not
    # module globals) so they bind to the running loop, not the import-time one.
    app.state.chat_argv_lock = asyncio.Lock()

    # Bring state.db schema current BEFORE the first session-list poll
    # (#79531/#80037): a store left behind by `hermes update` otherwise 500s
    # every poll until its owner completes schema reconciliation.
    # Off-thread so a locked store never delays the socket (Desktop
    # ready-probe times out at 10s, GH-73083). NOT a daemon, and joined at
    # shutdown: its sqlite connection must be closed by the thread that is
    # stepping it. A daemon copy that outlived the lifespan had its
    # connection closed from the main thread mid-probe (pytest's leaked-DB
    # sweep) and segfaulted the interpreter. The worker is time-bounded by
    # SessionDB's lock patience, so the join cannot hang shutdown.
    eager_reconcile_thread = threading.Thread(
        target=_eager_reconcile_own_session_db,
        name="statedb-eager-reconcile",
    )
    eager_reconcile_thread.start()

    # The listener already answers during app assembly. Do not import the
    # gateway/provider tree merely to prewarm it; real session/cron demand owns it.

    # Snapshot the checkout revision so lazy-import paths (model picker) can
    # refuse with "restart required" after `hermes update` replaced the code
    # (#86207); the update flow does not reliably restart the dashboard.
    from gateway.code_skew import install_boot_pinned_imports, record_boot_fingerprint

    record_boot_fingerprint()
    install_boot_pinned_imports()

    # Hosted Bot rooms belong to the backend process. Recovery may need a
    # contended state.db migration, so keep it off the pre-yield path: Group
    # Chat must degrade on its own rather than block every Desktop feature.
    from tui_gateway import methods_groups as _hosted_groups
    from hermes_cli.web_gateway_lazy import (
        GatewayOnDemand, install_message_host, clear_message_host, install_readopt_waker,
    )

    try:
        install_message_host()
    except Exception:
        logger.warning("TUI message injector did not install", exc_info=True)

    hosted_room_start_cancel = threading.Event()

    def _start_hosted_rooms() -> None:
        try:
            _hosted_groups.start_hosted_room_service(server=GatewayOnDemand())
        except Exception:
            logger.exception("Hosted Group Chat recovery failed during backend startup")
        finally:
            if hosted_room_start_cancel.is_set():
                _hosted_groups.stop_hosted_room_service(timeout=1.0)

    hosted_room_start_thread = threading.Thread(
        target=_start_hosted_rooms,
        daemon=True,
        name="hosted-room-startup",
    )
    hosted_room_start_thread.start()

    # desktop.auto_continue.resume_on_startup: continue every turn the last backend died in the
    # middle of, instead of waiting for the user to re-open each session (tui_gateway/startup_resume.py).
    try:
        from tui_gateway.startup_resume import start_startup_resume_sweep

        start_startup_resume_sweep()
    except Exception:
        logger.warning("startup resume sweep did not start", exc_info=True)

    # Unloaded-session kanban delivery cannot wait for the first /api/ws client.
    # entry.py and ws.py are the only other starters; an always-on `hermes serve`
    # (the desktop SSH backend included) hosts chats and loads plugins with no
    # websocket attached, so unseen tui completions sit unclaimed. Idempotent
    # with those callers. Skipped under the test harness so a TestClient
    # lifespan does not spawn a sweeper into the suite sandbox. [daveey]
    if not os.environ.get("HERMES_TEST_ISOLATION"):
        try:
            from tui_gateway.kanban_unloaded import start_kanban_unloaded_sweeper

            start_kanban_unloaded_sweeper()
        except Exception:
            logger.warning("kanban unloaded-session sweeper start failed", exc_info=True)
        # [daveey] Cold unrouted chats holding a trusted producer completion (tui_gateway.producer_admission).
        try:
            from tui_gateway.producer_admission import start_producer_admission_sweeper

            start_producer_admission_sweeper()
        except Exception:
            logger.warning("producer admission sweeper start failed", exc_info=True)

    # Build the session-list kanban annotation now, before the Desktop's reconnect burst: a cold one made every
    # session read, GET /api/sessions/<id> included, wait 4-10 s on one pass under that burst. [daveey]
    try:
        from hermes_cli.kanban_annotation_prewarm import start_annotation_prewarm

        if getattr(app.state, 'annotation_facts', None) is None:
            start_annotation_prewarm()
    except Exception:
        logger.warning("kanban annotation prewarm did not start", exc_info=True)
    # One process per home keeps the published annotation current for the others (kanban_annotation_host). [daveey]
    try:
        from hermes_cli.kanban_annotation_host import start_holder
        from hermes_cli.process_identity import is_desktop_owned_backend as _desktop_owned

        if getattr(app.state, 'annotation_facts', None) is None:
            start_holder("serve" if _desktop_owned() else "dashboard")
    except Exception:
        logger.warning("kanban annotation holder did not start", exc_info=True)

    # Re-adopt background jobs a previous backend's chats left running (orphans of a Desktop
    # restart) and wake each chat when its job exits (tools/process_registry_readopt.py). [daveey]
    from hermes_cli.process_identity import is_desktop_owned_backend
    try:
        from tools.process_registry_readopt import start_checkpoint_keeper, start_tui_orphan_recovery

        if is_desktop_owned_backend():  # a `hermes dashboard` hosts no chats: it must not adopt their jobs
            install_readopt_waker()
            start_tui_orphan_recovery()
        start_checkpoint_keeper()
    except Exception:
        logger.warning("background process re-adoption did not start", exc_info=True)

    # Desktop-spawned backends fire cron jobs themselves, since the app has no
    # gateway running the scheduler. Server `hermes dashboard` is unaffected —
    # it relies on its own gateway.
    cron_stop: threading.Event | None = None
    cron_thread: threading.Thread | None = None
    desktop_owned = is_desktop_owned_backend()
    if desktop_owned:
        # Reap an orphaned gateway from an abnormal previous exit (reparented to
        # launchd, still holding the platform WebSocket) before forking a fresh
        # one that would race the same credential (#77276). Runs
        # unconditionally; protection of a healthy standalone gateway lives
        # INSIDE the reaper (registration probed with cleanup_stale=False).
        # Startup grace: spare a gateway still claiming gateway.pid/lock (#122533).
        try:
            from hermes_cli.dashboard_procs import _REAP_MIN_AGE_SECONDS
            from hermes_cli.gateway import _reap_unsupervised_gateway_orphans

            _reap_unsupervised_gateway_orphans(min_age_s=_REAP_MIN_AGE_SECONDS)
        except Exception:
            logger.exception("Desktop startup: orphan gateway reap failed")

        cron_stop = threading.Event()
        cron_thread = threading.Thread(
            target=_start_desktop_cron_ticker,
            args=(cron_stop,),
            daemon=True,
            name="desktop-cron-ticker",
        )
        cron_thread.start()

    # Reap idle/dead keep-alive PTY sessions (30-min TTL). The registry is process-wide and
    # close_all() refuses new spawns, so a re-entered lifespan reopens it first.
    PTY_REGISTRY.reopen()
    pty_reaper_task = asyncio.create_task(run_reaper(PTY_REGISTRY))
    # Periodic authenticated self-test feeding the ``dashboard`` component on /api/status.
    selftest_task = asyncio.create_task(_dashboard_selftest_loop())
    # Live auto-archive timer, independent of list requests.
    auto_archive_task = asyncio.create_task(_auto_archive_ticker_loop())

    # Managed local runtime (local_runtime.enabled): bring llama-server back so a
    # restart doesn't strand a llamacpp main model. Off-thread and best-effort;
    # failure falls back to cloud providers like a cold start. Server only —
    # models load on first inference (an empty router holds no VRAM).
    def _boot_local_runtime():
        try:
            from hermes_cli.config import load_config
            from hermes_cli.local_runtime.bootstrap import ensure_local_runtime

            ensure_local_runtime(load_config())
        except Exception as exc:
            logging.getLogger(__name__).warning("local runtime boot failed: %s", exc)

    threading.Thread(target=_boot_local_runtime, daemon=True, name="local-runtime-boot").start()

    # Records who carries inference for setup.status / free_tier.status and broadcasts `setup.ready`
    # (no identity is created at boot). Off-thread; the desktop's first setup.status waits on the
    # record (bounded) instead.
    from hermes_cli.free_tier_bootstrap import start_background_bootstrap

    start_background_bootstrap()

    # Always-on backend only: keep app traffic unavailable until prewarms finish (bounded), so the Desktop's
    # reconnect burst after a core-update restart meets warm caches (hermes_cli/boot_warmup_gate.py). [daveey]
    from hermes_cli.boot_warmup_gate import hold_bind_for_prewarm

    await asyncio.to_thread(hold_bind_for_prewarm)

    # Thread-lane sizes from config.yaml, read off the loop before the first request (web_thread_lanes). [daveey]
    from hermes_cli import web_thread_lanes

    await asyncio.to_thread(web_thread_lanes.configure)

    try:
        yield
    finally:
        try:
            clear_message_host()
        except Exception:
            logger.debug("TUI message injector clear skipped", exc_info=True)
        hosted_room_start_cancel.set()
        _hosted_groups.stop_hosted_room_service(timeout=5.0)
        hosted_room_start_thread.join(timeout=1.0)
        if cron_stop is not None:
            cron_stop.set()
        pty_reaper_task.cancel()
        selftest_task.cancel()
        auto_archive_task.cancel()
        eager_reconcile_thread.join()
        await PTY_REGISTRY.close_all()

        # PtySession.close() removes markers owned by live registry sessions.
        # This second pass cleans any channel markers left in app state,
        # including stale paths from sessions reaped earlier.
        _unlink_pty_markers(app)

        # Stop the managed llama-server with its parent (an orphan pins VRAM).
        try:
            from hermes_cli.local_runtime.bootstrap import shutdown_local_runtime

            shutdown_local_runtime()
        except Exception:
            # Shutdown boundary: a supervisor that fails to stop must not skip the
            # managed-gateway teardown below; log it so the orphan is diagnosable.
            logger.warning("managed local runtime shutdown failed", exc_info=True)
        _release_ssh_runtime_marker()  # [daveey]
        if desktop_owned:
            _terminate_desktop_managed_gateway()


@asynccontextmanager
async def http_lifespan(app: FastAPI):
    """The gateway already owns schema, rooms, scheduler and model services."""
    from contextlib import AsyncExitStack

    from hermes_cli.web_server import _dashboard_selftest_loop
    from hermes_cli.web_server_chat import PTY_REGISTRY
    from hermes_cli.web_server_surface import policy

    app.state.event_channels = {}
    app.state.event_lock = asyncio.Lock()
    app.state.pty_active_session_files = {}
    app.state.chat_argv_lock = asyncio.Lock()
    PTY_REGISTRY.reopen()  # a prior lifespan's close_all() refuses spawns until reopened
    tasks = [
        asyncio.create_task(run_reaper(PTY_REGISTRY), name="gateway-api-pty-reaper"),
        asyncio.create_task(_dashboard_selftest_loop(), name="gateway-api-selftest"),
    ]
    try:
        async with AsyncExitStack() as surface:
            if policy(app.state).host_terminal:
                from hermes_cli.web_host_terminal_sessions import host_terminal_lifespan
                await surface.enter_async_context(host_terminal_lifespan(app))
            if policy(app.state).gateway_connections:
                from hermes_cli import gateway_connections_ssh
                from hermes_cli.gateway_connections_watch import watch_store
                # Registered first so it runs last: the watcher stops, then every SSH tunnel this
                # listener opened (and the remote serve it owns) is closed, never orphaned.
                surface.push_async_callback(asyncio.to_thread, gateway_connections_ssh.close_all)
                await surface.enter_async_context(watch_store())
            yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await PTY_REGISTRY.close_all()
        _unlink_pty_markers(app)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    lifespan = http_lifespan if getattr(app.state, "gateway_runner", None) is not None else standalone_lifespan
    # A dashboard serves many requests/s: a slow gateway ``identify`` must not be re-paid on each
    # (stale-while-revalidate for the host-gateway memo; CLI processes keep the synchronous TTL).
    from gateway import host_attach
    host_attach.enable_stale_while_revalidate()
    from hermes_cli.fact_plugin_mount import lifespan as plugin_lifespan
    from hermes_cli.fact_links_startup import lifespan as links_lifespan
    from hermes_cli.sessions_native_startup import lifespan as sessions_lifespan
    from hermes_cli.fact_tree_startup import lifespan as tree_lifespan
    from hermes_cli.fact_annotation_lifespan import lifespan as annotation_lifespan
    from hermes_cli.web_boot_services import services_lifespan
    from hermes_cli.session_state_startup import lifespan as session_state_lifespan
    try:
        async with links_lifespan(app), annotation_lifespan(app), sessions_lifespan(app), tree_lifespan(app), plugin_lifespan(app), session_state_lifespan(app), services_lifespan(app, lifespan):
            yield
    finally:
        host_attach.enable_stale_while_revalidate(False)
