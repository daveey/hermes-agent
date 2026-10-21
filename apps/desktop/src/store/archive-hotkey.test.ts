import { afterEach, describe, expect, it, vi } from 'vitest'

import { createPluginContext } from '@/contrib/plugin'

import {
  dispatchArchiveHotkey,
  nextSessionOpenerFor,
  openNextSessionFrom,
  registerArchiveHotkeyHandler,
  registerNextSessionOpener,
  resetArchiveHotkeyHandlersForTests,
  runArchiveHotkeyHandlers
} from './archive-hotkey'

const base = { isFocused: true, openNextSession: () => {}, sessionId: 's1' }

describe('archive hotkey handlers', () => {
  afterEach(() => {
    resetArchiveHotkeyHandlersForTests()
    vi.restoreAllMocks()
  })

  it('core archives with its own next step when no handler claims the session', () => {
    const archive = vi.fn()
    const onDeselect = vi.fn()
    registerArchiveHotkeyHandler('declines', () => false)

    expect(dispatchArchiveHotkey('s1', { archive, focusedId: 's1', onDeselect })).toBe('core')
    expect(archive).toHaveBeenCalledWith('s1', { onDeselect })
  })

  it('a claiming handler replaces core archive and gets the target, focus and core next step', () => {
    const archive = vi.fn()
    const onDeselect = vi.fn()
    const seen: unknown[] = []

    registerArchiveHotkeyHandler('inbox', ctx => {
      seen.push({ id: ctx.sessionId, focused: ctx.isFocused })
      ctx.openNextSession()

      return true
    })

    expect(dispatchArchiveHotkey('s1', { archive, focusedId: 'other', onDeselect })).toBe('handled')
    expect(archive).not.toHaveBeenCalled()
    expect(seen).toEqual([{ id: 's1', focused: false }])
    expect(onDeselect).toHaveBeenCalledTimes(1)
  })

  it('openNextSession is a no-op when core has no next session', () => {
    registerArchiveHotkeyHandler('inbox', ctx => {
      ctx.openNextSession()

      return true
    })

    expect(dispatchArchiveHotkey('s1', { archive: vi.fn(), focusedId: 's1', onDeselect: undefined })).toBe('handled')
  })

  it('skips a handler that throws and keeps asking the rest in order', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const order: string[] = []
    registerArchiveHotkeyHandler('boom', () => {
      order.push('boom')
      throw new Error('nope')
    })
    registerArchiveHotkeyHandler('ok', () => {
      order.push('ok')

      return true
    })
    registerArchiveHotkeyHandler('never', () => {
      order.push('never')

      return true
    })

    expect(runArchiveHotkeyHandlers(base)).toBe(true)
    expect(order).toEqual(['boom', 'ok'])
  })

  it('plugin ctx.registerArchiveHotkeyHandler is removed when the plugin unloads', () => {
    const disposers: Array<() => void> = []
    const plugin = createPluginContext('inbox', dispose => disposers.push(dispose))
    plugin.registerArchiveHotkeyHandler(() => true)

    expect(runArchiveHotkeyHandlers(base)).toBe(true)
    disposers.forEach(dispose => dispose())
    expect(runArchiveHotkeyHandlers(base)).toBe(false)
  })

  it('openNextSessionFrom: false before wiring installs an opener, then opens core next pick', () => {
    expect(openNextSessionFrom('s1')).toBe(false)
    const open = vi.fn()
    const pick = vi.fn(() => 's2')
    const dispose = registerNextSessionOpener(nextSessionOpenerFor(pick, open))

    expect(openNextSessionFrom('s1')).toBe(true)
    expect(pick).toHaveBeenCalledWith('s1')
    expect(open).toHaveBeenCalledWith('s2')
    expect(openNextSessionFrom('')).toBe(false)
    dispose()
    expect(openNextSessionFrom('s1')).toBe(false)
  })

  it('opener never re-opens the source and reports no next session', () => {
    const open = vi.fn()
    registerNextSessionOpener(nextSessionOpenerFor(id => (id === 'a' ? 'a' : null), open))

    expect(openNextSessionFrom('a')).toBe(false)
    expect(openNextSessionFrom('b')).toBe(false)
    expect(open).not.toHaveBeenCalled()
  })

  it('a throwing opener returns false instead of breaking the plugin', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    registerNextSessionOpener(() => {
      throw new Error('nope')
    })

    expect(openNextSessionFrom('s1')).toBe(false)
  })

  it('a stale disposer does not remove a newer opener', () => {
    const first = registerNextSessionOpener(() => true)
    registerNextSessionOpener(() => true)
    first()

    expect(openNextSessionFrom('s1')).toBe(true)
  })

  it('plugin ctx.openNextSession reaches the installed opener', () => {
    const plugin = createPluginContext('inbox', () => {})
    const open = vi.fn()

    expect(plugin.openNextSession('s1')).toBe(false)
    registerNextSessionOpener(nextSessionOpenerFor(() => 's9', open))
    expect(plugin.openNextSession('s1')).toBe(true)
    expect(open).toHaveBeenCalledWith('s9')
  })
})
