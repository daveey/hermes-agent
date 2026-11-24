"""Complete local store discovery for proofs of chat absence, not best-effort wake routing."""
from pathlib import Path
import stat

from hermes_constants import get_hermes_home


def profile_roster() -> tuple[list[str | None], bool]:
    """Retain discovered stores on errors; only an error-free roster proves absence."""
    profiles: list[str | None] = [None]
    complete = True
    root = Path(get_hermes_home()) / "profiles"
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return profiles, True  # A launch-only installation needs no profiles directory.
    except OSError:
        return profiles, False
    for entry in sorted(entries):
        try:
            if stat.S_ISREG((entry / "state.db").stat().st_mode):
                profiles.append(entry.name)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            complete = False
    return profiles, complete
