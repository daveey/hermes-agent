import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SidebarSessionsResponse } from './sessions'

vi.mock('@/lib/gateway-rpc', () => ({ isMissingRestEndpoint: () => false, isUnroutedRestPath: vi.fn(() => false) }))
const gatewayRpc = await import('@/lib/gateway-rpc')
vi.mock('@/store/transcript-tail', () => ({ pageHonorsLatestOrder: () => true, recordTranscriptTail: vi.fn() }))
vi.mock('./client', () => ({
  ambientOwnerConnectionId: vi.fn(() => 'prometheus'),
  capabilityScoped: vi.fn(),
  connectionScoped: vi.fn(() => ({})),
  getApiRequestConnection: vi.fn(() => 'prometheus'),
  getApiRequestProfile: vi.fn(() => null),
  hermesApi: vi.fn(),
  profileScoped: vi.fn(() => ({})),
  sessionReadOwnerPin: vi.fn(() => ({}))
}))

const client = await import('./client')

const {
  deleteSession,
  getSession,
  getLatestSessionMessages,
  renameSession,
  searchSessions,
  setSessionArchived,
  setSessionPinnedRemote,
  setSessionUnreadRemote,
  listSidebarSessions,
  listAllProfileSessions
} = await import('./sessions')

const hermesApi = vi.mocked(client.hermesApi)

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(client.getApiRequestConnection).mockReturnValue('prometheus')
  vi.mocked(client.getApiRequestProfile).mockReturnValue(null)
  // Session writes scope through the same helper as reads: a named owner is
  // its own scope, an unnamed one is the ambient (empty) scope.
  vi.mocked(client.capabilityScoped).mockImplementation(scope =>
    typeof scope === 'string' && scope.trim() ? { profile: scope.trim() } : {}
  )
})

describe('session write reconnect safety', () => {
  it('does not replay an identity-less legacy DELETE after losing its reply', async () => {
    vi.useFakeTimers()
    const missing = new Error('404: Not Found')
    const lostReply = new Error('socket hang up')
    vi.mocked(gatewayRpc.isUnroutedRestPath).mockImplementation(error => error === missing)
    hermesApi.mockImplementation(async request => {
      if (!request.method) { throw missing }
      throw lostReply
    })

    try {
      const result = deleteSession('legacy-lost-reply').catch(error => error)
      await vi.runAllTimersAsync()
      expect(await result).toMatchObject({ message: lostReply.message, cause: lostReply })
      const writes = hermesApi.mock.calls.filter(([request]) => request.method === 'DELETE')
      expect(writes).toHaveLength(1)
      expect(writes[0][0].path).not.toContain('request_id')
    } finally {
      vi.mocked(gatewayRpc.isUnroutedRestPath).mockReturnValue(false)
      vi.useRealTimers()
    }
  })

  it.each(['PATCH', 'DELETE'])('subtracts snapshot time and backoff from each %s timeout', async method => {
    vi.useFakeTimers()
    let writes = 0
    hermesApi.mockImplementation(async request => {
      if (!request.method) {
        await new Promise(resolve => setTimeout(resolve, 25_000))

        return { exists: true, runtime_revision: 7, runtime_generation: 3 } as never
      }

      writes += 1
      await new Promise(resolve => setTimeout(resolve, request.timeoutMs ?? 30_000))
      throw new Error('socket hang up')
    })

    try {
      const write = method === 'PATCH'
        ? setSessionArchived(`budget-snapshot-${method}`, true)
        : deleteSession(`budget-snapshot-${method}`)

      const result = write.catch(error => error)
      await vi.runAllTimersAsync()
      expect(await result).toBeInstanceOf(Error)
      expect(writes).toBe(2)
      expect(hermesApi.mock.calls.map(([request]) => request.timeoutMs)).toEqual([30_000, 30_000, 4_000])
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not send a write when the snapshot consumes the remaining deadline', async () => {
    vi.useFakeTimers()
    hermesApi.mockImplementation(async () => {
      await new Promise(resolve => setTimeout(resolve, 60_000))

      return { exists: true, runtime_revision: 7, runtime_generation: 3 } as never
    })

    try {
      const result = setSessionArchived('snapshot-deadline', true).catch(error => error)
      await vi.runAllTimersAsync()
      expect(await result).toBeInstanceOf(Error)
      expect(hermesApi).toHaveBeenCalledOnce()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('listAllProfileSessions automated rows', () => {
  it('asks the backend to leave automated rows out only when told to', async () => {
    hermesApi.mockResolvedValue({ sessions: [], total: 0 })

    await listAllProfileSessions(500, 0, 'only', 'recent', 'all', { automated: 'exclude' }, 500)
    await listAllProfileSessions(500, 0, 'only', 'recent', 'all', {}, 0)

    const paths = hermesApi.mock.calls.map(([request]) => request.path)

    expect(paths[0]).toContain('archived=only')
    expect(paths[0]).toContain('&automated=exclude')
    expect(paths[1]).not.toContain('automated')
  })
})

describe('deleteSession profile scoping', () => {
  it('scopes the DELETE to the owning profile in the URL (object owner)', async () => {
    // Regression: the sidebar "All Profiles" delete sent the profile only via
    // request.profile, not in the URL. On a remote gateway with no remoteProfile
    // alias the main-process path rewrite left the URL unscoped, so the backend
    // opened its own default state.db, missed the row, and returned
    // {ok:true, already_absent:true} — the row vanished optimistically but was
    // never deleted and came back on refresh. The URL must carry ?profile=.
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 5, runtime_generation: 2 } as never)
    // Mirrors the real capabilityScoped for an object owner (remote-stamped row).
    vi.mocked(client.capabilityScoped).mockReturnValue({ profile: 'tommy', connectionId: 'hermes-pi' })

    await deleteSession('sess-1', { connectionId: 'hermes-pi', profile: 'tommy' })

    expect(hermesApi.mock.calls.filter(([request]) => request.method)[0][0]).toMatchObject({
      method: 'DELETE',
      path: expect.stringContaining('/api/sessions/sess-1?profile=tommy'),
      connectionId: 'hermes-pi',
      profile: 'tommy'
    })
  })

  it('scopes the DELETE to the owning profile in the URL (bare string owner)', async () => {
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 5, runtime_generation: 2 } as never)
    // Bare-string owner: capabilityScoped resolves it to a profile scope.
    vi.mocked(client.capabilityScoped).mockReturnValue({ profile: 'tommy' })

    await deleteSession('sess-2', 'tommy')

    expect(hermesApi.mock.calls.filter(([request]) => request.method)[0][0]).toMatchObject({
      method: 'DELETE',
      path: expect.stringContaining('/api/sessions/sess-2?profile=tommy')
    })
  })

  it('omits the profile query when no owner is known', async () => {
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 5, runtime_generation: 2 } as never)

    await deleteSession('sess-3')

    expect(hermesApi.mock.calls.filter(([request]) => request.method)[0][0]).toMatchObject({
      method: 'DELETE',
      path: expect.stringContaining('/api/sessions/sess-3')
    })
    expect((hermesApi.mock.calls.filter(([request]) => request.method)[0][0] as { path: string }).path).not.toContain('profile=')
  })

  it('keeps an explicit local pin routed to the local pool', async () => {
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 5, runtime_generation: 2 } as never)
    // capabilityScoped drops a 'local' connection id by design; sessionScoped
    // must re-add it so the request stays pinned to this device.
    vi.mocked(client.capabilityScoped).mockReturnValue({ profile: 'tommy' })

    await deleteSession('sess-4', { connectionId: 'local', profile: 'tommy' })

    expect(hermesApi.mock.calls.filter(([request]) => request.method)[0][0]).toMatchObject({
      method: 'DELETE',
      path: expect.stringContaining('/api/sessions/sess-4?profile=tommy'),
      connectionId: 'local',
      profile: 'tommy'
    })
  })
})

describe('getSession dial priority', () => {
  it('does not dial an explicitly scoped session read foreground', async () => {
    // The scope helper tags every explicit scope foreground (#111651); the
    // cross-profile probe loop in resolveStoredSession would otherwise cold-start
    // every other profile on the reserved slot during a boot-time resume.
    hermesApi.mockResolvedValue({ id: 'sess-5' } as never)
    vi.mocked(client.capabilityScoped).mockReturnValue({ priority: 'foreground', profile: 'tommy' })

    await getSession('sess-5', { connectionId: 'local', profile: 'tommy' })

    expect(hermesApi.mock.calls[0][0]).toMatchObject({ profile: 'tommy', connectionId: 'local' })
    expect(hermesApi.mock.calls[0][0]).not.toHaveProperty('priority')
  })
})

describe('setSessionArchived profile scoping', () => {
  it('carries the owning profile in the PATCH body', async () => {
    // Same class as the unscoped DELETE: the PATCH handler reads its target DB
    // from body.profile, so archiving a foreign-profile session must send it in
    // the body, not only as request.profile (Electron routing), or on a remote
    // gateway the archive lands on the wrong state.db and silently no-ops.
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 5, runtime_generation: 2 } as never)

    await setSessionArchived('sess-a', true, 'tommy')

    expect(hermesApi.mock.calls.filter(([request]) => request.method)[0][0]).toMatchObject({
      method: 'PATCH',
      path: '/api/sessions/sess-a',
      profile: 'tommy',
      body: { archived: true, pinned: false, profile: 'tommy' }
    })
  })

  it('falls back to the ACTIVE profile in the body when no owner is given', async () => {
    // Multiplex-only: the PATCH handler resolves its state.db from
    // `body.profile` and there is no per-profile backend whose HERMES_HOME
    // could stand in. An unnamed owner therefore has to mean "the profile I am
    // looking at" — otherwise the archive lands on the shared backend's own
    // state.db and silently no-ops.
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 5, runtime_generation: 2 } as never)
    vi.mocked(client.getApiRequestProfile).mockReturnValue('beta')

    await setSessionArchived('sess-b', false)

    expect(hermesApi.mock.calls.filter(([request]) => request.method)[0][0]).toMatchObject({
      method: 'PATCH',
      profile: 'beta',
      body: { archived: false, profile: 'beta' }
    })
  })

  it('routes an object owner to ITS connection (All-Gateways row on another gateway)', async () => {
    // Regression: archiving a mac-mini row from the MacBook sidebar sent the
    // PATCH to the active (local) backend, which 404'd; the row reappeared.
    // Fork: archive mutations read a runtime snapshot first.
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 5, runtime_generation: 2 } as never)
    vi.mocked(client.capabilityScoped).mockReturnValue({ profile: 'default', connectionId: 'mac-mini' })

    await setSessionArchived('sess-remote', true, { connectionId: 'mac-mini', profile: 'default' })

    expect(hermesApi.mock.calls.filter(([request]) => request.method)[0][0]).toMatchObject({
      connectionId: 'mac-mini',
      method: 'PATCH',
      path: '/api/sessions/sess-remote',
      profile: 'default',
      body: { archived: true, profile: 'default' }
    })
  })

  it('sends a remote row\'s archive with the fence identity, read from ITS connection', async () => {
    // Regression (hub row 20261028_224631_5ff53d): a pre-fence client PATCHed with no
    // request_id/expected_revision and the hub answered 409 client_upgrade_required, so
    // the archive never landed. The snapshot read and the PATCH must share the row's
    // connection, and the PATCH must carry the identity the snapshot returned.
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 7, runtime_generation: 3 } as never)
    vi.mocked(client.capabilityScoped).mockReturnValue({ profile: 'default', connectionId: 'hermes-hub' })

    await setSessionArchived('sess-hub', true, { connectionId: 'hermes-hub', profile: 'default' })

    const calls = hermesApi.mock.calls.map(([request]) => request as Record<string, any>)
    const read = calls.find(request => String(request.path).includes('/mutation-snapshot'))
    const patch = calls.find(request => request.method === 'PATCH')!

    expect(read).toMatchObject({ connectionId: 'hermes-hub', path: expect.stringContaining('/api/sessions/sess-hub/mutation-snapshot') })
    expect(patch).toMatchObject({ connectionId: 'hermes-hub', method: 'PATCH', path: '/api/sessions/sess-hub' })
    expect(patch.body).toMatchObject({ archived: true, pinned: false, expected_revision: 7 })
    expect(typeof patch.body.request_id).toBe('string')
    expect(patch.body.request_id).not.toBe('')
  })

  it('replays a hub archive that hit a reconnect with the SAME identity, without a user retry', async () => {
    // Regression (MacBook 2026-11-12, hub chat 20261111_091916_d2d36a): the hub PATCH died with
    // "socket hang up", then the down gate failed the next tries fast ("reconnect in progress"), and
    // the user had to click Archive again. The write now waits out the reconnect and replays the
    // retained request_id/expected_revision, so an owner that already committed it answers from its receipt.
    vi.useFakeTimers()

    try {
      vi.mocked(client.capabilityScoped).mockReturnValue({ profile: 'default', connectionId: 'hermes-hub' })
      let patches = 0
      hermesApi.mockImplementation((async (request: { method?: string }) => {
        if (request.method !== 'PATCH') {
          return { exists: true, runtime_revision: 7, runtime_generation: 3 }
        }

        patches += 1

        if (patches === 1) {
          throw new Error("Error invoking remote method 'hermes:api': Error: socket hang up")
        }

        if (patches === 2) {
          throw new Error('Remote backend "conn:hermes-hub::default" is down (socket hang up, 3s ago); reconnect in progress. Failing fast instead of waiting for a timeout.')
        }

        return { ok: true }
      }) as never)

      const archived = setSessionArchived('sess-reconnect', true, { connectionId: 'hermes-hub', profile: 'default' })
      await vi.advanceTimersByTimeAsync(5_000)

      await expect(archived).resolves.toEqual({ ok: true })
      const sent = hermesApi.mock.calls.map(([request]) => request as Record<string, any>).filter(request => request.method === 'PATCH')
      expect(sent).toHaveLength(3)
      expect(new Set(sent.map(request => request.body.request_id)).size).toBe(1)
      expect(sent.every(request => request.connectionId === 'hermes-hub' && request.body.expected_revision === 7)).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not replay a definitive refusal (client_upgrade_required)', async () => {
    hermesApi.mockImplementation((async (request: { method?: string }) => {
      if (request.method !== 'PATCH') {
        return { exists: true, runtime_revision: 7, runtime_generation: 3 }
      }

      throw new Error('409: {"detail":"client_upgrade_required: this app is out of sync (different versions) with the Hermes backend; update the app to edit sessions"}')
    }) as never)

    await expect(setSessionArchived('sess-refused', true, 'default')).rejects.toThrow(/client_upgrade_required/)
    expect(hermesApi.mock.calls.filter(([request]) => request.method === 'PATCH')).toHaveLength(1)
  })

  it('omits the profile from the body only when there is no active profile at all', async () => {
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 5, runtime_generation: 2 } as never)

    await setSessionArchived('sess-b2', false)

    const req = hermesApi.mock.calls.filter(([request]) => request.method)[0][0] as { body: Record<string, unknown> }
    expect(req).toMatchObject({ method: 'PATCH', body: { archived: false } })
    expect(req.body).not.toHaveProperty('profile')
    expect(req.body).not.toHaveProperty('pinned')
  })
})

describe('setSessionPinnedRemote / setSessionUnreadRemote profile scoping', () => {
  it('carries the owning profile in the pin PATCH body', async () => {
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 5, runtime_generation: 2 } as never)

    await setSessionPinnedRemote('sess-p', true, 'tommy')

    expect(hermesApi.mock.calls.filter(([request]) => request.method)[0][0]).toMatchObject({
      method: 'PATCH',
      path: '/api/sessions/sess-p',
      profile: 'tommy',
      body: { pinned: true, profile: 'tommy' }
    })
  })

  it('carries the owning profile in the unread PATCH body', async () => {
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 5, runtime_generation: 2 } as never)

    await setSessionUnreadRemote('sess-u', true, 'tommy')

    expect(hermesApi.mock.calls.filter(([request]) => request.method)[0][0]).toMatchObject({
      method: 'PATCH',
      path: '/api/sessions/sess-u',
      profile: 'tommy',
      body: { unread: true, profile: 'tommy' }
    })
  })

  it('omits the profile from the body when none is given', async () => {
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 5, runtime_generation: 2 } as never)

    await setSessionPinnedRemote('sess-p2', false)

    const req = hermesApi.mock.calls.filter(([request]) => request.method)[0][0] as { body: Record<string, unknown> }
    expect(req).toMatchObject({ method: 'PATCH', body: { pinned: false } })
    expect(req.body).not.toHaveProperty('profile')
  })

  it('falls back to the ACTIVE profile in the body when no owner is given', async () => {
    hermesApi.mockResolvedValue({ ok: true, exists: true, runtime_revision: 5, runtime_generation: 2 } as never)
    vi.mocked(client.getApiRequestProfile).mockReturnValue('beta')

    await setSessionPinnedRemote('sess-p2', false)

    expect(hermesApi.mock.calls.filter(([request]) => request.method)[0][0]).toMatchObject({
      method: 'PATCH',
      profile: 'beta',
      body: { pinned: false, profile: 'beta' }
    })
  })
})

describe('renameSession profile scoping', () => {
  it('carries the owning profile in the PATCH body and request', async () => {
    hermesApi.mockResolvedValue({ ok: true, title: 'Prep Butler', exists: true, runtime_revision: 5, runtime_generation: 2 } as never)

    await renameSession('sess-r', 'Prep Butler', 'personal')

    expect(hermesApi.mock.calls.filter(([request]) => request.method)[0][0]).toMatchObject({
      method: 'PATCH',
      path: '/api/sessions/sess-r',
      profile: 'personal',
      body: { title: 'Prep Butler', profile: 'personal' }
    })
  })

  it('falls back to the active request profile when the argument is omitted', async () => {
    hermesApi.mockResolvedValue({ ok: true, title: 'Prep Butler', exists: true, runtime_revision: 5, runtime_generation: 2 } as never)
    vi.mocked(client.getApiRequestProfile).mockReturnValue('personal')

    await renameSession('sess-r2', 'Prep Butler')

    expect(hermesApi.mock.calls.filter(([request]) => request.method)[0][0]).toMatchObject({
      method: 'PATCH',
      path: '/api/sessions/sess-r2',
      profile: 'personal',
      body: { title: 'Prep Butler', profile: 'personal' }
    })
    vi.mocked(client.getApiRequestProfile).mockReturnValue(null)
  })
})

describe('listSidebarSessions remote ownership', () => {
  it('stamps active remote rows so a later resume stays on their gateway', async () => {
    hermesApi.mockResolvedValue({
      cron: { sessions: [] },
      messaging: { sessions: [] },
      recents: {
        sessions: [{ id: 'remote-session', profile: 'default', source: 'desktop', title: 'Remote chat' }]
      }
    } as never)

    const result = await listSidebarSessions({
      recentsProfile: 'default',
      recentsLimit: 40,
      recentsExclude: [],
      cronLimit: 20,
      messagingLimit: 40,
      messagingExclude: []
    })

    expect(result.recents.sessions[0]).toMatchObject({ connection_id: 'prometheus', id: 'remote-session' })
  })

  it("an explicit connection is asked directly and its rows never wear the window's connection", async () => {
    hermesApi.mockResolvedValue({
      cron: { sessions: [] },
      messaging: { sessions: [] },
      recents: { sessions: [{ id: 'own-session', profile: 'default', source: 'desktop', title: 'Own chat' }] }
    } as never)

    const result = await listSidebarSessions({
      connectionId: 'local',
      recentsProfile: 'default',
      recentsLimit: 40,
      recentsExclude: [],
      cronLimit: 20,
      messagingLimit: 40,
      messagingExclude: []
    })

    expect(hermesApi.mock.calls[0][0]).toMatchObject({ connectionId: 'local' })
    expect(result.recents.sessions[0].connection_id).toBeUndefined()
  })
})

describe('listSidebarSessions storage health', () => {
  it('passes the backend corrupt-store map through so the sidebar can say why it is empty', async () => {
    const response = {
      cron: { sessions: [] },
      errors: [{ error: 'database disk image is malformed', profile: 'default' }],
      messaging: { sessions: [] },
      recents: { sessions: [] },
      storage: { default: 'corrupt' }
    } satisfies SidebarSessionsResponse

    // SAFETY: vi cannot infer a concrete return from the generic hermesApi signature;
    // `satisfies` above checks the exact endpoint contract before it crosses the mock boundary.
    hermesApi.mockResolvedValue(response as never)

    const result = await listSidebarSessions({
      recentsProfile: 'all',
      recentsLimit: 40,
      recentsExclude: [],
      cronLimit: 20,
      messagingLimit: 40,
      messagingExclude: []
    })

    expect(result.storage).toEqual({ default: 'corrupt' })
  })
})

describe('session reads pin the owner connection (#125372)', () => {
  beforeEach(() => {
    // Ambient default: no explicit pin, no owner stamp.
    vi.mocked(client.capabilityScoped).mockReturnValue({})
    vi.mocked(client.sessionReadOwnerPin).mockReturnValue({})
  })

  it('pins getSession to the session owner when the ambient scope differs', async () => {
    // Regression: a session-scoped read dispatched on the WINDOW's ambient
    // connection scope. With a registered remote exposing a same-named
    // profile, the wrong backend answered 404 "Session not found" in both
    // directions. The read must carry the owner's connectionId (+ backend
    // profile when the caller named none) so Electron routes to the owner.
    vi.mocked(client.sessionReadOwnerPin).mockImplementation((id: string) =>
      id === 'remote-owned' ? { connectionId: 'dale-home-lan-9119', profile: 'default' } : {}
    )

    await getSession('remote-owned')

    expect(hermesApi.mock.calls[0][0]).toMatchObject({
      path: '/api/sessions/remote-owned?profile=default',
      connectionId: 'dale-home-lan-9119'
    })
  })

  it('keeps an explicit (connection, profile) pin authoritative in getLatestSessionMessages', async () => {
    // Explicit caller pin: the real capabilityScoped resolves it; the owner
    // lookup must not override it.
    vi.mocked(client.capabilityScoped).mockReturnValue({ profile: 'tommy', connectionId: 'other-conn' })

    await getLatestSessionMessages('sess-y', { connectionId: 'other-conn', profile: 'tommy' })

    expect(hermesApi.mock.calls[0][0]).toMatchObject({
      connectionId: 'other-conn',
      profile: 'tommy'
    })
  })
})

describe('searchSessions profile scope', () => {
  it('searches the given profile instead of the primary backend', async () => {
    // Unscoped, the primary searched its launch profile while the sidebar showed another.
    hermesApi.mockResolvedValue({ results: [] } as never)
    vi.mocked(client.profileScoped).mockImplementation(profile => (profile ? { priority: 'foreground', profile } : {}))

    await searchSessions('zebra', 'research')

    expect(hermesApi.mock.calls[0][0]).toMatchObject({
      path: '/api/sessions/search?q=zebra&profile=research',
      profile: 'research'
    })
  })
})
