// [daveey] Level-triggered refresh of disk plugins: a plugin.js whose bytes changed
// with NO fs watch event (FSEvents dropped around sleep/wake, a dead watch, a git
// pull right after a wake) is reloaded on the next power resume and on the
// once-a-minute interval. Own file: watchRuntimePlugins() is once-per-module.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesReadDirResult } from '@/global'

import {
  discoverRuntimePlugins,
  DISK_REFRESH_MS,
  refreshChangedDiskPlugins,
  unloadRuntimePlugin,
  watchRuntimePlugins
} from './runtime-loader'

const root = '/local/.hermes/desktop-plugins'
const file = `${root}/linked/plugin.js`

const desktopPluginsRoot = vi.fn<() => Promise<string>>()
const readDir = vi.fn<(path: string) => Promise<HermesReadDirResult>>()
const readFileText = vi.fn<(path: string) => Promise<{ text: string; truncated?: boolean }>>()
const watchDirectory = vi.fn<(path: string) => Promise<{ id: string }>>()
const watchPreviewFile = vi.fn<(path: string) => Promise<{ id: string }>>()
const stopPreviewFileWatch = vi.fn<(id: string) => Promise<boolean>>()
const onPreviewFileChanged = vi.fn()
const logLine = vi.fn<(line: string) => void>()
let resumeCallbacks: (() => void)[] = []

const onPowerResume = vi.fn((cb: () => void) => {
  resumeCallbacks.push(cb)

  return () => undefined
})

const counters = globalThis as unknown as Record<string, number | undefined>
const version = (v: string) => `export default { id: "linked", register() { globalThis.__linked_${v} = (globalThis.__linked_${v} ?? 0) + 1 } }`
let source = version('v1')
let watchN = 0
let restoreBlob: () => void = () => undefined

beforeEach(() => {
  source = version('v1')
  watchN = 0
  resumeCallbacks = []

  for (const key of Object.keys(counters).filter(k => k.startsWith('__linked_'))) {
    delete counters[key]
  }

  desktopPluginsRoot.mockResolvedValue(root)
  readDir.mockImplementation(async dir => {
    if (dir === root) {
      return { entries: [{ isDirectory: true, name: 'linked', path: `${root}/linked` }] }
    }

    if (dir === `${root}/linked`) {
      return { entries: [{ isDirectory: false, name: 'plugin.js', path: file }] }
    }

    return { entries: [] }
  })
  readFileText.mockImplementation(async () => ({ text: source }))
  watchDirectory.mockResolvedValue({ id: 'dir-watch' })
  watchPreviewFile.mockImplementation(async () => ({ id: `w-linked-${++watchN}` }))
  stopPreviewFileWatch.mockResolvedValue(true)
  stopPreviewFileWatch.mockClear()
  logLine.mockClear()
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
    desktopPluginsRoot,
    logLine,
    onPowerResume,
    onPreviewFileChanged,
    readDir,
    readFileText,
    stopPreviewFileWatch,
    watchDirectory,
    watchPreviewFile
  }

  const createObjectURL = vi
    .spyOn(URL, 'createObjectURL')
    .mockImplementation(
      blob =>
        `data:text/javascript;base64,${Buffer.from((blob as unknown as { parts: string[] }).parts.join('')).toString('base64')}`
    )

  const revokeObjectURL = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
  const RealBlob = globalThis.Blob
  vi.stubGlobal(
    'Blob',
    class {
      parts: string[]
      constructor(parts: string[]) {
        this.parts = parts
      }
    }
  )

  restoreBlob = () => {
    createObjectURL.mockRestore()
    revokeObjectURL.mockRestore()
    vi.stubGlobal('Blob', RealBlob)
  }
})

afterEach(() => {
  restoreBlob()
  vi.useRealTimers()
})

describe('disk plugins: level-triggered refresh (missed fs events)', () => {
  it('entry paths: power resume and the interval each reload a plugin.js rewritten with no watch event', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })

    watchRuntimePlugins()
    await vi.waitFor(() => expect(counters.__linked_v1).toBe(1))
    // The first scan has also bound the file watch (scan finished).
    await vi.waitFor(() => expect(watchPreviewFile).toHaveBeenCalledTimes(1))
    await Promise.resolve()
    expect(onPowerResume).toHaveBeenCalledTimes(1)

    // A git fast-forward rewrites the file; the fs watch never fires.
    source = version('v2')
    resumeCallbacks.forEach(cb => cb())
    await vi.waitFor(() => expect(counters.__linked_v2).toBe(1))
    // The orphaned watch is released and the current file is watched again.
    expect(stopPreviewFileWatch).toHaveBeenCalledWith('w-linked-1')
    expect(watchPreviewFile).toHaveBeenLastCalledWith(file)
    expect(logLine).toHaveBeenCalledWith(expect.stringContaining('linked: plugin.js changed with no watch event; reloaded (resume)'))

    // Later change, still no event, no wake: the minute interval picks it up.
    source = version('v3')
    vi.advanceTimersByTime(DISK_REFRESH_MS)
    await vi.waitFor(() => expect(counters.__linked_v3).toBe(1))
    expect(logLine).toHaveBeenCalledWith(expect.stringContaining('reloaded (interval)'))
    expect(counters.__linked_v1).toBe(1)
    expect(counters.__linked_v2).toBe(1)

    unloadRuntimePlugin('linked')
  })

  it('unchanged bytes never reload, and a vanished file hands off to the scan instead of reloading', async () => {
    // The module's disk map still holds `linked` from the first case (v3 there);
    // re-sync it to this case's source through a refresh first.
    source = version('v4')
    await refreshChangedDiskPlugins('test')
    expect(counters.__linked_v4).toBe(1)

    const reads = readFileText.mock.calls.length
    expect(await refreshChangedDiskPlugins('test')).toEqual([])
    expect(await refreshChangedDiskPlugins('test')).toEqual([])
    expect(counters.__linked_v4).toBe(1)
    expect(readFileText.mock.calls.length).toBe(reads + 2)

    // File gone (folder removed between scans): no reload, the scan retires it.
    readFileText.mockImplementation(async () => {
      throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' })
    })
    readDir.mockImplementation(async dir => (dir === root ? { entries: [] } : { entries: [] }))
    expect(await refreshChangedDiskPlugins('test')).toEqual([])
    await vi.waitFor(() => expect(stopPreviewFileWatch).toHaveBeenCalled())
  })

  it('a refresh tick while a scan is in flight is skipped, never racing the scan', async () => {
    // Previous case retired `linked`; load it again through a manual rescan.
    source = version('v5')
    await discoverRuntimePlugins()
    expect(counters.__linked_v5).toBe(1)

    // Hold the next scan at its first await (the plugins root).
    let releaseRoot: (root: string) => void = () => undefined
    const rootReads = desktopPluginsRoot.mock.calls.length
    desktopPluginsRoot.mockImplementationOnce(() => new Promise<string>(resolve => (releaseRoot = resolve)))
    const scan = discoverRuntimePlugins()
    await vi.waitFor(() => expect(desktopPluginsRoot).toHaveBeenCalledTimes(rootReads + 1))

    source = version('v6')
    const reads = readFileText.mock.calls.length
    expect(await refreshChangedDiskPlugins('test')).toEqual([])
    expect(readFileText.mock.calls.length).toBe(reads)
    expect(counters.__linked_v6).toBeUndefined()

    // The scan itself (a reload) then picks up the new bytes, once.
    releaseRoot(root)
    await scan
    expect(counters.__linked_v6).toBe(1)

    unloadRuntimePlugin('linked')
  })
})
