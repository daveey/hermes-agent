import { describe, expect, it, vi } from 'vitest'

import { classifyBackendExit, createBackendExitSender } from './backend-exit-report'

describe('classifyBackendExit', () => {
  it('marks a SIGTERM during a core update (live marker) as a planned update restart', async () => {
    await expect(classifyBackendExit({ code: null, signal: 'SIGTERM' }, async () => true)).resolves.toEqual({
      code: null,
      signal: 'SIGTERM',
      planned: 'update'
    })
  })

  it('reports an exit with no update in progress as unexpected', async () => {
    const payload = { code: 1, signal: null, error: 'boom' }

    await expect(classifyBackendExit(payload, async () => false)).resolves.toEqual(payload)
  })

  it('reports the exit as unexpected when the update probe fails, so a real crash is never hidden', async () => {
    const payload = { code: null, signal: 'SIGKILL' }

    await expect(
      classifyBackendExit(payload, async () => {
        throw new Error('marker unreadable')
      })
    ).resolves.toEqual(payload)
  })
})

describe('createBackendExitSender', () => {
  function harness({ planned = false, silenced = false } = {}) {
    const send = vi.fn()
    const log = vi.fn()

    const sender = createBackendExitSender({
      isSilenced: () => silenced,
      isPlannedRestart: async () => planned,
      window: () => ({ isDestroyed: () => false, webContents: { isDestroyed: () => false, send } }),
      log
    })

    return { log, send, sender }
  }

  it('sends a planned update exit with planned set and logs it', async () => {
    const { log, send, sender } = harness({ planned: true })

    await sender({ code: null, signal: 'SIGTERM' })

    expect(send).toHaveBeenCalledWith('hermes:backend-exit', { code: null, signal: 'SIGTERM', planned: 'update' })
    expect(log).toHaveBeenCalledWith(expect.stringContaining('planned update'))
  })

  it('sends an unexpected exit unchanged', async () => {
    const { log, send, sender } = harness()

    await sender({ code: 1, signal: null })

    expect(send).toHaveBeenCalledWith('hermes:backend-exit', { code: 1, signal: null })
    expect(log).not.toHaveBeenCalled()
  })

  it('stays silent during an intentional soft re-home', async () => {
    const { send, sender } = harness({ silenced: true })

    await sender({ code: null, signal: 'SIGTERM' })

    expect(send).not.toHaveBeenCalled()
  })
})
