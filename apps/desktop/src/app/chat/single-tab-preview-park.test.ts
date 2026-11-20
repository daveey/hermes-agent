import { atom } from 'nanostores'
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'

import { allPaneIds, findGroupOfPane, group } from '@/components/pane-shell/tree/model'
import { $parkedTreePanes } from '@/components/pane-shell/tree/parked-panes'
import {
  $layoutTree,
  adoptContributedPanes,
  declareDefaultTree,
  noteActiveTreeGroup,
  revealTreePane,
  watchContributedPanes
} from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import { $previewTabs, newBrowserTab, openPreview } from '@/store/preview'
import { $selectedStoredSessionId } from '@/store/session'
import { $focusedStoredSessionId } from '@/store/session-focus'
import { $sessionTiles } from '@/store/session-states'
import { setTabStripDefault } from '@/store/tabstrip-prefs'

import { paneMirror } from './pane-mirror'
import { watchPreviewTiles } from './preview-tile'

vi.mock('./right-rail/preview', () => ({ PreviewTilePane: () => null }))
vi.mock('./right-rail/preview-console-store', () => ({ forgetPreviewConsole: () => undefined }))

beforeAll(() => {
  watchContributedPanes()
  paneMirror({
    source: $sessionTiles,
    key: tile => tile.storedSessionId,
    prefix: 'session-tile',
    title: id => id,
    minWidth: '22vw',
    dir: () => 'center',
    anchor: () => 'workspace',
    render: () => null,
    close: () => undefined
  })()
  watchPreviewTiles()
})

beforeEach(() => {
  window.localStorage.clear()
  $previewTabs.set([])
  $sessionTiles.set([])
  $layoutTree.set(null)
  $selectedStoredSessionId.set(null)
  noteActiveTreeGroup(null)
  $parkedTreePanes.set(new Set())
  registry.register({
    area: 'panes',
    data: { placement: 'main', uncloseable: true },
    id: 'workspace',
    render: () => null,
    title: 'workspace'
  })
  declareDefaultTree(group(['workspace'], { active: 'workspace', id: 'main' }))
})

afterEach(() => {
  $previewTabs.set([])
  $sessionTiles.set([])
  $layoutTree.set(null)
  $selectedStoredSessionId.set(null)
  noteActiveTreeGroup(null)
})

function previewIds(): string[] {
  return $previewTabs.get().map(tab => `preview-tile:${tab.id}`)
}

function dockedPreviews(): string[] {
  const tree = $layoutTree.get()

  return tree ? allPaneIds(tree).filter(id => id.startsWith('preview-tile:')) : []
}

it('keeps a hidden session Browser parked when Single refills after a registry change', () => {
  setTabStripDefault('single')
  $sessionTiles.set([{ storedSessionId: 'tile-a' }, { storedSessionId: 'tile-b' }])
  $selectedStoredSessionId.set('tile-a')
  revealTreePane('session-tile:tile-a')
  noteActiveTreeGroup('main')
  expect($focusedStoredSessionId.get()).toBe('tile-a')

  openPreview({ kind: 'url', label: 'Browser', source: 'https://a.example/', url: 'https://a.example/' })
  const first = previewIds()[0]!
  newBrowserTab()
  expect(previewIds()).toHaveLength(2)

  revealTreePane('session-tile:tile-b')
  noteActiveTreeGroup(findGroupOfPane($layoutTree.get()!, 'session-tile:tile-b')?.id ?? 'main')
  expect($focusedStoredSessionId.get()).toBe('tile-b')

  registry.register({
    area: 'panes',
    data: { placement: 'right' },
    id: `probe-${Date.now()}`,
    render: () => null,
    title: 'probe'
  })
  adoptContributedPanes()

  expect(dockedPreviews()).not.toContain(first)
  expect(findGroupOfPane($layoutTree.get()!, first)).toBeNull()
  expect($parkedTreePanes.get().has(first)).toBe(true)
})

it('does not dock a hidden session Browser under Auto either', () => {
  setTabStripDefault('auto')
  $sessionTiles.set([{ storedSessionId: 'tile-a' }, { storedSessionId: 'tile-b' }])
  $selectedStoredSessionId.set('tile-a')
  revealTreePane('session-tile:tile-a')
  noteActiveTreeGroup('main')
  openPreview({ kind: 'url', label: 'Browser', source: 'https://a.example/', url: 'https://a.example/' })
  const first = previewIds()[0]!
  newBrowserTab()
  revealTreePane('session-tile:tile-b')
  noteActiveTreeGroup(findGroupOfPane($layoutTree.get()!, 'session-tile:tile-b')?.id ?? 'main')
  registry.register({
    area: 'panes',
    data: { placement: 'right' },
    id: `probe-auto-${Date.now()}`,
    render: () => null,
    title: 'probe'
  })
  adoptContributedPanes()
  expect(dockedPreviews()).not.toContain(first)
  expect($focusedStoredSessionId.get()).toBe('tile-b')
})

it('leaving Single does not unpark panes a retaining mirror still holds off screen', () => {
  setTabStripDefault('single')
  const source = atom([{ id: 'kept' }])
  paneMirror({
    source,
    key: tile => tile.id,
    prefix: 'retain-tile',
    title: id => id,
    minWidth: '10rem',
    dir: () => 'center',
    anchor: () => 'workspace',
    render: () => null,
    close: () => undefined,
    retain: () => true,
    retainLimit: 4
  })()

  source.set([{ id: 'kept' }])
  revealTreePane('retain-tile:kept')
  source.set([])
  expect($parkedTreePanes.get().has('retain-tile:kept')).toBe(true)
  expect(allPaneIds($layoutTree.get()!)).not.toContain('retain-tile:kept')

  setTabStripDefault('auto')
  expect($parkedTreePanes.get().has('retain-tile:kept')).toBe(true)
  expect(allPaneIds($layoutTree.get()!)).not.toContain('retain-tile:kept')
})

it('keeps both focused-session Browser tabs in Single instead of replacing the first', () => {
  setTabStripDefault('single')
  $sessionTiles.set([{ storedSessionId: 'tile-a' }])
  $selectedStoredSessionId.set('tile-a')
  revealTreePane('session-tile:tile-a')
  noteActiveTreeGroup('main')
  openPreview({ kind: 'url', label: 'Browser', source: 'https://a.example/', url: 'https://a.example/' })
  newBrowserTab()
  expect(previewIds()).toHaveLength(2)
  expect(dockedPreviews().sort()).toEqual([...previewIds()].sort())
})
