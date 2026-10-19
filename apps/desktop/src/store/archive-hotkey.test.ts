import { afterEach, describe, expect, it, vi } from 'vitest'

import { createPluginContext } from '@/contrib/plugin'

import {
  dispatchArchiveHotkey,
  registerArchiveHotkeyHandler,
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
})
