import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { BrowserWindow, type WebContents } from 'electron'

interface BytePin { path: string; sha256: string; byteLength: number }
export interface SessionTasksDiagnosticInventory {
  schemaVersion: 1
  manifest: { sha256: string; byteLength: number }
  entryAsset: string
  assets: BytePin[]
}
interface Lifetime { epoch: number }
const lifetimes = new WeakMap<WebContents, Lifetime>()

function notReady(reason: string): never { throw new Error(`SESSION_TASKS_NOT_READY:${reason}`) }

function lifetime(contents: WebContents): Lifetime {
  const prior = lifetimes.get(contents)

  if (prior) { return prior }
  const state = { epoch: 0 }

  const invalidate = () => { state.epoch++ }
  // Subscribe before dispatch: an identical-URL replacement still retires pending reads.
  contents.on('did-start-navigation', (_event, _url, _inPlace, mainFrame) => { if (mainFrame) { invalidate() } })
  contents.on('did-navigate-in-page', (_event, _url, mainFrame) => { if (mainFrame) { invalidate() } })
  contents.on('render-process-gone', invalidate)
  contents.on('destroyed', invalidate)
  lifetimes.set(contents, state)

  return state
}

function assetPath(root: string, relative: string): string {
  if (!/^assets\/[A-Za-z0-9_.-]+\.js$/.test(relative)) { notReady('ASSET_PATH') }
  const absolute = path.join(root, relative)

  if (fs.realpathSync(absolute) !== absolute) { notReady('ASSET_SYMLINK') }

  return absolute
}

function verifyBytes(bytes: Buffer, expected: { sha256: string; byteLength: number }) {
  if (!/^[a-f0-9]{64}$/.test(expected.sha256) || bytes.length !== expected.byteLength || createHash('sha256').update(bytes).digest('hex') !== expected.sha256) { notReady('BUILD_BYTES_CHANGED') }
}

function resolveEntry(pageUrl: string, inventory: SessionTasksDiagnosticInventory) {
  if (inventory?.schemaVersion !== 1 || !Array.isArray(inventory.assets)) { notReady('INVENTORY_SCHEMA') }
  const page = new URL(pageUrl)

  if (page.protocol !== 'file:' || page.search || path.basename(fileURLToPath(page)) !== 'index.html') { notReady('SERVING_INDEX') }
  const root = path.dirname(fileURLToPath(page))

  if (fs.realpathSync(root) !== root) { notReady('SERVING_SYMLINK') }
  const bytes = fs.readFileSync(path.join(root, 'renderer-manifest.json'))
  verifyBytes(bytes, inventory.manifest)
  const manifest = JSON.parse(bytes.toString('utf8')) as Record<string, {file: string; imports?: string[]; isEntry?: boolean}>
  const entry = manifest['src/session-tasks-diagnostics.ts']

  if (!entry?.isEntry || entry.file !== inventory.entryAsset) { notReady('ENTRY_MAPPING') }
  const pins = new Map(inventory.assets.map(pin => [pin.path, pin]))

  if (pins.size !== inventory.assets.length) { notReady('DUPLICATE_ASSET') }
  const visited = new Set<string>()

  const visit = (key: string) => {
    if (visited.has(key)) { return }
    visited.add(key)
    const item = manifest[key]

    if (!item || !Array.isArray(item.imports ?? [])) { notReady('MANIFEST_CLOSURE') }
    const pin = pins.get(item.file)

    if (!pin) { notReady('UNPINNED_ASSET') }
    verifyBytes(fs.readFileSync(assetPath(root, item.file)), pin)

    for (const dependency of item.imports ?? []) { visit(dependency) }
  }

  visit('src/session-tasks-diagnostics.ts')

  return { root, url: pathToFileURL(assetPath(root, entry.file)).href }
}

function stamp(contents: WebContents, state: Lifetime) {
  if (contents.isDestroyed()) { notReady('WINDOW_DESTROYED') }
  const frame = contents.mainFrame

  return { epoch: state.epoch, frame, process: contents.getOSProcessId(), routing: frame.routingId, frameProcess: frame.processId, url: contents.getURL() }
}

/** Main-inspector-only reader. No renderer/preload registration, setter or caller result is accepted.
 * The caller must supply an independently reviewed build inventory; it is not process attestation.
 */
async function dispatchWindow(windowId: number, inventory: SessionTasksDiagnosticInventory, body: string): Promise<unknown> {
  const window = BrowserWindow.fromId(windowId)

  if (!window || window.isDestroyed()) { notReady('WINDOW_ABSENT') }
  const contents = window.webContents
  const state = lifetime(contents)
  const before = stamp(contents, state)
  const entry = resolveEntry(before.url, inventory)
  const script = `(async()=>{const module=await import(${JSON.stringify(entry.url)});${body}})()`
  const result: unknown = await contents.executeJavaScript(script, false)

  if (BrowserWindow.fromId(windowId) !== window || window.isDestroyed()) { notReady('WINDOW_REPLACED') }
  const after = stamp(contents, state)

  if (after.epoch !== before.epoch || after.frame !== before.frame || after.process !== before.process || after.routing !== before.routing || after.frameProcess !== before.frameProcess || after.url !== before.url) { notReady('LIFETIME_CHANGED') }
  const current = resolveEntry(after.url, inventory)

  if (current.root !== entry.root || current.url !== entry.url) { notReady('SERVING_COPY_CHANGED') }

  return result
}

export async function readSessionTasksLoadedIdentityForWindow(windowId: number, inventory: SessionTasksDiagnosticInventory): Promise<string> {
  const result = await dispatchWindow(windowId, inventory, 'return module.readSessionTasksLoadedIdentityJson()')

  if (typeof result !== 'string') { notReady('NONPRIMITIVE_RESPONSE') }

  return result
}

/** Inspector-only operation gate. Callers still own per-await controller admission and cleanup.
 * Check synchronously in the real operation realm, not only before IPC dispatch: an old
 * diagnostic reply delivered after a plugin reload must never authorize the next mutation.
 */
export async function evaluateSessionTasksWindowOperation(windowId: number, inventory: SessionTasksDiagnosticInventory, admittedJson: string, expression: string): Promise<unknown> {
  const admitted: unknown = JSON.parse(admittedJson)

  if (!admitted || typeof admitted !== 'object' || !('status' in admitted) || admitted.status !== 'MATCH' || !('currentSurfaceMatches' in admitted) || admitted.currentSurfaceMatches !== true) { notReady('ADMISSION_NOT_MATCH') }
  const expected = JSON.stringify(admittedJson)

  return dispatchWindow(windowId, inventory, `
    if(module.readSessionTasksLoadedIdentityJson()!==${expected})throw new Error('SESSION_TASKS_NOT_READY:OPERATION_IDENTITY_CHANGED');
    const value=await (${expression});
    if(module.readSessionTasksLoadedIdentityJson()!==${expected})throw new Error('SESSION_TASKS_NOT_READY:OPERATION_IDENTITY_CHANGED');
    return value;
  `)
}
