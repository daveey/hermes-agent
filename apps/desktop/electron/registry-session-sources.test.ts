import { describe, expect, it } from 'vitest'

import { hasPinnedRegistrySessionSource, mergeRegistrySessionRows } from './profile-session-routing'
import { collectRegistrySessionSources } from './registry-session-sources'
import { resolveSharedSshProfileBackend, sharedSshProfiles } from './ssh-shared-profile-backend'

// Box (SSH) is the window's primary; a URL remote is connected as a registry
// gateway. Box's registry routes reuse the primary descriptor, so the pool only
// holds the URL remote.
const primary = { baseUrl: 'http://127.0.0.1:4310', mode: 'remote', remoteKind: 'ssh', profile: 'default' }
const lan = { baseUrl: 'http://127.0.0.1:4320', mode: 'remote', remoteKind: 'url' }

const rowsByHost: Record<string, unknown[]> = {
  [primary.baseUrl]: [
    { id: 'box-default-1', profile: 'default', last_active: 3 },
    { id: 'box-gamma-1', profile: 'gamma', last_active: 2 }
  ],
  [lan.baseUrl]: [{ id: 'lan-eps-1', profile: 'eps', last_active: 1 }]
}

async function getJson(descriptor: unknown, path: string) {
  const rows = rowsByHost[(descriptor as { baseUrl: string }).baseUrl] ?? []

  return path.startsWith('/api/profiles/sessions') ? { sessions: rows, total: rows.length } : { sessions: [], total: 0 }
}

describe('registry session sources with the SSH primary reused', () => {
  it('keeps the primary SSH gateway in the All-profiles aggregate after a named profile rides it', async () => {
    // Selecting box/gamma: gamma rides the default backend, which is the primary.
    const gamma = await resolveSharedSshProfileBackend({ id: 'box', kind: 'ssh' }, 'gamma', {
      ensureDefault: async () => ({ ...primary, connectionId: 'box' }),
      listProfiles: async () => ({ profiles: [{ name: 'default' }, { name: 'gamma' }] })
    })

    expect(gamma?.baseUrl).toBe(primary.baseUrl)

    const sources = await collectRegistrySessionSources(
      {
        connections: [
          { id: 'local', kind: 'local' },
          { id: 'box', kind: 'ssh' },
          { id: 'lan', kind: 'remote' }
        ],
        pool: [['conn:lan::default', { connectionPromise: Promise.resolve(lan) }]],
        scopePrefix: id => `conn:${id}::`,
        reusedPrimary: async id => (id === 'box' ? primary : null),
        installId: async id => `install-${id}`,
        sharedProfiles: sharedSshProfiles
      },
      true
    )

    expect(hasPinnedRegistrySessionSource('box', 'gamma', sources, false)).toBe(true)

    const merged: unknown[] = []
    await mergeRegistrySessionRows(sources, new URLSearchParams('limit=50'), getJson, merged, {})

    const scopes = merged.map(row => {
      const { connection_id, id } = row as { connection_id: string; id: string }

      return `${connection_id}/${id}`
    })

    expect(scopes.sort()).toEqual(['box/box-default-1', 'box/box-gamma-1', 'lan/lan-eps-1'])
  })

  it('reuses the primary aggregate for the reused SSH primary instead of re-reading that backend', async () => {
    const deps = {
      connections: [
        { id: 'box', kind: 'ssh' },
        { id: 'lan', kind: 'remote' }
      ],
      pool: [['conn:lan::default', { connectionPromise: Promise.resolve(lan) }]] as Array<
        [string, { connectionPromise: Promise<unknown> }]
      >,
      scopePrefix: (id: string) => `conn:${id}::`,
      reusedPrimary: async (id: string) => (id === 'box' ? primary : null),
      installId: async (id: string) => `install-${id}`,
      sharedProfiles: sharedSshProfiles
    }

    const sources = await collectRegistrySessionSources(deps, false)
    expect(sources.find(source => source.connectionId === 'box')?.reusesPrimary).toBe(true)
    expect(sources.find(source => source.connectionId === 'lan')?.reusesPrimary).toBeUndefined()

    const read = async (base: unknown) => {
      const hosts: string[] = []

      const counting = async (descriptor: unknown, path: string) => {
        hosts.push((descriptor as { baseUrl: string }).baseUrl)

        return JSON.parse(JSON.stringify(await getJson(descriptor, path)))
      }

      const merged: unknown[] = []
      const totals: Record<string, number> = {}
      await mergeRegistrySessionRows(sources, new URLSearchParams('limit=500'), counting, merged, totals, { base })

      return { hosts, merged, totals }
    }

    const fetched = await read(null)
    // Without the reuse the same backend is scanned again (five 100-row pages
    // per 500-row refresh against a real history; one here, as it runs short).
    expect(fetched.hosts.filter(host => host === primary.baseUrl).length).toBeGreaterThan(0)

    const primaryRows: Array<Record<string, unknown>> = [
      { id: 'box-default-1', profile: 'default', last_active: 3 },
      { id: 'box-gamma-1', profile: 'gamma', last_active: 2 }
    ]
    const first = { sessions: primaryRows, total: 2, profile_totals: { default: 1, gamma: 1 } }
    const reused = await read(first)

    expect(reused.hosts).not.toContain(primary.baseUrl)
    expect(reused.hosts).toContain(lan.baseUrl)
    expect(reused.merged).toEqual(fetched.merged)
    expect(reused.totals).toEqual(fetched.totals)
    // The primary read is shared with the base list: tagging must not mutate it.
    expect(primaryRows.every(row => !('connection_id' in row))).toBe(true)

    // A degraded primary read (scan errors) is never reused as authoritative.
    const degraded = await read({ ...first, errors: [{ profile: 'gamma', error: 'locked' }] })
    expect(degraded.hosts).toContain(primary.baseUrl)
    expect(degraded.merged).toEqual(fetched.merged)
  })
})
