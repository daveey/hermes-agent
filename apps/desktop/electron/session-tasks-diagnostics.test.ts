import { createHash } from 'node:crypto'
import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { readSessionTasksLoadedIdentityForWindow as read } from './session-tasks-diagnostics'

const windows = vi.hoisted(() => new Map<number, unknown>())
vi.mock('electron', () => ({ BrowserWindow: { fromId: (id: number) => windows.get(id) } }))
const pin = (bytes: string) => ({ sha256: createHash('sha256').update(bytes).digest('hex'), byteLength: Buffer.byteLength(bytes) })
const manifest = JSON.stringify({ 'src/session-tasks-diagnostics.ts': { isEntry: true, file: 'assets/diag.js', imports: ['core'] }, core: { file: 'assets/core.js' } })
const inventory = () => ({ schemaVersion: 1 as const, manifest: pin(manifest), entryAsset: 'assets/diag.js', assets: [{path: 'assets/diag.js', ...pin('diag')}, {path: 'assets/core.js', ...pin('core')}] })
let root: string, contents: EventEmitter & { mainFrame: {routingId: number; processId: number}; getURL: () => string; getOSProcessId: () => number; isDestroyed: () => boolean; executeJavaScript: ReturnType<typeof vi.fn> }
let window: {webContents: typeof contents; isDestroyed: () => boolean}

function makeRoot() {
  const directory = fs.mkdtempSync(path.join(root, 'copy-'))
  fs.mkdirSync(path.join(directory, 'assets'))
  fs.writeFileSync(path.join(directory, 'index.html'), '<html></html>')
  fs.writeFileSync(path.join(directory, 'renderer-manifest.json'), manifest)
  fs.writeFileSync(path.join(directory, 'assets/diag.js'), 'diag')
  fs.writeFileSync(path.join(directory, 'assets/core.js'), 'core')

  return directory
}

let serving: string
beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'session-tasks-route-'))
  serving = makeRoot()
  contents = Object.assign(new EventEmitter(), {
    mainFrame: { routingId: 5, processId: 6 }, getURL: () => pathToFileURL(path.join(serving, 'index.html')).href,
    getOSProcessId: () => 77, isDestroyed: () => false, executeJavaScript: vi.fn(async () => 'honest primitive')
  })
  window = { webContents: contents, isDestroyed: () => false }; windows.set(1, window)
})
afterEach(() => { windows.clear(); fs.rmSync(root, {recursive: true, force: true}) })

it('executes the supported entry from the exact selected live serving copy', async () => {
  const other = makeRoot()
  expect(await read(1, inventory())).toBe('honest primitive')
  expect(contents.executeJavaScript.mock.calls[0][0]).toContain(pathToFileURL(path.join(serving, 'assets/diag.js')).href)
  expect(contents.executeJavaScript.mock.calls[0][0]).not.toContain(other)
  expect(contents.executeJavaScript.mock.calls[0][1]).toBe(false)
})
it('selects the new actual serving copy, never an identical-byte alternate directory', async () => {
  const first = serving; serving = makeRoot()
  await read(1, inventory())
  expect(contents.executeJavaScript.mock.calls[0][0]).toContain(serving)
  expect(contents.executeJavaScript.mock.calls[0][0]).not.toContain(first)
})
it.each(['assets/diag.js', 'assets/core.js', 'renderer-manifest.json'])('refuses changed %s before any renderer call', async file => {
  fs.appendFileSync(path.join(serving, file), 'changed')
  await expect(read(1, inventory())).rejects.toThrow('BUILD_BYTES_CHANGED')
  expect(contents.executeJavaScript).not.toHaveBeenCalled()
})
it('refuses an independently pinned inventory without the actual dependency', async () => {
  const expected = inventory(); expected.assets.pop()
  await expect(read(1, expected)).rejects.toThrow('UNPINNED_ASSET')
  expect(contents.executeJavaScript).not.toHaveBeenCalled()
})
it('refuses duplicate inventory members', async () => {
  const expected = inventory(); expected.assets.push(expected.assets[0])
  await expect(read(1, expected)).rejects.toThrow('DUPLICATE_ASSET')
})
it('refuses a wrong diagnostic entry even when supplied bytes are valid', async () => {
  const expected = inventory(); expected.entryAsset = 'assets/core.js'
  await expect(read(1, expected)).rejects.toThrow('ENTRY_MAPPING')
})
it('refuses wrong schema', async () => {
  const expected = inventory(); Object.assign(expected, {schemaVersion: 2})
  await expect(read(1, expected)).rejects.toThrow('INVENTORY_SCHEMA')
})
it('refuses non-file live renderer', async () => {
  contents.getURL = () => 'https://fixture.invalid/index.html'
  await expect(read(1, inventory())).rejects.toThrow('SERVING_INDEX')
})
it('refuses absent window', async () => { await expect(read(2, inventory())).rejects.toThrow('WINDOW_ABSENT') })
it('refuses object responses rather than serializing or awaiting their contents', async () => {
  contents.executeJavaScript.mockResolvedValue({status: 'MATCH'})
  await expect(read(1, inventory())).rejects.toThrow('NONPRIMITIVE_RESPONSE')
})
it('propagates renderer rejection without any subsequent call', async () => {
  contents.executeJavaScript.mockRejectedValue(new Error('owned rejection'))
  await expect(read(1, inventory())).rejects.toThrow('owned rejection')
  expect(contents.executeJavaScript).toHaveBeenCalledTimes(1)
})

const replacements = [
  ['same-URL main navigation', () => { contents.emit('did-start-navigation', {}, contents.getURL(), false, true) }],
  ['same-document main navigation', () => { contents.emit('did-navigate-in-page', {}, contents.getURL(), true) }],
  ['renderer gone', () => { contents.emit('render-process-gone', {}) }],
  ['destroy event', () => { contents.emit('destroyed') }],
  ['PID', () => { contents.getOSProcessId = () => 78 }],
  ['frame object', () => { contents.mainFrame = {...contents.mainFrame} }],
  ['frame process', () => { contents.mainFrame.processId++ }],
  ['routing id', () => { contents.mainFrame.routingId++ }],
  ['serving copy', () => { serving = makeRoot() }],
  ['window identity', () => { windows.set(1, { ...window }) }],
  ['window destroyed', () => { window.isDestroyed = () => true }],
  ['contents destroyed', () => { contents.isDestroyed = () => true }],
  ['static bytes', () => { fs.appendFileSync(path.join(serving, 'assets/core.js'), 'changed') }]
] as const

it.each(replacements)('rejects old in-flight response after %s with no next ordinary call', async (_name, replace) => {
  let finish!: (value: string) => void
  contents.executeJavaScript.mockImplementation(() => new Promise<string>(resolve => { finish = resolve }))
  const pending = read(1, inventory())
  replace(); finish('old honest MATCH')
  await expect(pending).rejects.toThrow()
  expect(contents.executeJavaScript).toHaveBeenCalledTimes(1)
})
it('ignores child-frame navigation without retiring the real main realm', async () => {
  contents.executeJavaScript.mockImplementation(async () => { contents.emit('did-start-navigation', {}, 'child', false, false);

 return 'current' })
  await expect(read(1, inventory())).resolves.toBe('current')
})
