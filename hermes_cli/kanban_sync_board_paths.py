"""Board discovery split into non-additive CPU diagnostics, without caching."""
from hermes_cli import kanban_sync_cpu as cpu


def board_paths(sync, board_id_re):
    from hermes_cli import kanban_db, kanban_db_boards
    root = kanban_db.kanban_home()

    def enumerate_boards():
        found = [("default", root / "kanban.db")] if (root / "kanban.db").exists() else []
        # The shared enumerator excludes archived and non-slug directories.
        return found + [(slug, p) for slug, p in kanban_db_boards.named_board_db_files() if board_id_re.match(slug)]

    found = cpu.detail("enumerate", enumerate_boards)
    local = {slug for slug, _ in found}

    def linked_boards():
        links_dir = root / "kanban" / sync.LINKS_DIR
        if links_dir.is_dir():
            return [(p.stem, p) for p in sorted(links_dir.glob("*.db"))
                    if board_id_re.match(p.stem) and p.stem not in local]
        return []

    found += cpu.detail("links", linked_boards)

    def selected():
        config = sync.sync_config()  # once, not per board
        return [(slug, path) for slug, path in found if sync.board_selected(str(path), config)]

    return cpu.detail("filter", selected)
