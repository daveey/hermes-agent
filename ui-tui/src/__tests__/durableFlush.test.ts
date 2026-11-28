import type * as Fs from 'node:fs'
import { existsSync, fstatSync, mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs'
import type { FileHandle } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, expect, it, vi } from 'vitest'

import type * as DurableFlush from '../lib/durableFlush.js'
import { flushDirectorySync, flushFileHandle, flushFileSync } from '../lib/durableFlush.js'
import { stageImagePath } from '../lib/imageAttachments.js'
import { removePendingInput, savePendingInput } from '../lib/pendingInputs.js'

// vitest.setup.ts replaces durableFlush with no-op spies for every suite. These cases pin
// what that replacement must not hide: each flush happens at the point that makes the
// journal crash-safe, and the real module issues the fsyncs (recorded, not sent to the disk;
// coldPendingRenderer's child process still runs the real syscalls).
const fsyncs = vi.hoisted(() => [] as number[])
vi.mock('node:fs', async importOriginal => {
  const actual = await importOriginal<typeof Fs>()

  return {
    ...actual,
    fsyncSync: vi.fn((fd: number) => {
      fsyncs.push(fd)
    })
  }
})

const homes: string[] = []
afterEach(() => {
  vi.mocked(flushFileSync).mockReset()
  vi.mocked(flushDirectorySync).mockReset()
  vi.mocked(flushFileHandle).mockReset()
  fsyncs.length = 0

  for (const home of homes.splice(0)) {
    rmSync(home, { recursive: true, force: true })
  }
})

const freshHome = () => {
  const home = mkdtempSync(join(tmpdir(), 'ink-flush-'))
  homes.push(home)

  return home
}

const item = (home: string) =>
  ({
    submissionId: '00000000-0000-4000-8000-000000000001',
    text: 'hello',
    display: 'hello',
    createdAt: 1,
    destination: { sid: 'S1', storedSid: 'stored', profile: 'default', profileHome: home }
  }) as any

it('flushes the complete temporary record before the rename and the directory after it', () => {
  const home = freshHome()
  const dir = join(home, 'tui-pending-inputs')
  const final = join(dir, '00000000-0000-4000-8000-000000000001.json')
  const order: string[] = []

  vi.mocked(flushFileSync).mockImplementation(fd => {
    order.push('file')
    const [temporary] = readdirSync(dir).filter(name => name.endsWith('.tmp'))
    expect(existsSync(final)).toBe(false)
    expect(fstatSync(fd).size).toBe(readFileSync(join(dir, temporary!)).length)
    expect(JSON.parse(readFileSync(join(dir, temporary!), 'utf8')).text).toBe('hello')
  })
  vi.mocked(flushDirectorySync).mockImplementation(flushed => {
    order.push('dir')
    expect(flushed).toBe(dir)
    expect(readdirSync(dir)).toEqual(['00000000-0000-4000-8000-000000000001.json'])
  })

  savePendingInput(item(home))
  expect(order).toEqual(['file', 'dir'])
  expect(JSON.parse(readFileSync(final, 'utf8')).text).toBe('hello')
})

it('flushes the directory after removing a record', () => {
  const home = freshHome()
  const dir = join(home, 'tui-pending-inputs')
  savePendingInput(item(home))
  vi.mocked(flushDirectorySync).mockReset()
  vi.mocked(flushDirectorySync).mockImplementation(flushed => {
    expect(flushed).toBe(dir)
    expect(readdirSync(dir)).toEqual([])
  })

  removePendingInput(item(home))
  expect(flushDirectorySync).toHaveBeenCalledTimes(1)
})

it('flushes staged image bytes before handing out their path', async () => {
  const home = freshHome()
  vi.stubEnv('HERMES_TUI_GATEWAY_URL', '')
  const source = join(home, 'shot.png')
  const png = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nL8AAAAASUVORK5CYII=',
    'base64'
  )
  const { writeFileSync } = await vi.importActual<typeof Fs>('node:fs')
  writeFileSync(source, png)

  vi.mocked(flushFileHandle).mockImplementation(async (handle: FileHandle) => {
    expect((await handle.stat()).size).toBe(png.length)
  })

  try {
    const staged = await stageImagePath(source, {} as any, { sid: 'S1', profile: 'default', profileHome: home } as any)
    expect(flushFileHandle).toHaveBeenCalledTimes(1)
    expect(readFileSync(staged.path)).toEqual(png)
  } finally {
    vi.unstubAllEnvs()
  }
})

it('the real module fsyncs the file descriptor and the directory', async () => {
  const real = await vi.importActual<typeof DurableFlush>('../lib/durableFlush.js')
  const home = freshHome()
  const { openSync, closeSync } = await vi.importActual<typeof Fs>('node:fs')
  const fd = openSync(join(home, 'record'), 'w')

  try {
    real.flushFileSync(fd)
  } finally {
    closeSync(fd)
  }
  expect(fsyncs).toEqual([fd])

  real.flushDirectorySync(home)
  expect(fsyncs).toHaveLength(process.platform === 'win32' ? 1 : 2)

  const sync = vi.fn(async () => {})
  await real.flushFileHandle({ sync } as unknown as FileHandle)
  expect(sync).toHaveBeenCalledTimes(1)
})
