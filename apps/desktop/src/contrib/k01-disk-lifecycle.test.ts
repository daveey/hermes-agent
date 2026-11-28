import { afterEach, expect, it, vi } from 'vitest'

import { $pluginRecords, setPluginEnabled } from './plugins-store'
import {
  discoverRuntimePlugins,
  refreshChangedDiskPlugins,
  unloadRuntimePlugin,
  watchRuntimePlugins
} from './runtime-loader'
import { routeBlobImportsToDataUrls } from './runtime-loader.test-helpers'

const root = '/fixture/desktop-plugins'
const file = `${root}/owned/plugin.js`
const state = globalThis as any
const version = (name: string, prefix = '') =>
  `${prefix}; export default {id:'disk-owned',register(ctx){globalThis.__diskRuns.push('${name}');ctx.onDispose(()=>globalThis.__diskStops.push('${name}'))}} // ${name}`

let restore = () => {}
afterEach(() => {
  unloadRuntimePlugin('disk-owned')
  restore()
  vi.restoreAllMocks()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  delete state.__diskRuns
  delete state.__diskStops
  delete state.__diskBarrier
  delete state.__diskEvaluated
  delete state.__diskImportEntered
  delete (window as any).hermesDesktop
})
it('actual disk discovery/watch/refresh preserves disabled and superseding owners across pending identity IO', async () => {
  restore = routeBlobImportsToDataUrls()
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  state.__diskRuns = []
  state.__diskStops = []
  let source = version('initial')

  let watchEvent: (event: { id: string }) => void = () => {}
  const readFileText = vi.fn(async () => ({ text: source }))
  const roots = vi.fn(async () => root)

  let present = true

  ;(window as any).hermesDesktop = {
    desktopPluginsRoot: roots,
    readDir: async (path: string) => ({
      entries: !present
        ? []
        : path === root
          ? [{ name: 'owned', path: `${root}/owned`, isDirectory: true }]
          : path === `${root}/owned`
            ? [{ name: 'plugin.js', path: file, isDirectory: false }]
            : []
    }),
    readFileText,
    watchPreviewFile: async () => ({ id: 'owned-watch' }),
    stopPreviewFileWatch: async () => true,
    watchDirectory: async () => ({ id: 'directory-watch' }),
    onPreviewFileChanged: (cb: typeof watchEvent) => {
      watchEvent = cb
    },
    onPowerResume: () => () => {}
  }
  await setPluginEnabled('disk-owned', true)
  watchRuntimePlugins()
  await vi.waitFor(() => expect(state.__diskRuns).toEqual(['initial']))
  const digest = crypto.subtle.digest.bind(crypto.subtle)
  let releases: (() => void)[] = []
  const spy = vi
    .spyOn(crypto.subtle, 'digest')
    .mockImplementation((...args) => new Promise(resolve => releases.push(() => resolve(digest(...args)))))
  source = version('disabled-reload')
  const disabledReload = refreshChangedDiskPlugins('disable-proof')
  await vi.waitFor(() => expect(releases).toHaveLength(2))
  await setPluginEnabled('disk-owned', false)
  releases.forEach(release => release())
  await disabledReload
  expect($pluginRecords.get()['disk-owned']).toMatchObject({ status: 'disabled' })
  await setPluginEnabled('disk-owned', true)
  expect(state.__diskRuns).toEqual(['initial', 'initial'])
  // Cancelled bytes were never committed as loadedSource: they remain retryable.
  spy.mockRestore()
  await refreshChangedDiskPlugins('retry-proof')
  expect(state.__diskRuns).toEqual(['initial', 'initial', 'disabled-reload'])

  releases = []
  vi.spyOn(crypto.subtle, 'digest').mockImplementation(
    (...args) => new Promise(resolve => releases.push(() => resolve(digest(...args))))
  )
  source = version('stale')
  const stale = refreshChangedDiskPlugins('supersession-proof')
  await vi.waitFor(() => expect(releases).toHaveLength(2))
  source = version('replacement')
  watchEvent({ id: 'owned-watch' })
  await vi.waitFor(() => expect(releases).toHaveLength(4))
  releases[2]()
  releases[3]()
  await vi.waitFor(() => expect(state.__diskRuns.at(-1)).toBe('replacement'))
  releases[0]()
  releases[1]()
  await stale
  expect($pluginRecords.get()['disk-owned'].status).toBe('loaded')
  expect(state.__diskRuns).not.toContain('stale')
  expect(state.__diskStops).not.toContain('replacement')
  expect(await refreshChangedDiskPlugins('unchanged-proof')).toEqual([])

  // A newer watcher read supersedes an older file read before evaluation.
  let releaseRead!: (value: { text: string }) => void
  const slowText = version('slow-read', 'globalThis.__diskEvaluated = true')
  readFileText.mockResolvedValueOnce({ text: slowText }).mockImplementationOnce(
    () =>
      new Promise(resolve => {
        releaseRead = resolve
      })
  )
  const slowRead = refreshChangedDiskPlugins('read-order-proof')
  await vi.waitFor(() => expect(releaseRead).toBeTypeOf('function'))
  source = version('read-winner')
  const start = releases.length
  watchEvent({ id: 'owned-watch' })
  await vi.waitFor(() => expect(releases).toHaveLength(start + 2))
  releases.slice(start).forEach(release => release())
  await vi.waitFor(() => expect(state.__diskRuns.at(-1)).toBe('read-winner'))
  let slowReadDone = false
  void slowRead.then(() => {
    slowReadDone = true
  })
  releaseRead({ text: slowText })
  await vi.waitFor(() => expect(slowReadDone || releases.length > start + 2).toBe(true))
  releases.slice(start + 2).forEach(release => release())
  await slowRead
  expect(state.__diskEvaluated).toBeUndefined()

  // A superseded import must not start identity IO, even when its top-level
  // evaluation completes after a newer module registered.
  let releaseImport!: () => void
  state.__diskBarrier = new Promise<void>(resolve => {
    releaseImport = resolve
  })
  source = version(
    'slow-import',
    'globalThis.__diskImportEntered = true; await globalThis.__diskBarrier; globalThis.__diskEvaluated = true'
  )
  const importStart = releases.length
  const slowImport = refreshChangedDiskPlugins('import-order-proof')
  await vi.waitFor(() => expect(readFileText).toHaveBeenLastCalledWith(file))
  // Wait for the actual module to enter its top-level await, not a sleep.
  // A sentinel before await is retained in the source below.
  await vi.waitFor(() => expect(state.__diskImportEntered).toBe(true))
  source = version('import-winner')
  watchEvent({ id: 'owned-watch' })
  await vi.waitFor(() => expect(releases).toHaveLength(importStart + 2))
  releases.slice(importStart).forEach(release => release())
  await vi.waitFor(() => expect(state.__diskRuns.at(-1)).toBe('import-winner'))
  releaseImport()
  await vi.waitFor(() => expect(state.__diskEvaluated).toBe(true))
  releases.slice(importStart).forEach(release => release())
  await slowImport
  expect(releases).toHaveLength(importStart + 2)
  expect(state.__diskRuns).not.toContain('slow-import')

  // Supersession starts at the next disk request, not its eventual import.
  const hashStart = releases.length
  source = version('stale-hash')
  const staleHash = refreshChangedDiskPlugins('hash-order-proof')
  await vi.waitFor(() => expect(releases).toHaveLength(hashStart + 2))
  state.__diskImportEntered = false
  state.__diskBarrier = new Promise<void>(resolve => {
    releaseImport = resolve
  })
  source = version('hash-winner', 'globalThis.__diskImportEntered = true; await globalThis.__diskBarrier')
  watchEvent({ id: 'owned-watch' })
  await vi.waitFor(() => expect(state.__diskImportEntered).toBe(true))
  releases.slice(hashStart).forEach(release => release())
  await staleHash
  expect(state.__diskRuns).not.toContain('stale-hash')
  releaseImport()
  await vi.waitFor(() => expect(releases).toHaveLength(hashStart + 4))
  releases.slice(hashStart + 2).forEach(release => release())
  await vi.waitFor(() => expect(state.__diskRuns.at(-1)).toBe('hash-winner'))

  // Late errors from stale reads/imports do not overwrite current inventory.
  let rejectRead!: (error: Error) => void
  readFileText.mockResolvedValueOnce({ text: version('failed-read') }).mockImplementationOnce(
    () =>
      new Promise((_, reject) => {
        rejectRead = reject
      })
  )
  const failedRead = refreshChangedDiskPlugins('read-rejection-proof')
  await vi.waitFor(() => expect(rejectRead).toBeTypeOf('function'))
  source = version('read-error-winner')
  const errorStart = releases.length
  watchEvent({ id: 'owned-watch' })
  await vi.waitFor(() => expect(releases).toHaveLength(errorStart + 2))
  releases.slice(errorStart).forEach(release => release())
  await vi.waitFor(() => expect(state.__diskRuns.at(-1)).toBe('read-error-winner'))
  const rootReads = roots.mock.calls.length
  rejectRead(new Error('stale read failed'))
  await failedRead
  expect(roots).toHaveBeenCalledTimes(rootReads)
  expect(state.__diskStops).not.toContain('read-error-winner')

  state.__diskImportEntered = false
  state.__diskBarrier = new Promise<void>(resolve => {
    releaseImport = resolve
  })
  source = version(
    'failed-import',
    'globalThis.__diskImportEntered = true; await globalThis.__diskBarrier; throw new Error("stale import failed")'
  )
  const failedImport = refreshChangedDiskPlugins('import-rejection-proof')
  await vi.waitFor(() => expect(state.__diskImportEntered).toBe(true))
  source = version('import-error-winner')
  const rejectionStart = releases.length
  watchEvent({ id: 'owned-watch' })
  await vi.waitFor(() => expect(releases).toHaveLength(rejectionStart + 2))
  releases.slice(rejectionStart).forEach(release => release())
  await vi.waitFor(() => expect(state.__diskRuns.at(-1)).toBe('import-error-winner'))
  releaseImport()
  await failedImport
  expect($pluginRecords.get().owned).toBeUndefined()
  expect($pluginRecords.get()['disk-owned'].status).toBe('loaded')

  state.__diskImportEntered = false
  state.__diskEvaluated = false
  state.__diskBarrier = new Promise<void>(resolve => {
    releaseImport = resolve
  })
  source = version(
    'retired-import',
    'globalThis.__diskImportEntered = true; await globalThis.__diskBarrier; globalThis.__diskEvaluated = true'
  )
  const retired = refreshChangedDiskPlugins('entry-retirement-proof')
  await vi.waitFor(() => expect(state.__diskImportEntered).toBe(true))
  present = false
  await discoverRuntimePlugins()
  const retiredStart = releases.length
  releaseImport()
  await vi.waitFor(() => expect(state.__diskEvaluated).toBe(true))
  releases.slice(retiredStart).forEach(release => release())
  await retired
  expect(releases).toHaveLength(retiredStart)
  expect(state.__diskRuns).not.toContain('retired-import')
  expect($pluginRecords.get()['disk-owned']).toBeUndefined()
}, 15_000)
