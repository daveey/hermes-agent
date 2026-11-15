"""Review notices report a handoff, not an implementation result."""
from pathlib import Path

import pytest
import hermes_yaml as yaml


@pytest.mark.parametrize('path', sorted(path for path in
                         (Path(__file__).resolve().parents[2] / 'locales').glob('*.yaml')
                         if not path.name.endswith('.tui.yaml')), ids=lambda path: path.stem)
def test_review_wake_locale_reports_only_the_handoff(path):
    translated = {'ar': 'أُحيلت للمراجعة', 'hu': 'átadva átnézésre', 'zh': '已交付评审'}
    text = yaml.safe_load(path.read_text())['gateway']['kanban']['wake']['review_requested']
    assert text == translated.get(path.stem, 'handed off for review')
