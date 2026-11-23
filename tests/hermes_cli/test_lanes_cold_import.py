"""Read-only spend diagnostics must not load routing catalogs or HTTP clients."""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

from pathlib import Path
import subprocess
import sys


def test_spend_decision_import_does_not_load_model_catalog_or_requests():
    root = Path(__file__).resolve().parents[2]
    code = f'''
import sys
sys.path.insert(0, {str(root)!r})
import hermes_cli.kanban_spend_decision
assert 'hermes_cli.kanban_model_neutral' not in sys.modules
assert 'requests' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
