import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { atom, type WritableAtom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { makeSessionInfo } from '@/test/session-info'

const state = vi.hoisted(() => ({ focus: vi.fn(), reveal: vi.fn(), note: vi.fn() }))
vi.mock('@/components/pane-shell/tree/store', () => ({ revealTreePane: state.reveal, noteActiveTreeGroup: state.note }))
vi.mock('@/i18n', () => ({ useI18n: () => ({ t: { sidebar: { nav: { 'new-session': 'New session' } } } }) }))
vi.mock('@/store/session-states', () => ({ $sessionTiles: atom([]), focusOpenSession: state.focus }))
vi.mock('@/store/session-focus', () => ({ $focusedStoredSessionId: atom(null), $focusedSessionIsTile: atom(false) }))
vi.mock('@/store/session', () => ({
  $configuredDefaultProjectDir: atom(''),
  $connection: atom(null),
  $currentCwd: atom(''),
  $newChatWorkspaceTarget: atom(undefined),
  $workspaceCwdOwner: atom(null),
  $freshDraftReady: atom(false),
  $selectedStoredSessionId: atom(null),
  $activeSessionId: atom(null),
  $sessions: atom([]),
  $unlistedSessionOwnerRows: atom([])
}))
vi.mock('@/store/composer', () => ({
  $draftTitles: atom({}),
  $freshDraftKey: atom('__new__:test'),
  takeSessionDraft: () => ({ text: '', attachments: [] })
}))
vi.mock('@/store/projects', async () => {
  const { computed } = await import('nanostores')
  const { projectOwnerBySessionId } = await import('./projects/workspace-groups')
  const $projectTree = atom([])

  return {
    $projectTree,
    $projects: atom([]),
    $projectOwnerBySessionId: computed($projectTree, projectOwnerBySessionId),
    resolveNewSessionCwd: () => ''
  }
})
vi.mock('react-router', () => ({ useLocation: () => ({ pathname: '/' }) }))

import { $draftTitles } from '@/store/composer'
import { $projects, $projectTree } from '@/store/projects'
import { $currentCwd } from '@/store/session'
import {
  $activeSessionId,
  $freshDraftReady,
  $selectedStoredSessionId,
  $sessions,
  $unlistedSessionOwnerRows
} from '@/store/session'
import { $focusedSessionIsTile, $focusedStoredSessionId } from '@/store/session-focus'
import { $sessionTiles } from '@/store/session-states'

import { SidebarDraftRows } from './draft-rows'

const focusedId = $focusedStoredSessionId as WritableAtom<string | null>
const focusedTile = $focusedSessionIsTile as WritableAtom<boolean>
const rows = () => [...document.querySelectorAll('[data-sidebar-draft-id], [data-real-row]')]

const view = (props = {}) => (
  <>
    <SidebarDraftRows {...props} />
    <div data-real-row="older">Older session</div>
  </>
)

beforeEach(() => {
  $freshDraftReady.set(false)
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  focusedId.set(null)
  focusedTile.set(false)
  $currentCwd.set('')
  $projects.set([])
  $projectTree.set([])

  $sessions.set([])
  $unlistedSessionOwnerRows.set([])
  $sessionTiles.set([])
  $draftTitles.set({})
  vi.clearAllMocks()
})
afterEach(cleanup)

describe('client-only sidebar drafts', () => {
  it.each([
    { name: 'repo subfolder', cwd: '/repo/src', bucket: '/repo' },
    { name: 'in-tree worktree', cwd: '/repo/.worktrees/task', bucket: '/repo' },
    { name: 'sibling lane', cwd: '/lane-task', bucket: '/repo' },
    { name: 'root control', cwd: '/repo', bucket: '/repo' },
    { name: 'unclaimed folder', cwd: '/unclaimed', bucket: '__no_project__' },
    { name: 'explicit p1 sibling lane', cwd: '/lane-task', bucket: 'p1', explicit: true }
  ])('uses real sidebar placement inputs for $name', ({ cwd, bucket, explicit }) => {
    $freshDraftReady.set(true)
    $currentCwd.set(cwd)

    if (explicit) {
      $projects.set([{ id: 'p1', name: 'Project', folders: [{ path: '/repo' }] }] as never)
    }

    $projectTree.set([
      {
        id: explicit ? 'p1' : '/repo',
        label: 'Repo',
        path: '/repo',
        sessionCount: 0,
        repos: [
          {
            id: '/repo',
            label: 'Repo',
            path: '/repo',
            sessionCount: 0,
            groups: [{ id: 'lane', label: 'Lane', path: '/lane-task', sessions: [] }]
          }
        ]
      }
    ])
    render(view({ projectId: bucket, filter: [bucket] }))
    expect(rows()).toHaveLength(2)
    expect(rows()[0].getAttribute('data-sidebar-draft-project')).toBe(bucket)
  })

  it('moves the main draft with the live send target when a tile publishes cwd', () => {
    $projects.set([{ id: '/other-project', name: 'Other', folders: [{ path: '/other-project' }] }] as never)
    $freshDraftReady.set(true)
    const { rerender } = render(view({ projectId: '__no_project__' }))
    expect(rows()).toHaveLength(2)
    act(() => {
      $sessionTiles.set([{ storedSessionId: 'project-tile', runtimeId: 'tile-runtime' }])
      focusedId.set('project-tile')
      focusedTile.set(true)
      $currentCwd.set('/other-project')
    })
    expect(rows()).toHaveLength(1)
    rerender(view({ projectId: '/other-project' }))
    expect(rows()).toHaveLength(2)
    expect(screen.getByRole('button', { name: 'New session' }).getAttribute('aria-current')).toBeNull()
  })

  it('drops an untouched tab on navigation and never paints bot or archived drafts', () => {
    $unlistedSessionOwnerRows.set([makeSessionInfo({ id: 'draft' })])
    $sessionTiles.set([{ storedSessionId: 'draft', runtimeId: 'runtime' }])
    focusedId.set('draft')
    const { rerender } = render(view())
    expect(rows()).toHaveLength(2)
    act(() => focusedId.set('other'))
    expect(rows()).toHaveLength(1)
    act(() => focusedId.set('draft'))
    rerender(view({ archived: true }))
    expect(rows()).toHaveLength(1)
    act(() => $sessionTiles.set([{ storedSessionId: 'draft', runtimeId: 'runtime', workspaceMode: 'bots' }]))
    rerender(view())
    expect(rows()).toHaveLength(1)
  })

  it('shows the fresh main draft before older rows and focuses it without creating a session', () => {
    $freshDraftReady.set(true)
    render(view())
    expect(rows()).toHaveLength(2)
    expect(rows()[0].getAttribute('data-sidebar-draft-id')).toBe('__new__:test')
    expect(screen.getByRole('button', { name: 'New session' }).getAttribute('aria-current')).toBe('page')
    fireEvent.click(screen.getByRole('button', { name: 'New session' }))
    expect(state.reveal).toHaveBeenCalledWith('workspace')
    expect(state.note).toHaveBeenCalledWith(null)
    expect($sessions.get()).toEqual([])
  })

  it('shows a listed:false tab exactly once, previews text, and focuses that tab', () => {
    const stub = makeSessionInfo({ id: 'draft', cwd: null, connection_id: 'hub' })
    $unlistedSessionOwnerRows.set([stub, stub])
    $sessionTiles.set([{ storedSessionId: 'draft', runtimeId: 'runtime' }])
    focusedId.set('draft')
    focusedTile.set(true)
    $draftTitles.set({ draft: 'Typed preview' })
    render(view())
    expect(rows()).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: 'Typed preview' }))
    expect(state.focus).toHaveBeenCalledWith('draft')
    expect(document.querySelector('[data-sidebar-stripe]')).toBeNull()
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('removes an abandoned empty draft and retains a touched tab', () => {
    $freshDraftReady.set(true)
    const { rerender } = render(view())
    act(() => {
      $selectedStoredSessionId.set('other')
      $freshDraftReady.set(false)
    })
    expect(rows()).toHaveLength(1)
    act(() => {
      $unlistedSessionOwnerRows.set([makeSessionInfo({ id: 'draft' })])
      $sessionTiles.set([{ storedSessionId: 'draft', runtimeId: 'runtime' }])
      $draftTitles.set({ draft: 'Keep this draft' })
    })
    rerender(view())
    expect(screen.getByRole('button', { name: 'Keep this draft' })).toBeTruthy()
  })

  it.each(['main', 'tab'])('replaces %s with one optimistic row in one render before refresh', kind => {
    if (kind === 'main') {
      $freshDraftReady.set(true)
    } else {
      $unlistedSessionOwnerRows.set([makeSessionInfo({ id: 'created' })])
      $sessionTiles.set([{ storedSessionId: 'created', runtimeId: 'runtime' }])
      focusedId.set('created')
    }

    const { rerender } = render(view())
    expect(rows()).toHaveLength(2)
    act(() => {
      $sessions.set([makeSessionInfo({ id: 'created', preview: 'First send' })])

      if (kind === 'main') {
        $activeSessionId.set('runtime')
        $selectedStoredSessionId.set('created')
      }
    })
    rerender(
      <>
        <SidebarDraftRows />
        <div data-real-row="created">First send</div>
        <div data-real-row="older">Older</div>
      </>
    )
    expect(rows()).toHaveLength(2)
    expect(rows()[0].getAttribute('data-real-row')).toBe('created')
    expect(document.querySelectorAll('[data-sidebar-draft-id]')).toHaveLength(0)
  })

  it('places detached drafts only in Home and project drafts only in that project', () => {
    $projects.set([{ id: '/repo', name: 'Repo', folders: [{ path: '/repo' }] }] as never)
    $freshDraftReady.set(true)
    const { rerender } = render(view({ projectId: '/repo' }))
    expect(rows()).toHaveLength(1)
    rerender(view({ projectId: '__no_project__' }))
    expect(rows()).toHaveLength(2)
    act(() => $currentCwd.set('/repo'))
    rerender(view({ projectId: '/repo' }))
    expect(rows()).toHaveLength(2)
    rerender(view({ filter: ['__no_project__'] }))
    expect(rows()).toHaveLength(1)
  })
})
