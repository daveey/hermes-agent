import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const key = 'hermes.desktop.tabStripDefault'

beforeEach(() => {
  localStorage.clear()
  vi.resetModules()
})

afterEach(() => vi.resetModules())

describe('tab strip default', () => {
  it('defaults to Single without writing its seed to storage', async () => {
    const prefs = await import('./tabstrip-prefs')
    expect(prefs.$tabStripDefault.get()).toBe('single')
    expect(localStorage.getItem(key)).toBeNull()
    expect(prefs.effectiveTabStripMode(undefined)).toBeUndefined()
  })

  it.each(['auto', 'always', 'never'] as const)('restores an explicit %s preference', async mode => {
    localStorage.setItem(key, mode)
    const prefs = await import('./tabstrip-prefs')
    expect(prefs.$tabStripDefault.get()).toBe(mode)
    expect(prefs.effectiveTabStripMode(undefined)).toBe(mode === 'auto' ? undefined : mode)
  })

  it('persists explicit Auto so a relaunch does not revert it to Single', async () => {
    let prefs = await import('./tabstrip-prefs')
    prefs.setTabStripDefault('auto')
    expect(localStorage.getItem(key)).toBe('auto')
    vi.resetModules()
    prefs = await import('./tabstrip-prefs')
    expect(prefs.$tabStripDefault.get()).toBe('auto')
    prefs.setTabStripDefault('single')
    expect(localStorage.getItem(key)).toBeNull()
  })

  it('retains per-zone choices for side-panel chrome', async () => {
    const prefs = await import('./tabstrip-prefs')
    expect(prefs.effectiveTabStripMode('always')).toBe('always')
    expect(prefs.effectiveTabStripMode('never')).toBe('never')
  })
})
