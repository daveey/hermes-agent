import { beforeEach, describe, expect, it, vi } from 'vitest'

const LEGACY_VISIBLE_KEY = 'hermes.desktop.statusbarVisible'

const loadStore = () => import('./statusbar-prefs')

describe('statusbar whole-bar visibility', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('shows the bar on a fresh install and for installs that hid it under the v1 key', async () => {
    window.localStorage.setItem(LEGACY_VISIBLE_KEY, 'false')

    const { $statusbarVisible } = await loadStore()

    expect($statusbarVisible.get()).toBe(true)
  })

  it('still honours a hide made after the update', async () => {
    const first = await loadStore()

    first.toggleStatusbarVisible()
    expect(first.$statusbarVisible.get()).toBe(false)

    vi.resetModules()
    const reloaded = await loadStore()

    expect(reloaded.$statusbarVisible.get()).toBe(false)
  })
})

describe('statusbar hidden items', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('surfaces the approval pill for installs that hid it under the v1 defaults, keeping their other choices', async () => {
    window.localStorage.setItem(
      'hermes.desktop.statusbarHidden',
      JSON.stringify(['approval-mode', 'cron', 'gateway-health'])
    )

    const { $statusbarHiddenIds } = await loadStore()

    expect($statusbarHiddenIds.get()).toEqual(['cron', 'gateway-health', 'version-client', 'version-backend'])
  })

  it.each([{ hidden: [] }, { hidden: ['approval-mode', 'gateway-health', 'plugin-custom', 'version-client'] }])(
    'migrates v2 once without changing unrelated preferences: $hidden',
    async ({ hidden }) => {
      window.localStorage.setItem('hermes.desktop.statusbarHidden.v2', JSON.stringify(hidden))
      const first = await loadStore()
      expect(first.$statusbarHiddenIds.get()).toEqual([...new Set([...hidden, 'version-client', 'version-backend'])])

      first.setStatusbarItemVisible('version-client', true)
      vi.resetModules()
      const second = await loadStore()
      expect(second.$statusbarHiddenIds.get()).not.toContain('version-client')
      expect(second.$statusbarHiddenIds.get()).toContain('version-backend')

      second.setStatusbarItemVisible('version-backend', true)
      vi.resetModules()
      const third = await loadStore()
      expect(third.$statusbarHiddenIds.get()).toEqual(hidden.filter(id => !id.startsWith('version-')))
      third.setStatusbarItemVisible('version-client', false)
      vi.resetModules()
      expect((await loadStore()).$statusbarHiddenIds.get()).toEqual([
        ...hidden.filter(id => !id.startsWith('version-')),
        'version-client'
      ])
    }
  )

  it('hides both versions on fresh installs and reset, without hiding the approval pill', async () => {
    const store = await loadStore()
    expect(store.$statusbarHiddenIds.get()).toContain('version-client')
    expect(store.$statusbarHiddenIds.get()).toContain('version-backend')
    expect(store.$statusbarHiddenIds.get()).not.toContain('approval-mode')
    store.$statusbarHiddenIds.set([])
    vi.resetModules()
    const reloaded = await loadStore()
    expect(reloaded.$statusbarHiddenIds.get()).toEqual([])
    reloaded.resetStatusbarLayout()
    expect(reloaded.$statusbarHiddenIds.get()).toEqual([...reloaded.STATUSBAR_HIDDEN_BY_DEFAULT])
  })

  it.each(['gateway-switcher', 'profile-switcher', 'runners:ci-chip', 'account-usage:chips', 'throughput:chip'])(
    'persists independent optional control toggles across reload: %s',
    async id => {
      const first = await loadStore()
      const original = first.$statusbarHiddenIds.get()
      first.setStatusbarItemVisible(id, false)
      vi.resetModules()
      const hidden = await loadStore()
      expect(hidden.$statusbarHiddenIds.get()).toEqual([...original, id])
      hidden.setStatusbarItemVisible(id, true)
      vi.resetModules()
      expect((await loadStore()).$statusbarHiddenIds.get()).toEqual(original)
    }
  )

  it('still honours hiding the approval pill after the update', async () => {
    const first = await loadStore()

    first.setStatusbarItemVisible('approval-mode', false)

    vi.resetModules()
    const reloaded = await loadStore()

    expect(reloaded.$statusbarHiddenIds.get()).toContain('approval-mode')
  })
})
