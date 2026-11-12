import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  batchedPrimarySidebarProfile,
  fanOutSidebarSessionSlices,
  fetchPrimaryProfileSessions,
  fetchSidebarSessionsBatchedOrSliced,
  tagSidebarSlicesOwner
} from './profile-session-routing'

// [daveey] With registry gateways connected, every sidebar refresh of a concrete
// local profile used to cost three /api/profiles/sessions reads on the primary.
// The primary's batched /sidebar route serves the same three slices in one read.

type Call = { profile: string | null; path: string }

const QUERY =
  'recents_profile=default&recents_limit=20&cron_limit=50&messaging_limit=100&recents_exclude=cron&messaging_exclude=tui'

const batched = () => ({
  recents: {
    sessions: [
      { id: 'r1', profile: 'default' },
      { id: 'r2', profile: 'default', connection_id: 'mac-mini' }
    ],
    profiles_truncated: { default: false },
    profiles_usage: { default: { cost_usd: 1.5, tokens: 1200 } }
  },
  cron: { sessions: [{ id: 'c1', profile: 'default', connection_id: '  ' }] },
  messaging: { sessions: [{ id: 'm1', profile: 'default' }], total: 1 },
  errors: [{ profile: 'default', error: 'transient' }],
  storage: { other: 'corrupt' },
  profiles_failed: { other: { failed: true, retry: true } }
})

/** Today's per-slice path, built from the same module pieces main.ts uses for
 *  a concrete local profile (fetchProfilesSessionSlice -> fetchPrimaryProfileSessions). */
function sliceFanOut(
  searchParams: URLSearchParams,
  fetchJson: (profile: string | null, path: string) => Promise<unknown>,
  ownerTag: string | null
) {
  return () => fanOutSidebarSessionSlices(searchParams, sp => fetchPrimaryProfileSessions(sp, fetchJson, { ownerTag }))
}

function recorder(answer: (call: Call) => unknown) {
  const calls: Call[] = []

  const fetchJson = async (profile: string | null, path: string) => {
    calls.push({ profile, path })

    return answer({ profile, path })
  }

  return { calls, fetchJson }
}

test('a concrete local profile reads the whole sidebar from the primary in one batched request', async () => {
  const searchParams = new URLSearchParams(QUERY)
  const { calls, fetchJson } = recorder(() => batched())

  await fetchSidebarSessionsBatchedOrSliced(searchParams, {
    fanOut: sliceFanOut(searchParams, fetchJson, null),
    fetchJsonForProfile: fetchJson,
    hasRemoteOverride: () => false,
    ownerTag: null
  })

  assert.deepEqual(calls, [{ profile: null, path: `/api/profiles/sessions/sidebar?${QUERY}` }])
})

test('the batched response keeps its other keys and stamps the owner tag on bare rows of every slice', async () => {
  const searchParams = new URLSearchParams(QUERY)
  const response = batched()
  const before = structuredClone(response)
  const { fetchJson } = recorder(() => response)

  const result = (await fetchSidebarSessionsBatchedOrSliced(searchParams, {
    fanOut: async () => assert.fail('fan-out must not run'),
    fetchJsonForProfile: fetchJson,
    hasRemoteOverride: () => false,
    ownerTag: 'local'
  })) as ReturnType<typeof batched>

  assert.deepEqual(response, before, 'input not mutated')
  assert.deepEqual(
    (['recents', 'cron', 'messaging'] as const).map(key =>
      result[key].sessions.map((row: Record<string, unknown>) => [row.id, row.connection_id])
    ),
    [
      [
        ['r1', 'local'],
        ['r2', 'mac-mini']
      ],
      [['c1', 'local']],
      [['m1', 'local']]
    ]
  )
  assert.deepEqual(result.recents.profiles_truncated, before.recents.profiles_truncated)
  assert.deepEqual(result.recents.profiles_usage, before.recents.profiles_usage)
  assert.equal(result.messaging.total, 1)
  assert.deepEqual(result.errors, before.errors)
  assert.deepEqual(result.storage, before.storage)
  assert.deepEqual(result.profiles_failed, before.profiles_failed)
})

test('a null owner tag leaves the batched response untouched', async () => {
  const response = batched()
  assert.equal(tagSidebarSlicesOwner(response, null), response)

  const { fetchJson } = recorder(() => response)

  const result = await fetchSidebarSessionsBatchedOrSliced(new URLSearchParams(QUERY), {
    fanOut: async () => assert.fail('fan-out must not run'),
    fetchJsonForProfile: fetchJson,
    hasRemoteOverride: () => false,
    ownerTag: null
  })

  assert.deepEqual(result, batched())
})

test('owner tagging copies every slice and leaves owned rows alone', () => {
  const response = batched()
  const tagged = tagSidebarSlicesOwner(response, 'nous-cloud') as ReturnType<typeof batched>

  assert.notEqual(tagged, response)
  assert.notEqual(tagged.recents, response.recents)
  assert.equal(tagged.recents.sessions[1], response.recents.sessions[1], 'an owned row is passed through')
  assert.equal(response.recents.sessions[0].connection_id, undefined)
  assert.equal(response.cron.sessions[0].connection_id, '  ')
})

test('a failed batched read falls back to the three per-slice reads and returns their result', async () => {
  for (const failure of [
    Object.assign(new Error('No such API endpoint'), { statusCode: 404 }),
    new Error('timed out')
  ]) {
    const searchParams = new URLSearchParams(QUERY)

    const { calls, fetchJson } = recorder(({ path }) => {
      if (path.startsWith('/api/profiles/sessions/sidebar')) {
        throw failure
      }

      const source = new URL(path, 'http://x').searchParams.get('source')

      return { sessions: [{ id: source === 'cron' ? 'cron-1' : 'chat-1' }], total: 1, profile_totals: { default: 1 } }
    })

    const fanOut = sliceFanOut(searchParams, fetchJson, 'local')

    const result = (await fetchSidebarSessionsBatchedOrSliced(searchParams, {
      fanOut,
      fetchJsonForProfile: fetchJson,
      hasRemoteOverride: () => false,
      ownerTag: 'local'
    })) as any

    assert.equal(calls[0].path, `/api/profiles/sessions/sidebar?${QUERY}`)
    assert.deepEqual(
      calls.slice(1).map(call => [call.profile, new URL(call.path, 'http://x').pathname]),
      [
        [null, '/api/profiles/sessions'],
        [null, '/api/profiles/sessions'],
        [null, '/api/profiles/sessions']
      ]
    )
    assert.deepEqual(result.recents.sessions, [{ id: 'chat-1', connection_id: 'local' }])
    assert.deepEqual(result.cron.sessions, [{ id: 'cron-1', connection_id: 'local' }])
    assert.equal(result.recents.total, 1)
  }
})

test('every refresh retries the batched read after a failure (route-missing is not remembered)', async () => {
  let batchedReads = 0

  const { fetchJson } = recorder(({ path }) => {
    if (path.startsWith('/api/profiles/sessions/sidebar')) {
      batchedReads += 1
      throw Object.assign(new Error('No such API endpoint'), { statusCode: 404 })
    }

    return { sessions: [], total: 0, profile_totals: {} }
  })

  for (let round = 0; round < 2; round += 1) {
    const searchParams = new URLSearchParams(QUERY)
    await fetchSidebarSessionsBatchedOrSliced(searchParams, {
      fanOut: sliceFanOut(searchParams, fetchJson, null),
      fetchJsonForProfile: fetchJson,
      hasRemoteOverride: () => false,
      ownerTag: null
    })
  }

  assert.equal(batchedReads, 2)
})

test('the all-profiles scope and a remote-override profile keep the per-slice path', async () => {
  for (const [query, override] of [
    ['recents_profile=all&recents_limit=20', () => false],
    ['recents_limit=20', () => false],
    ['recents_profile=work&recents_limit=20', (profile: string) => profile === 'work']
  ] as const) {
    const searchParams = new URLSearchParams(query)
    const { calls, fetchJson } = recorder(() => ({ sessions: [], total: 0, profile_totals: {} }))
    const fanned = { fanned: true }
    let fanOuts = 0

    const result = await fetchSidebarSessionsBatchedOrSliced(searchParams, {
      fanOut: async () => {
        fanOuts += 1

        return fanned
      },
      fetchJsonForProfile: fetchJson,
      hasRemoteOverride: override,
      ownerTag: 'local'
    })

    assert.equal(result, fanned, query)
    assert.equal(fanOuts, 1, query)
    assert.deepEqual(calls, [], query)
  }
})

test('the batched scope reads recents_profile like the slice params do', () => {
  assert.equal(
    batchedPrimarySidebarProfile(new URLSearchParams('recents_profile=%20default%20'), () => false),
    'default'
  )
  assert.equal(
    batchedPrimarySidebarProfile(new URLSearchParams('recents_profile=%20'), () => false),
    null
  )
  assert.equal(
    batchedPrimarySidebarProfile(new URLSearchParams('recents_profile=work'), () => true),
    null
  )
})
