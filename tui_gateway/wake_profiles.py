"""Profile-fenced lookup and compression-tip recovery for explicit wake selectors."""
from pathlib import Path


def live_for_profiles(key, profiles):
    from tui_gateway import server
    if profiles is None:
        return server._find_live_session_by_key(key)
    from hermes_constants import get_default_hermes_root, get_process_hermes_home
    launch = Path(get_process_hermes_home()).resolve()
    for profile in profiles:
        home = launch if profile is None else Path(get_default_hermes_root()) / "profiles" / profile
        # The launch profile's runtime record may use None even when launch is named.
        selectors = [str(home)] + ([None] if home.resolve() == launch else [])
        for selector in selectors:
            live = server._find_live_session_by_key(key, profile_home=selector)
            if live is not None:
                return live
    return None


def compression_live(key, profiles):
    from hermes_state import SessionDB
    from hermes_constants import get_default_hermes_root, get_process_hermes_home
    selectors = profiles if profiles is not None else [None]
    for profile in selectors:
        home = get_process_hermes_home() if profile is None else Path(get_default_hermes_root()) / "profiles" / profile
        path = Path(home) / "state.db"
        if not path.is_file():
            continue
        with SessionDB(db_path=path, read_only=True) as db:
            tip = db.get_compression_tip(key)
        live = live_for_profiles(tip, [profile] if profiles is not None else None) if tip else None
        if live is not None:
            return live
    return None
