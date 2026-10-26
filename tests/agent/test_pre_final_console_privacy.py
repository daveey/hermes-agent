import pytest
from tests.agent import test_pre_final_gate as harness
from tests.agent.test_pre_final_gate import Run, _buffered, _run

gate_agent = harness.agent

@pytest.mark.parametrize('verbose',[False,True])
def test_nonquiet_nonstreaming_rejection_not_printed(gate_agent,verbose,capsys):
    gate_agent.quiet_mode=False
    gate_agent.verbose_logging=verbose
    gate_agent.max_iterations=1
    gate_agent.iteration_budget.max_total=1
    Run(gate_agent,['REJECTED_PRIVATE_DIAGNOSIS'])
    gate_agent.stream_delta_callback=None
    capsys.readouterr()
    hook=_buffered(lambda **kw:dict(hold=True,nudge='Continue authorized repair',
          unresolved_fallback=dict(goal='Repair example',evidence_refs=['user:0'])))
    result=_run(gate_agent,hook,'Repair example')
    captured=capsys.readouterr()
    assert 'Unresolved goal' in result['final_response']
    assert 'REJECTED_PRIVATE_DIAGNOSIS' not in captured.out


@pytest.mark.parametrize('verbose', [False, True])
def test_unbuffered_console_preview_preserved(gate_agent, verbose, capsys):
    gate_agent.quiet_mode = False
    gate_agent.verbose_logging = verbose
    Run(gate_agent, ['ORDINARY_CONSOLE_ANSWER'])
    gate_agent.stream_delta_callback = None
    capsys.readouterr()
    result = _run(gate_agent, lambda **kw: None, 'Explain example')
    assert result['final_response'] == 'ORDINARY_CONSOLE_ANSWER'
    assert 'ORDINARY_CONSOLE_ANSWER' in capsys.readouterr().out
