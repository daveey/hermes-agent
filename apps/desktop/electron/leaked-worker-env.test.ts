import { describe, expect, it } from 'vitest'

import { scrubLeakedWorkerEnv } from './leaked-worker-env'

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

  it('keeps a deliberate board pin when no worker marker is present', () => {
    const env: NodeJS.ProcessEnv = { HERMES_KANBAN_BOARD: 'x', HERMES_KANBAN_DB: '/tmp/k.db' }

    expect(scrubLeakedWorkerEnv(env)).toEqual([])
    expect(env).toEqual({ HERMES_KANBAN_BOARD: 'x', HERMES_KANBAN_DB: '/tmp/k.db' })
  })
})
