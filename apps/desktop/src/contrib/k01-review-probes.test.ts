import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { host } from '@/sdk'
import type * as Assets from '@/sdk/loaded-assets'

import { readK01Companion } from './k01-companion.test-helpers'
import { $pluginRecords, dropPlugin, setPluginEnabled } from './plugins-store'
import { loadRuntimePlugin, unloadRuntimePlugin } from './runtime-loader'
import { routeBlobImportsToDataUrls } from './runtime-loader.test-helpers'

vi.mock('@/sdk/loaded-assets', async original => {
  const actual = await original<typeof Assets>()

  return {
    ...actual,
    readServedAssets: async () => [
      { url: 'https://fixture.invalid/assets/sdk.js', ...(await actual.identifyBytes('rework fixture')) }
    ]
  }
})
const companion = readK01Companion()
const ids = ['review-race', 'review-plain', 'session-tasks']
const ordinary = (version = '') =>
  `globalThis.__raceEvaluations.push('${version}'); export default {id:'review-race',register(){globalThis.__raceRegistrations.push('${version}');}} // ${version}`
let restore: () => void
beforeEach(() => {
  restore = routeBlobImportsToDataUrls()

  for (const id of ids) {
    dropPlugin(id)
    void setPluginEnabled(id, true)
  }
  ;(globalThis as any).__raceRegistrations = []
  ;(globalThis as any).__raceEvaluations = []
})
afterEach(() => {
  for (const id of ids) {
    unloadRuntimePlugin(id)
  }
  restore()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  delete (window as any).__sessionTasks
  delete (globalThis as any).__raceRegistrations
  delete (globalThis as any).__raceEvaluations
  delete (globalThis as any).__ownedTimers
})

function holdDigests() {
  const digest = crypto.subtle.digest.bind(crypto.subtle)
  const releases: (() => void)[] = []
  vi.spyOn(crypto.subtle, 'digest').mockImplementation(
    (...args) =>
      new Promise(resolve => {
        releases.push(() => resolve(digest(...args)))
      })
  )

  return releases
}

it('first file reserves duplicate ownership throughout asynchronous identity work', async () => {
  const releases = holdDigests()
  const first = loadRuntimePlugin(ordinary('first'), 'first', { file: '/first/plugin.js' })
  await vi.waitFor(() => expect(releases).toHaveLength(2))
  const second = loadRuntimePlugin(ordinary('second'), 'second', { file: '/second/plugin.js' })
  await vi.waitFor(() => expect((globalThis as any).__raceEvaluations).toEqual(['first', 'second']))
  await Promise.resolve()

  for (const release of releases) {
    release()
  }
  const results = await Promise.all([first, second])
  expect(results).toEqual(['review-race', null])
  expect($pluginRecords.get()['review-race'].file).toBe('/first/plugin.js')
  expect((globalThis as any).__raceRegistrations).toEqual(['first'])
})
it('unload cancels pending identity work before it can publish or register', async () => {
  const releases = holdDigests()
  const first = loadRuntimePlugin(ordinary('cancelled'), 'first', { file: '/first/plugin.js' })
  await vi.waitFor(() => expect(releases).toHaveLength(2))
  unloadRuntimePlugin('review-race')

  for (const release of releases) {
    release()
  }
  expect(await first).toBeNull()
  expect($pluginRecords.get()['review-race']).toBeUndefined()
  expect((globalThis as any).__raceRegistrations).toEqual([])
})
it('superseded same-file completion cannot release the replacement reservation', async () => {
  const releases = holdDigests()
  const first = loadRuntimePlugin(ordinary('old'), 'first', { file: '/first/plugin.js' })
  await vi.waitFor(() => expect(releases).toHaveLength(2))
  const replacement = loadRuntimePlugin(ordinary('new'), 'first', { file: '/first/plugin.js' })
  await vi.waitFor(() => expect(releases).toHaveLength(4))
  releases[0]()
  releases[1]()
  expect(await first).toBeNull()
  const foreign = loadRuntimePlugin(ordinary('foreign'), 'foreign', { file: '/foreign/plugin.js' })
  await vi.waitFor(() => expect((globalThis as any).__raceEvaluations).toEqual(['old', 'new', 'foreign']))
  await Promise.resolve()

  for (const release of releases.slice(2)) {
    release()
  }
  expect(await foreign).toBeNull()
  expect(await replacement).toBe('review-race')
  expect((globalThis as any).__raceRegistrations).toEqual(['new'])
})
it('disable during a pending reload prevents old completion from registering', async () => {
  await loadRuntimePlugin(ordinary('initial'), 'first', { file: '/first/plugin.js' })
  const releases = holdDigests()
  const pending = loadRuntimePlugin(ordinary('reload'), 'first', { file: '/first/plugin.js' })
  await vi.waitFor(() => expect(releases).toHaveLength(2))
  await setPluginEnabled('review-race', false)

  for (const release of releases) {
    release()
  }
  expect(await pending).toBeNull()
  expect($pluginRecords.get()['review-race'].status).toBe('disabled')
  expect((globalThis as any).__raceRegistrations).toEqual(['initial'])
  await setPluginEnabled('review-race', true)
  expect((globalThis as any).__raceRegistrations).toEqual(['initial', 'initial'])
})
it.each(['absent', 'rejected'])('ordinary uninstrumented loading survives %s cryptography', async failure => {
  if (failure === 'absent') {
    vi.stubGlobal('crypto', { randomUUID: () => 'ordinary-fixture-uuid' })
  } else {
    vi.spyOn(crypto.subtle, 'digest').mockRejectedValue(new Error('fixture crypto refused'))
  }

  expect(
    await loadRuntimePlugin("export default {id:'review-plain',register(){}}", 'plain', { file: '/plain/plugin.js' })
  ).toBe('review-plain')
  expect($pluginRecords.get()['review-plain'].status).toBe('loaded')
})
it.each(['absent', 'rejected'])(
  'whole companion with %s cryptography stays functional but NOT_READY',
  async failure => {
    if (failure === 'absent') {
      vi.stubGlobal('crypto', { randomUUID: () => 'companion-fixture-uuid' })
    } else {
      vi.spyOn(crypto.subtle, 'digest').mockRejectedValue(new Error('fixture crypto refused'))
    }

    expect(await loadRuntimePlugin(companion, 'session-tasks', { file: '/session-tasks/plugin.js' })).toBe(
      'session-tasks'
    )
    expect($pluginRecords.get()['session-tasks'].status).toBe('loaded')
    expect(host.diagnostics.readSessionTasksLoadedIdentity()).toMatchObject({
      status: 'NOT_READY',
      reason: 'DIAGNOSTIC_IDENTITY_UNAVAILABLE',
      original: { sha256: '' },
      rewritten: { sha256: '' }
    })
    expect(Object.getOwnPropertyDescriptor(window, '__sessionTasks')?.value.version).toBe('wait-visible-v1')
  }
)
it('whole companion rollback releases surface, style, listeners and both timer kinds on duplicate-bind rejection', async () => {
  vi.useFakeTimers()
  const visibilityAdd = vi.spyOn(document, 'addEventListener')
  const visibilityRemove = vi.spyOn(document, 'removeEventListener')
  const stops: ReturnType<typeof vi.fn>[] = []

  for (const state of [
    host.state.focusedSessionId,
    host.state.focusedStoredSessionId,
    host.state.focusedSessionOwner,
    host.state.gateway
  ]) {
    const listen = state.listen.bind(state)
    vi.spyOn(state, 'listen').mockImplementation((listener: any) => {
      const stop = vi.fn(listen(listener))

      if (['observeFocusAuthority', 'onGateway'].includes(listener.name)) {
        stops.push(stop)
      }

      return stop
    })
  }

  const clearIntervalSpy = vi.spyOn(globalThis, 'clearInterval')
  const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout')

  const changed = companion.replace(
    'ctx.bindSessionTasksDiagnostic?.(surface)',
    'surface.subscribe(() => {}); onGatewayState(); globalThis.__ownedTimers = [timer, rpcEventTimer]; ctx.bindSessionTasksDiagnostic(surface); ctx.bindSessionTasksDiagnostic(surface)'
  )

  expect(changed).not.toBe(companion)
  await loadRuntimePlugin(changed, 'session-tasks', { file: '/session-tasks/plugin.js' })
  expect($pluginRecords.get()['session-tasks'].status).toBe('error')
  expect(Object.getOwnPropertyDescriptor(window, '__sessionTasks')).toBeUndefined()
  expect(document.getElementById('session-tasks-style') === null).toBe(true)
  const visibility = visibilityAdd.mock.calls.find(([type]) => type === 'visibilitychange')!
  expect(visibility).toBeDefined()
  expect(visibilityRemove).toHaveBeenCalledWith('visibilitychange', visibility[1])
  expect(stops).toHaveLength(4)

  for (const stop of stops) {
    expect(stop).toHaveBeenCalledTimes(1)
  }
  const [poll, rpc] = (globalThis as any).__ownedTimers
  expect(poll).toBeTruthy()
  expect(rpc).toBeTruthy()
  expect(clearIntervalSpy).toHaveBeenCalledWith(poll)
  expect(clearTimeoutSpy).toHaveBeenCalledWith(rpc)
  delete (globalThis as any).__ownedTimers
  expect(host.diagnostics.readSessionTasksLoadedIdentity().status).toBe('NOT_READY')
})
