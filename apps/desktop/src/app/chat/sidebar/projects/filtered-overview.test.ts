import { expect, it } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { makeSessionInfo } from '@/test/session-info'

import { filteredOverview } from './filtered-overview'
import { gatewayProjectId, sessionIdentityKey } from './gateway-project-rows'
import { knownProjectsOf, projectOwnerBySessionId, type SidebarProjectTree } from './workspace-groups'

const foreignOwner = (row: SessionInfo) => row.connection_id || null
const ownerKey = (row: SessionInfo) => sessionIdentityKey(foreignOwner(row) ?? '', row)

const node = (id: string, rows: SessionInfo[] = []): SidebarProjectTree => ({
  id,
  label: id,
  path: id,
  repos: [],
  sessionCount: rows.length,
  sessionIds: rows.map(row => row.id),
  previewSessions: rows.slice(0, 3)
})

const derive = (
  tree: SidebarProjectTree[],
  rows: SessionInfo[],
  hidden = (_row: SessionInfo) => false,
  narrow = true
) =>
  filteredOverview(
    tree,
    rows,
    [],
    narrow,
    hidden,
    {
      owners: projectOwnerBySessionId(tree),
      known: knownProjectsOf(tree),
      ownerKey
    },
    foreignOwner
  )!

it('retains the original structural tree without narrowing', () => {
  const tree = [node('/a')]
  expect(derive(tree, [], () => true, false)).toBe(tree)
})

it('does not turn snapshot-only matches into loaded filter results or mutate structural membership', () => {
  const stale = makeSessionInfo({ id: 'stale', cwd: '/a' })
  const tree = [node('/a', [stale])]
  expect(derive(tree, [])).toEqual([])
  expect(tree[0].previewSessions).toEqual([stale])
  expect(tree[0].sessionIds).toEqual(['stale'])
  expect(tree[0].filteredOverview).toBeUndefined()
})

it('retains complete owner/profile membership with empty previews and missing folder paths', () => {
  const rows = ['default', 'research'].map(profile =>
    makeSessionInfo({
      id: 'same-id',
      profile,
      connection_id: 'remote',
      title: profile
    })
  )

  const tree = rows.map(row => ({
    ...node(`remote-${row.profile}`),
    connectionId: 'remote',
    profile: row.profile,
    sessionMembership: [{ id: row.id, profile: row.profile!, connection_id: 'remote' }]
  }))

  const result = derive(tree, rows)
  expect(result.map(project => [project.id, project.previewSessions?.map(row => row.profile)])).toEqual([
    ['remote-default', ['default']],
    ['remote-research', ['research']]
  ])
})

it('isolates same-path foreign rows and synthesised foreign buckets from local placement', () => {
  const local = makeSessionInfo({ id: 'local', cwd: '/a', title: 'local' })
  const remote = makeSessionInfo({ id: 'remote', cwd: '/a', connection_id: 'remote', title: 'remote' })
  const remoteId = gatewayProjectId('remote', '/a')
  const tree = [node('/a', [local]), { ...node(remoteId), connectionId: 'remote' }]
  expect(
    derive(tree, [local, remote], row => row.id === 'local').map(project => [
      project.id,
      project.previewSessions?.map(row => row.title)
    ])
  ).toEqual([[remoteId, ['remote']]])
})
