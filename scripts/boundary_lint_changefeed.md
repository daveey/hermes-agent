# Changefeed board boundary declarations

The five exact sites added to `boundary_lint_baseline.json` are intentional
bundled-Kanban integration boundaries, not an expansion of the public plugin API.
No wildcard exemption or aggregate ceiling is added.

- `board_event_history.py` → `kanban_diagnostic_history.EventHistory`: the captured
  event history must retain the canonical diagnostic history representation and
  storage-value equality, rather than duplicating its semantics in the dashboard.
- `board_fact_capture.py` → `kanban_db`: owner-only capture uses canonical task age
  serialization and profile discovery off the request path, in the authorized
  capture scope. This is not permission for fact readers to open storage.
- `board_fact_views.py` → `kanban_db`: only the explicit legacy fallback resolves
  the current board; fact-mode requests continue to use the injected binding.
- `board_fact_views.py` → `projection_request.RequestBinding`: share the host's
  context-local authorized request binding instead of inventing another routing
  authority in the plugin.
- `board_shadow.py` → `kanban_db`: shadow selection needs the same canonical row
  decoding (including malformed historical values) and keyed task reads as the
  live board. Duplicating private coercions would split the selection contract.

These are file-and-target-specific allowances for the bundled board's existing
canonical store/host integration. Third-party plugins still receive the private
import warning; the public API manifest and lint enforcement are unchanged.
