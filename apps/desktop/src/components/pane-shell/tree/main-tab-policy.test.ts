import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { shownGroupPanes, singleMainTabPanes } from './main-tab-policy'

const panes = ['workspace', 'session-tile:a', 'route-tile:settings', 'terminal', 'files']
const isMain = (id: string) => !['terminal', 'files'].includes(id)

it('shares hidden, unregistered and narrow-collapsed eligibility with keyboard slots', () => {
  const registered = panes.map(id => ({
    area: 'panes',
    data: { collapsible: id === 'files', placement: isMain(id) ? 'main' : 'right' },
    id,
    render: () => null,
    title: id
  }))

  const group = { active: 'session-tile:a', panes: [...panes, 'unregistered'] }
  expect(shownGroupPanes(group, registered, new Set(['terminal']), true, true)).toEqual(['session-tile:a'])
  expect(shownGroupPanes(group, registered, new Set(), false, false)).toEqual(panes)
})

it('retains the active main slot and all standing side/tool panels', () => {
  expect(singleMainTabPanes({ active: 'session-tile:a', panes }, panes, isMain, true)).toEqual([
    'session-tile:a', 'terminal', 'files'
  ])
})

it('does not close background owners or mutate their persisted tree', () => {
  const group = { active: 'session-tile:a', panes: [...panes] }
  singleMainTabPanes(group, group.panes, isMain, true)
  expect(group.panes).toEqual(panes)
  expect(group.active).toBe('session-tile:a')
})

it('retains a main slot while a side panel is active', () => {
  expect(singleMainTabPanes({ active: 'terminal', panes }, panes, isMain, true)).toEqual([
    'workspace', 'terminal', 'files'
  ])
})

it('never revives a pane excluded by visibility or registry filtering', () => {
  expect(singleMainTabPanes({ active: 'session-tile:a', panes }, ['files'], isMain, true)).toEqual(['files'])
})

it('does not collapse side-panel-only tab groups', () => {
  expect(singleMainTabPanes({ active: 'terminal', panes }, ['terminal', 'files'], isMain, true)).toEqual([
    'terminal', 'files'
  ])
})

it('leaves Auto/Always/Never tab lists unchanged', () => {
  expect(singleMainTabPanes({ active: 'workspace', panes }, panes, isMain, false)).toEqual(panes)
})

describe('Single main slots through the actual tree store', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.resetModules()
  })

  afterEach(() => vi.resetModules())

  async function setup() {
    const store = await import('./store')
    const model = await import('./model')
    const { registry } = await import('@/contrib/registry')
    const prefs = await import('@/store/tabstrip-prefs')
    const parked = await import('./parked-panes')

    for (const id of panes) {
      registry.register({
        area: 'panes',
        data: { placement: isMain(id) ? 'main' : 'right' },
        id,
        render: () => null,
        title: id
      })
    }

    store.declareDefaultTree(model.group(panes, { active: 'session-tile:a', id: 'main' }))
    const shown = () => store.shownPanesInGroup(model.findGroup(store.$layoutTree.get()!, 'main')!)

    return { model, parked, prefs, shown, store }
  }

  it('defaults to Single, retaining the restored active main tab immediately', async () => {
    const { prefs, shown } = await setup()
    expect(prefs.$tabStripDefault.get()).toBe('single')
    expect(shown()).toEqual(['session-tile:a', 'terminal', 'files'])
  })

  it('collapses immediately on enabling Single and preserves the active tab', async () => {
    const { prefs, shown, store } = await setup()
    prefs.setTabStripDefault('auto')
    expect(shown()).toEqual(panes)
    store.activateTreePane('main', 'route-tile:settings')
    prefs.setTabStripDefault('single')
    expect(shown()).toEqual(['route-tile:settings', 'terminal', 'files'])
    prefs.setTabStripDefault('always')
    expect(shown()).toEqual(panes)
  })

  it('replaces the visible main slot on explicit reveal without closing the session', async () => {
    const { model, parked, shown, store } = await setup()
    store.revealTreePane('route-tile:settings')
    expect(shown()).toEqual(['route-tile:settings', 'terminal', 'files'])
    expect(model.allPaneIds(store.$layoutTree.get()!)).toEqual(['route-tile:settings', 'terminal', 'files'])
    expect(parked.$parkedTreePanes.get().has('session-tile:a')).toBe(true)
    store.revealTreePane('workspace')
    expect(shown()).toEqual(['workspace', 'terminal', 'files'])
  })

  it('keeps every side-panel tab available and excludes collapsed main siblings from keyboard slots', async () => {
    const { shown, store } = await setup()
    expect(shown()).toEqual(['session-tile:a', 'terminal', 'files'])
    expect(store.activateTreeTabSlot(2)).toBe('terminal')
    expect(shown()).toContain('files')
  })
})

it("keeps every Browser tab in a preview-only zone under Single", () => {
  const previews = ["preview-tile:url:one", "preview-tile:url:two"]
  const all = (id: string) => id.startsWith("preview-tile:")

  expect(singleMainTabPanes({ active: previews[1]!, panes: previews }, previews, all, true)).toEqual(previews)
})
