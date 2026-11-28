import { describe, expect, it, vi } from 'vitest'

import { hideAgentTestInstance, isHiddenAgentTestInstance } from './agent-test-instance'

const leaked = ['HERMES_KANBAN_TASK', 'TERMINAL_CWD']

describe('isHiddenAgentTestInstance', () => {
  it('hides an unpackaged `electron .` a worker started (the t_ed201037 second window)', () => {
    expect(
      isHiddenAgentTestInstance({
        leakedWorkerEnv: leaked,
        isPackaged: false,
        argv: ['/x/Electron', '.', '--remote-debugging-port=9455', '--user-data-dir=/x/ssr-live/userdata'],
        env: {}
      })
    ).toBe(true)
  })

  it('hides a packaged build a worker started on a scratch profile', () => {
    expect(
      isHiddenAgentTestInstance({
        leakedWorkerEnv: leaked,
        isPackaged: true,
        argv: ['/x/Hermes', '--user-data-dir', '/tmp/p'],
        env: {}
      })
    ).toBe(true)
  })

  it("keeps the owner's packaged Desktop visible when a worker reopens it", () => {
    expect(isHiddenAgentTestInstance({ leakedWorkerEnv: leaked, isPackaged: true, argv: ['/x/Hermes'], env: {} })).toBe(
      false
    )
  })

  it('never hides a Desktop no worker launched', () => {
    expect(
      isHiddenAgentTestInstance({ leakedWorkerEnv: [], isPackaged: false, argv: ['/x/Electron', '.'], env: {} })
    ).toBe(false)
  })

  it('HERMES_DESKTOP_AGENT_VISIBLE=1 opts a test instance back in', () => {
    expect(
      isHiddenAgentTestInstance({
        leakedWorkerEnv: leaked,
        isPackaged: false,
        argv: ['/x/Electron', '.'],
        env: { HERMES_DESKTOP_AGENT_VISIBLE: '1' }
      })
    ).toBe(false)
  })
})

describe('hideAgentTestInstance', () => {
  function fakeApp() {
    let created: ((event: unknown, win: never) => void) | null = null

    const app = {
      on: vi.fn((_event: string, listener: (event: unknown, win: never) => void) => {
        created = listener
      }),
      setActivationPolicy: vi.fn(),
      dock: { hide: vi.fn() },
      focus: vi.fn()
    }

    return { app, emitWindow: (win: object) => created?.({}, win as never) }
  }

  it('drops the Dock icon on macOS and makes every new window unshowable', () => {
    const { app, emitWindow } = fakeApp()
    const focus = app.focus

    hideAgentTestInstance(app as never, 'darwin')

    expect(app.setActivationPolicy).toHaveBeenCalledWith('accessory')
    expect(app.dock.hide).toHaveBeenCalled()

    const win = { show: vi.fn(), showInactive: vi.fn(), focus: vi.fn(), moveTop: vi.fn() }
    const original = { ...win }
    emitWindow(win)
    win.show()
    win.showInactive()
    win.focus()
    win.moveTop()
    app.focus({ steal: true })

    expect(original.show).not.toHaveBeenCalled()
    expect(original.showInactive).not.toHaveBeenCalled()
    expect(original.focus).not.toHaveBeenCalled()
    expect(original.moveTop).not.toHaveBeenCalled()
    expect(focus).not.toHaveBeenCalled()
  })

  it('leaves the Dock alone off macOS but still never shows windows', () => {
    const { app, emitWindow } = fakeApp()

    hideAgentTestInstance(app as never, 'linux')

    expect(app.dock.hide).not.toHaveBeenCalled()
    const win = { show: vi.fn(), showInactive: vi.fn(), focus: vi.fn(), moveTop: vi.fn() }
    const show = win.show
    emitWindow(win)
    win.show()
    expect(show).not.toHaveBeenCalled()
  })
})
