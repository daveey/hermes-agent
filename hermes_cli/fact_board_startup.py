"""Trusted bundled board mount cutover and scoped native evidence. [daveey]

The flag is startup-only. ON admits no legacy board read, including cold views;
OFF starts the unchanged prewarm lifetime once. External roster/diagnostic inputs
have 30s bounded recapture and 60s local validity, not subsecond upstream truth.
Mount installs the provider only; a scope's owner is constructed and started on
first scoped demand via the fact_process activation queue, never on the request
thread. Failed activations stay fail-closed; close() drains their custody.
"""
import json
import os
import sys
import threading

from hermes_cli.fact_board_inputs import CapturedInput
from hermes_cli.fact_content_owner import ContentBinding
from hermes_cli.fact_contract import Scope, SourceId, Ready, Pending, Unavailable
from hermes_cli.fact_view_owner import ViewOwner
from functools import partial
from hermes_cli import fact_board_native as board_native_inputs


class BoardRuntime:
    Ready, Pending, Unavailable = Ready, Pending, Unavailable
    _failed = frozenset()  # instance rebinds to a set in __init__

    def __init__(self, api, *, boards=None):
        from hermes_cli import kanban_db
        from hermes_cli.config import get_hermes_home
        from hermes_cli.profiles import get_active_profile_name
        self.api = api
        self.home = str(get_hermes_home())
        self.profile = get_active_profile_name()
        self.current = kanban_db.get_current_board()
        self.boards = tuple(dict.fromkeys(boards if boards is not None else
                            [self.current, *(b['slug'] for b in kanban_db.list_boards())]))
        self.owners, self.views, self._activating_views = {}, {}, {}
        self._failed = set()
        self._closed = self._drained = False
        self._lock = threading.RLock()
        self._previous = None

    def start(self):
        """Install the provider only: no owner, path resolution or thread until demand."""
        self.api.board_prewarm.stop_board_prewarm()
        self._previous = self.api.board_fact_views._installed
        self.api.board_fact_views.install_provider(self, self.provider)
        return self

    def _activate(self, board):
        """Build and start one scope's owner; runs on the fact_process worker."""
        from hermes_cli import kanban_db
        # The lock is the closing fence: close() cannot interleave with an
        # activation, and a queued activation that loses the race sees _closed.
        with self._lock:
            if self._closed or board in self.views or board in self._failed or board in self.owners:
                return
            try:
                scope = Scope(self.home, self.profile, board)
                path = kanban_db.kanban_db_path(board=board)
                def capture(view, selector, budget, board=board, scope=scope):
                    result = board_native_inputs.capture(self.api, board, view, dict(selector))
                    source = SourceId(scope, 'board.input', 'native')
                    return CapturedInput(result['value'], ((source, result['observations']),), result['observed_at'])
                owner = ViewOwner(scope, capture, partial(board_native_inputs.project, self.api),
                                  ('board', 'boards', 'projects'), paths=(path, path.parent, self.home),
                                  bindings=[ContentBinding(SourceId(scope, 'tasks.content', 'local'), path)])
                self.owners[board] = owner  # custody recorded before start: close() drains a partial owner
                handle = owner.start()
                paths = (str(path),)
                def stop(paths=paths):
                    self.api.board_legacy.stop(paths)
                    self.api.board_rows.retire(paths)
                    self.api.board_snapshot.retire(paths)
                view = self.api.board_fact_views.bind(handle.v1, scope, stop_legacy=stop,
                    start_legacy=lambda: None, demand=handle.demand,
                    hot_selectors=(('board', self.api.board_fact_views.board_selector()), ('projects', {}),
                                   ('boards', {'include_archived': False})))
                self._activating_views[board] = view  # retained on failure; close() drains it
                view.on_mode('facts', 1)
                view.prepare()
            except BaseException:
                self._failed.add(board)  # no legacy fallback, no retry
                raise
            self.views[board] = view
            del self._activating_views[board]

    def _request_activation(self, board):
        from hermes_cli import fact_process
        try:
            fact_process.submit(('board', self.home, self.profile, board), lambda: self._activate(board))
        except RuntimeError:
            return False  # capacity exceeded: stay fail-closed, no legacy read
        return True

    def provider(self, board):
        from hermes_cli.config import get_hermes_home
        # Scope comes from the already-authenticated native/plugin route context.
        # Foreign profiles must never borrow the launch profile's projection.
        if str(get_hermes_home()) != self.home or self._closed:
            return Unavailable('board_scope_unavailable')
        if board is None:
            owner = self.owners.get(self.current)
            if owner is None and self.current not in self._failed:
                self._request_activation(self.current)  # unscoped request == current-board demand
            if owner is None or owner.handle is None:
                return Unavailable('board_scope_unavailable')
            catalog = owner.handle.v1.snapshot(owner.scope).view('boards', {'include_archived': False})
            if not isinstance(catalog, Ready):
                return Unavailable('board_catalog_unavailable')
            board = json.loads(catalog.encoded)['current']
        view = self.views.get(board)
        if view is not None:
            return view
        if board not in self.boards or board in self._failed:
            return Unavailable('board_scope_unavailable')
        if not self._request_activation(board):  # memory lookup + enqueue only
            return Unavailable('board_scope_unavailable')
        return Pending('board_activating')

    def ready(self, board):
        if board in self.boards and board not in self.views and board not in self._failed:
            self._request_activation(board)  # an explicit readiness probe is scoped demand
        view = self.views.get(board)
        return view.prepare() if view is not None else False

    def close(self):
        with self._lock:
            if self._drained:
                return
            self._closed = True  # failed teardown keeps a fail-closed installed provider
            failures = []
            for resource in (*getattr(self, '_activating_views', {}).values(), *self.views.values(), *self.owners.values()):
                try:
                    resource.close()
                except Exception as exc:  # health: allow BLE001 -- retain failed custody and attempt all sibling drains
                    failures.append(exc)
            if failures:
                raise ExceptionGroup('board shutdown incomplete', failures)
            from hermes_cli import kanban_db
            board_native_inputs.retire(kanban_db.kanban_db_path(board=board) for board in getattr(self, 'boards', ()))
            views = self.api.board_fact_views
            if views._installed is not None and views._installed[1] == self.provider:
                views._installed = self._previous
            self._drained = True


def start(api):
    # health: allow HX002 -- task-defined startup-only rollback switch; not a credential
    if (os.environ.get('HERMES_FACTS_BOARD', '1').strip().lower() in ('0', 'off', 'false', 'no')
            or not sys.platform.startswith('linux')):
        from hermes_cli import kanban_db
        boards = {kanban_db.get_current_board(), *(b['slug'] for b in kanban_db.list_boards())}
        api.board_legacy.resume(str(kanban_db.kanban_db_path(board=board)) for board in boards)
        return api.board_prewarm.LegacyLifecycle(api)
    runtime = BoardRuntime(api)
    try:
        return runtime.start()
    except BaseException:
        runtime.close()
        raise
