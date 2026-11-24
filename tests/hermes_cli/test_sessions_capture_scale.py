"""Exercise the real first capture child over 30k sessions in twelve profiles."""
import json
import time

from hermes_cli.sessions_capture_process import CaptureProcess, CaptureUnavailable
from hermes_state import SessionDB
from tests.hermes_state import test_recency_hot_rows as helpers
from tests.hermes_cli.test_sessions_native_startup import selected


def test_first_30k_twelve_home_capture_finishes_in_unchanged_budget(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HOME', str(home))
    for number in range(12):
        target = home if number == 0 else home / 'profiles' / f'profile-{number}'
        target.mkdir(parents=True, exist_ok=True)
        (target / 'config.yaml').write_text('model: test\n')
        db = SessionDB(db_path=target / 'state.db')
        try:
            helpers._seed(db, 2500, prefix=f'p{number}-')
        finally:
            db.close()
    from hermes_cli import sessions_capture_process as module
    original_spawn = module.spawn_owned
    diagnostics = tmp_path / 'child-stderr'
    def diagnostic_spawn(*args, **kwargs):
        with diagnostics.open('wb') as output:
            kwargs['stderr'] = output
            return original_spawn(*args, **kwargs)
    monkeypatch.setattr(module, 'spawn_owned', diagnostic_spawn)
    capture = CaptureProcess()  # production's unchanged five-second deadline
    started = time.monotonic()
    try:
        results = capture.capture_many([
            ['sessions', selected(limit=50, min_messages=1)],
            ['sessions_sidebar', {}],
        ])
        elapsed = time.monotonic() - started
        assert elapsed < 5, elapsed
        assert all(not isinstance(body, CaptureUnavailable) for body in results)
        listing, sidebar = [json.loads(body) for body in results]
        assert len(listing['sessions']) == 50
        assert len(listing['profile_totals']) == 12
        assert listing['total'] == 30_000
        assert sidebar['recents']['sessions']
        print(f'first cold 30k/12-home list50+sidebar capture: {elapsed:.3f}s')
    except CaptureUnavailable as exc:
        raise AssertionError(diagnostics.read_text()) from exc
    finally:
        capture.close()
