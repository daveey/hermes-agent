"""Serve-owned project-tree activation, independent of sessions annotations. [daveey]"""
from contextlib import asynccontextmanager
from functools import partial
import asyncio
import os
import sys

from hermes_cli import fact_tree_inputs, projects_fact_routes
from hermes_cli.fact_contract import Scope, SourceId, Unavailable
from hermes_cli.fact_content_owner import ContentBinding
from hermes_cli.fact_view_owner import ViewOwner


class TreeBinding:
    def __init__(self, runtime, profile):
        self.runtime, self.profile = runtime, profile

    def read(self, view, selector):
        runtime = self.runtime
        if runtime.closed:
            return Unavailable('tree_owner_closed')
        selector = dict(selector)
        if self.profile is not None:
            selector['profile'] = self.profile
        try:
            result = runtime.handle.v1.snapshot(runtime.scope).view(view, selector)
            if isinstance(result, runtime.handle.v1.Pending):
                runtime.handle.demand(view, selector)
            return result
        except RuntimeError:
            return Unavailable('tree_owner_unavailable')


# The tree's captured input stays servable this long (BoardInputSource valid_seconds; the board keeps 60 s).
# Hub 2026-11-24, board facts on: each tree capture walks all twelve profiles, and its four selectors (native and
# profiles surfaces x preview limits) queue on the one shared capture worker with the board views, so a full cycle
# took longer than 60 s in the loaded dashboard backend. The inputs expired before their recapture (ages 64-178 s,
# validity_expired) and GET /api/profiles/projects/tree answered 503 dependency_unavailable ~15-24 times a minute,
# while every per-profile sessions.content clock was fresh. Refresh is still every 30 s; this bounds how stale a
# served tree, and a project_sessions drill-in on the same owner, can get while a slow recapture cycles, and a
# single capture may take up to TREE_CAPTURE_SECONDS. A failed capture still marks its input stale at once.
TREE_INPUT_VALID_SECONDS = 300
TREE_CAPTURE_SECONDS = 60


class TreeRuntime:
    def __init__(self, *, targets=None):
        from hermes_cli.config import get_hermes_home
        from hermes_cli.web_routers import profiles
        self.home = str(get_hermes_home())
        self.targets = tuple(targets if targets is not None else profiles._scoped_profile_targets('facts tree startup', None))
        self.targets = tuple(sorted(self.targets, key=lambda item: str(item[1]) != self.home))
        self.names = {name for name, _home in self.targets}
        self.homes = {str(home): name for name, home in self.targets}
        # Shared storage needs a distinct materializer namespace from links,
        # which legitimately uses the same home/profile origin.
        self.scope = Scope('serve.projects.' + self.home, self.homes.get(self.home, 'default'))
        self.source_id = SourceId(self.scope, 'board.input', 'native')
        self.closed = self.drained = False
        self._previous = None
        self.handle = None
        bindings = [ContentBinding(SourceId(self.scope, 'sessions.content', name), home / 'state.db')
                    for name, home in self.targets if (home / 'state.db').exists()]
        paths = [path for _name, home in self.targets for path in (home, home / 'state.db', home / 'projects.db')]
        self.owner = ViewOwner(self.scope, partial(fact_tree_inputs.capture, self.targets, self.source_id),
            fact_tree_inputs.project, ('tree', 'project_sessions'), paths=paths, bindings=bindings, consumer='projects',
            input_options={'valid_seconds': TREE_INPUT_VALID_SECONDS, 'max_capture_seconds': TREE_CAPTURE_SECONDS})

    def start(self):
        self.handle = self.owner.start()
        self._previous = projects_fact_routes._installed
        projects_fact_routes.install(self.handle.v1, self.provider)
        for surface in ('native', 'profiles'):
            self.handle.demand('tree', {'surface': surface, 'profile': None, 'preview_limit': 3, 'session_limit': 2000})
        return self

    def provider(self, profile):
        from hermes_cli.config import get_hermes_home
        home = str(get_hermes_home())
        if self.closed or home not in self.homes or profile is not None and profile not in self.names:
            return Unavailable('tree_scope_unavailable')
        # Native grant overrides the ambient home before this memory-only lookup.
        # An omitted profile then means that grant, never the launch profile.
        selected = profile if profile is not None else (self.homes[home] if home != self.home else None)
        if home != self.home and selected != self.homes[home]:
            return Unavailable('tree_scope_unavailable')
        return TreeBinding(self, selected)

    def close(self):
        if self.drained:
            return
        self.closed = True
        self.owner.close()
        if projects_fact_routes._installed is not None and projects_fact_routes._installed[1] == self.provider:
            projects_fact_routes._installed = self._previous
        self.drained = True


@asynccontextmanager
async def lifespan(app):
    # health: allow HX002 -- shared board/projects startup-only emergency rollback switch
    if (os.environ.get('HERMES_FACTS_BOARD', '1').strip().lower() in ('0', 'off', 'false', 'no')
            or not sys.platform.startswith('linux')):
        yield
        return
    runtime = TreeRuntime()
    try:
        runtime.start()
        yield
    finally:
        await asyncio.to_thread(runtime.close)
