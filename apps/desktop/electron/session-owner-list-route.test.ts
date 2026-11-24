import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { test } from 'vitest'

import { isMergeableSessionListRequest, parseSessionRequestPath } from './session-owner-list-route'

test('owner proof stays on the pinned backend rather than best-effort sidebar union', () => {
  assert.equal(parseSessionRequestPath('http://['), null)
  assert.equal(parseSessionRequestPath('/api/profiles/sessions')?.pathname, '/api/profiles/sessions')
  const params = new URLSearchParams('profile=all&archive_owner_lookup=1')
  assert.equal(isMergeableSessionListRequest('GET', '/api/profiles/sessions', params), false)
  assert.equal(isMergeableSessionListRequest('GET', '/api/profiles/sessions', new URLSearchParams('profile=all')), true)
  assert.equal(isMergeableSessionListRequest('GET', '/api/profiles/sessions', new URLSearchParams('profile=work')), true)
  assert.equal(isMergeableSessionListRequest('PATCH', '/api/profiles/sessions', params), false)
  // Main's normal direct route still authenticates the explicit connection.
  const main = readFileSync(new URL('./main.ts', import.meta.url), 'utf8')
  assert.match(main, /if \(isMergeableSessionListRequest\(method, pathname, searchParams\)\)/)
})
