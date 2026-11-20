"""Disposable managed-launch child: no model, Board calls or live profile writes."""
import json
import os
import sys
from unittest.mock import patch


def observation():
    return {'source': {k: v for k, v in os.environ.items() if k.startswith('HERMES_KANBAN_SOURCE_')},
            'cwd': os.getcwd()}


def main():
    result = {'startup': observation()}
    if sys.argv[1] == 'tools':
        from tools.terminal_tool import _get_env_config
        from tools.terminal_tool_lifecycle import _create_configured_env
        config = _get_env_config()
        assert config['env_type'] == 'local', config
        env = _create_configured_env(config, 'local', image=None, cwd=os.getcwd(), timeout=30,
                                     task_id='source-env-terminal', host_cwd=None)
        code = ('import os,json; print(json.dumps({"source":{k:v for k,v in os.environ.items() '
                'if k.startswith("HERMES_KANBAN_SOURCE_")},"cwd":os.getcwd()}))')
        import shlex
        try:
            answer = env.execute(shlex.join([sys.executable, '-c', code]))
            assert answer['returncode'] == 0, answer
            result['terminal'] = json.loads(answer['output'].strip())
        finally:
            env.cleanup()
        from tools.code_execution_tool import execute_code
        from tools.code_kernel import shutdown_all_kernels
        # This disposable test authorizes only its own literal readback cell, not a live agent turn.
        try:
            with patch('tools.approval.check_execute_code_guard', return_value={'approved': True}):
                answer = json.loads(execute_code(code, task_id='source-env-code', enabled_tools=[], reset=True))
            assert answer['status'] == 'success', answer
            result['execute_code'] = json.loads(answer['output'].strip())
        finally:
            shutdown_all_kernels()
    print(json.dumps(result))


if __name__ == '__main__':
    main()
