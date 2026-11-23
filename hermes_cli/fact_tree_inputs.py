"""Project-tree source captures and pure canonical payload composition. [daveey]"""
import time

from hermes_cli.fact_board_inputs import CapturedInput
from tui_gateway import project_tree_capture


def capture(targets, source, view, selector, budget):
    from hermes_cli.web_routers import profiles
    from tui_gateway import server
    observed_at = time.time()
    selected = selector.get('profile')
    native = selector['surface'] == 'native'
    drill = view == 'project_sessions'
    rows, errors = [], []
    names = targets[:1] if native and selected is None else targets
    if selected is not None:
        names = tuple(item for item in targets if item[0] == selected)
        if not names:
            raise ValueError('unauthorized tree profile')
    for name, home in names:
        def read(db, home=home):
            with profiles._hermes_home_scope(home):
                return server._capture_project_tree(db, preview_limit=0 if drill else selector['preview_limit'],
                    hydrate=drill, session_limit=selector['session_limit'], include_discovered=native and not drill)
        failed = []
        result = profiles._read_profile_db(name, home, failed, read)
        # Profile fan-out intentionally returns partial trees with per-profile
        # errors. Native routes have no such envelope: fail the source capture
        # so ContentOwner retains prior evidence stale (or cold unavailable).
        if native and failed:
            raise OSError('native project tree capture failed')
        errors.extend(failed)
        rows.append((name, result))
    return CapturedInput({'rows': rows, 'errors': errors, 'view': view, 'selector': dict(selector)},
                         ((source, {'profiles': tuple(name for name, _home in names)}),), observed_at)


def project(view, captured):
    from hermes_cli.web_routers.profiles import _merge_profile_tree
    from tui_gateway.project_tree import stamp_profile
    value = project_tree_capture._plain(captured)
    selector = value['selector']
    rows = []
    for name, result in value['rows']:
        if result is None:
            tree, active = {'projects': [], 'scoped_session_ids': []}, None
        else:
            tree, active = project_tree_capture.project(result[0]), result[1]
        if selector['surface'] == 'native':
            stamp_profile(tree['projects'], name)
            if view == 'project_sessions':
                return {'project': next((p for p in tree['projects'] if p['id'] == selector['project_id']), None)}
            return {'projects': tree['projects'], 'active_id': active, 'scoped_session_ids': tree['scoped_session_ids']}
        rows.append((name, tree))
    merged, ids = {}, []
    for name, tree in rows:
        _merge_profile_tree(merged, tree['projects'], name, selector['preview_limit'])
        ids.extend(tree['scoped_session_ids'])
    return {'projects': sorted(merged.values(), key=lambda p: p.get('lastActive') or 0, reverse=True),
            'active_id': None, 'scoped_session_ids': ids, 'errors': value['errors']}
