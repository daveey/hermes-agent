import assert from 'node:assert/strict'

import { test } from 'vitest'

import { fetchSidebarPrimaryListBatch } from './sidebar-primary-list-batch'

const response = (n: number) => ({
  list_slices_version: 1,
  ...Object.fromEntries(
    ['recents', 'cron', 'messaging'].map(key => [
      key,
      {
        sessions: [{ id: `${key}-${n}`, profile: 'work' }],
        total: 731,
        profile_totals: { work: 731 }
      }
    ])
  )
})

test('whole all-profile refresh reads primary once, retains exact bases and rereads without notification', async () => {
  let reads = 0
  const merged: any[] = []
  const query = new URLSearchParams(
    'recents_profile=all&recents_limit=50&cron_limit=50&messaging_limit=100&recents_exclude=cron'
  )

  const deps = {
    fetch: async (path: string) => {
      assert.equal(new URL(path, 'http://x').searchParams.get('list_slices'), 'true')

      return response(++reads)
    },
    merge: async (sp: URLSearchParams, base: any) => {
      merged.push({ sp, base })
      assert.equal(base.total, 731)
      assert.equal(base.profile_totals.work, 731)
      assert.equal(base.sessions[0].connection_id, 'hub')

      return base
    },
    ownerTag: 'hub',
    fallback: async () => assert.fail('unexpected legacy read')
  }

  const first: any = await fetchSidebarPrimaryListBatch(query, deps)
  const second: any = await fetchSidebarPrimaryListBatch(query, deps)
  assert.equal(reads, 2)
  assert.equal(merged.length, 6)
  assert.equal(first.recents.sessions[0].id, 'recents-1')
  assert.equal(second.recents.sessions[0].id, 'recents-2')
  assert.equal(merged[0].sp.get('exclude_sources'), 'cron')
  assert.equal(merged[1].sp.get('source'), 'cron')
})

test('old, malformed and failed batches fall back, and the next refresh retries', async () => {
  for (const answer of [null, {}, { ...response(1), cron: {} }, new Error('404')]) {
    let fallbacks = 0
    let reads = 0

    const deps = {
      fetch: async () => {
        reads++

        if (answer instanceof Error) {
          throw answer
        }

        return answer
      },
      merge: async () => assert.fail('incomplete base merged'),
      ownerTag: null,
      fallback: async () => {
        fallbacks++

        return 'legacy'
      }
    }

    for (let i = 0; i < 2; i++) {
      assert.equal(await fetchSidebarPrimaryListBatch(new URLSearchParams(), deps), 'legacy')
    }
    assert.equal(reads, 2)
    assert.equal(fallbacks, 2)
  }
})

test('concrete profiles and large windows retain paginated legacy reads', async () => {
  for (const query of ['recents_profile=work', 'recents_limit=501']) {
    assert.equal(
      await fetchSidebarPrimaryListBatch(new URLSearchParams(query), {
        fetch: async () => assert.fail('batch must not truncate'),
        merge: async () => assert.fail('merge'),
        fallback: async () => 'legacy',
        ownerTag: null
      }),
      'legacy'
    )
  }
})
