import { describe, expect, it } from 'vitest'

import { AGENT_PROVENANCE_KEYS, dropAgentProvenance, scrubLeakedWorkerEnv } from './leaked-worker-env'

describe('dropAgentProvenance', () => {
  it('pins the stamp names hermes_cli/proof_home.py PROVENANCE_KEYS stamps', () => {
    // tests/hermes_cli/test_serve_proof_home.py pins the Python side to this same literal list.
    expect([...AGENT_PROVENANCE_KEYS]).toEqual(['HERMES_AGENT_TOOL_CALL', 'HERMES_AGENT_ORIGIN', 'HERMES_AGENT_HOME'])
  })

  it('drops tool-child stamps even without a worker marker', () => {
    const env: NodeJS.ProcessEnv = {
      HERMES_AGENT_HOME: '/Users/x/.hermes',
      HERMES_AGENT_ORIGIN: 'tool',
      HERMES_AGENT_TOOL_CALL: '1',
      PATH: '/bin'
    }

    expect(scrubLeakedWorkerEnv(env)).toEqual([])
    expect(dropAgentProvenance(env).sort()).toEqual([
      'HERMES_AGENT_HOME',
      'HERMES_AGENT_ORIGIN',
      'HERMES_AGENT_TOOL_CALL'
    ])
    expect(env).toEqual({ PATH: '/bin' })
  })
})

describe('scrubLeakedWorkerEnv', () => {
  it('drops a worker identity env inherited by the Desktop', () => {
    const env: NodeJS.ProcessEnv = {
      HERMES_DELEGATED_CHILD_CONTEXT: '/Users/x/.hermes',
      HERMES_KANBAN_BOARD: 'default',
      HERMES_KANBAN_DB: '/Users/x/.hermes/kanban.db',
      HERMES_KANBAN_WORKSPACE: '/Users/x/.hermes/kanban/workspaces/t_45d5a361',
      HERMES_KANBAN_WORKSPACES_ROOT: '/Users/x/.hermes/kanban/workspaces',
      PATH: '/bin'
    }

    expect(scrubLeakedWorkerEnv(env).sort()).toEqual([
      'HERMES_DELEGATED_CHILD_CONTEXT',
      'HERMES_KANBAN_BOARD',
      'HERMES_KANBAN_DB',
      'HERMES_KANBAN_WORKSPACE',
      'HERMES_KANBAN_WORKSPACES_ROOT'
    ])
    expect(env).toEqual({ PATH: '/bin' })
  })

  it('drops the whole agent-scope family the leaked Desktop carried, keeping tunables', () => {
    const env: NodeJS.ProcessEnv = {
      HERMES_CRON_TIMEOUT: '900',
      HERMES_DELEGATED_CHILD_CONTEXT: '/Users/x/.hermes',
      HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS: '60',
      HERMES_KANBAN_WORKSPACE: '/w/t_45d5a361',
      HERMES_KERNEL_DIR: '/tmp/k',
      HERMES_PARENT_PID: '84584',
      HERMES_RPC_TOKEN: 'tok',
      HERMES_SESSION_ID: '20261021_185223_3c48bc',
      HERMES_SESSION_SOURCE: 'kanban',
      HERMES_SESSION_STALL_TIMEOUT: '600',
      HERMES_SINGLE_QUERY_SESSION: '1',
      HERMES_SPAWN: '1',
      HERMES_SUPERVISED_CHILD: '1',
      PATH: '/bin'
    }

    expect(scrubLeakedWorkerEnv(env).sort()).toEqual([
      'HERMES_DELEGATED_CHILD_CONTEXT',
      'HERMES_KANBAN_WORKSPACE',
      'HERMES_KERNEL_DIR',
      'HERMES_PARENT_PID',
      'HERMES_RPC_TOKEN',
      'HERMES_SESSION_ID',
      'HERMES_SESSION_SOURCE',
      'HERMES_SINGLE_QUERY_SESSION',
      'HERMES_SPAWN',
      'HERMES_SUPERVISED_CHILD'
    ])
    expect(env).toEqual({
      HERMES_CRON_TIMEOUT: '900',
      HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS: '60',
      HERMES_SESSION_STALL_TIMEOUT: '600',
      PATH: '/bin'
    })
  })

  it('drops the dispatcher TERMINAL_CWD pin and UI session id that rode along (pid 59331)', () => {
    const env: NodeJS.ProcessEnv = {
      HERMES_DELEGATED_CHILD_CONTEXT: '/Users/x/.hermes',
      HERMES_UI_SESSION_ID: 'ui-1',
      HERMES_BROWSER_CONTROL_PRINCIPAL: 'p',
      TERMINAL_CWD: '/Users/x/.hermes/kanban/workspaces/t_45d5a361',
      TERMINAL_TIMEOUT: '180',
      PATH: '/bin'
    }

    scrubLeakedWorkerEnv(env)
    expect(env).toEqual({ PATH: '/bin', TERMINAL_TIMEOUT: '180' })
  })

  it('keeps a deliberate board pin when no worker marker is present', () => {
    const env: NodeJS.ProcessEnv = { HERMES_KANBAN_BOARD: 'x', HERMES_KANBAN_DB: '/tmp/k.db' }

    expect(scrubLeakedWorkerEnv(env)).toEqual([])
    expect(env).toEqual({ HERMES_KANBAN_BOARD: 'x', HERMES_KANBAN_DB: '/tmp/k.db' })
  })
})
