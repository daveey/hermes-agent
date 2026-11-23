import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'

import { describe, expect, it, vi } from 'vitest'

import { BootTimeoutRecovery } from '../src/app/gateway/hooks/boot-timeout-recovery'

import { broadcastBootstrapReset } from './bootstrap-reset-windows'

function windowStub(destroyed = false, contentsDestroyed = false) {
  return {
    isDestroyed: () => destroyed,
    webContents: { isDestroyed: () => contentsDestroyed, send: vi.fn() }
  }
}

describe('bootstrap reset fanout', () => {
  it('executes the registered main reset and clears a timed-out second window', async () => {
    vi.useFakeTimers()
    const recovery = new BootTimeoutRecovery()

    try {
      let secondError: string | null = 'Timed out connecting to Hermes backend'
      recovery.schedule(() => {
        secondError = null
      })
      const caller = windowStub()
      const second = windowStub()
      second.webContents.send.mockImplementation((_channel, payload) => {
        if (payload.phase === 'bootstrap.reset') {
          recovery.expedite()
        }
      })
      const source = readFileSync(new URL('./main.ts', import.meta.url), 'utf8')
      const start = source.indexOf("ipcMain.handle('hermes:bootstrap:reset'")
      const end = source.indexOf("ipcMain.handle('hermes:bootstrap:repair'", start)
      expect(start).toBeGreaterThan(0)
      expect(end).toBeGreaterThan(start)
      let reset: (() => Promise<unknown>) | undefined
      const teardown = vi.fn(async () => undefined)
      runInNewContext(source.slice(start, end), {
        ipcMain: {
          handle: (_channel: string, handler: () => Promise<unknown>) => {
            reset = handler
          }
        },
        rememberLog: vi.fn(),
        remoteUpdatePause: { clear: vi.fn() },
        teardownPrimaryBackendAndWait: teardown,
        getFirstRunSetupGate: () => ({ resetForRetry: vi.fn() }),
        resetBootstrapSnapshot: vi.fn(),
        broadcastBootstrapReset,
        BrowserWindow: { getAllWindows: () => [caller, second] },
        bootProgressState: { phase: 'backend.error' }
      })
      await reset!()
      expect(teardown).toHaveBeenCalledOnce()
      expect(secondError).toBeNull()
      expect(second.webContents.send).toHaveBeenCalledOnce()
    } finally {
      recovery.stop()
      vi.useRealTimers()
    }
  })

  it('delivers reset to the timed-out second window, not just the requesting window', () => {
    const caller = windowStub()
    const second = windowStub()
    const closed = windowStub(true)
    const closedContents = windowStub(false, true)

    const progress = {
      error: null,
      fakeMode: false,
      message: '',
      phase: 'init',
      progress: 0,
      running: false,
      timestamp: 1
    }

    broadcastBootstrapReset([caller, second, closed, closedContents], progress)

    for (const window of [caller, second]) {
      expect(window.webContents.send).toHaveBeenCalledExactlyOnceWith('hermes:boot-progress', {
        ...progress,
        phase: 'bootstrap.reset'
      })
    }

    expect(closed.webContents.send).not.toHaveBeenCalled()
    expect(closedContents.webContents.send).not.toHaveBeenCalled()
    expect(progress.phase).toBe('init')
  })
})
