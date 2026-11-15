"""Self-work is explicit and product cards are never inferred from titles."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_selfwork as policy


def declaration(kind='sweep', **extra):
    return {'version': 1, 'repository': 'hermes-core', 'area': 'kanban', 'kind': kind, **extra}


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        yield conn


@pytest.mark.parametrize('value', [[], {}, declaration(repository=[]), declaration(kind=[]),
                                  declaration(repository='babybear'), declaration(repository='ask-eve'),
                                  declaration(repository='fabric'), declaration(area='../escape'),
                                  declaration(area='a//b'), declaration(version=True), declaration(unknown=True)])
def test_invalid_contracts(value):
    with pytest.raises(ValueError):
        policy.contract(value)


@pytest.mark.parametrize('kind', sorted(policy.KINDS))
def test_all_maintenance_kinds(kind):
    assert policy.contract(declaration(kind)) == declaration(kind)


def test_product_and_quoted_contract_untouched(board):
    tid = kb.create_task(board, title='Product guard', body=str(declaration()))
    assert policy.declared(board, tid) is None
