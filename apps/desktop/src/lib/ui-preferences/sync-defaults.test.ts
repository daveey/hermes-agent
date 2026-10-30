import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, expect, it, vi } from 'vitest'

import {
  parseUiPreferencesWrite,
  readUiPreferences,
  type UiPreferencesSnapshot,
  writeUiPreferences
} from '../../../electron/ui-preferences'

import type { UiPreferencesBridge } from './sync'
import { observeSync } from './sync-test-bridge'

// A value a store writes for itself at load (its default, a migration from
// config) is not the user's choice and must never reach the machine's store
// ahead of the client that holds the real one (#145536 review), against the
// real native writer on a real file.
const TRANSLUCENCY = 'hermes.desktop.translucency.v2'
const AUTO_SPEAK = 'hermes.desktop.autoSpeakReplies'
const THEME = 'hermes-desktop-theme-v2'
const KEYBINDS = 'hermes.desktop.keybinds'
const HAPTICS_MUTED = 'hermes.desktop.hapticsMuted'

// Every store that writes its value the moment its module evaluates.
const LOAD_WRITERS = [
  () => import('@/store/backdrop'),
  () => import('@/store/completion-sound'),
  () => import('@/store/external-links'),
  () => import('@/store/haptics'),
  () => import('@/store/keybinds'),
  () => import('@/store/profile'),
  () => import('@/store/reasoning-disclosure'),
  () => import('@/store/thread-timeline'),
  () => import('@/store/tool-view')
]

// Transform every store this file loads while it is collected, outside any
// test's timeout: vi.resetModules() keeps the transformed code, so a load only
// re-evaluates it. Left to the first boot(), the cold transform timed that
// test out at 15s on a CPU-starved runner.
await Promise.all([
  import('./sync'),
  import('@/store/translucency'),
  import('@/store/voice-prefs'),
  ...LOAD_WRITERS.map(load => load())
])
// Drop the warm-up's evaluated stores and the defaults they wrote at load, so
// the first test starts from the same empty page as every later one.
vi.resetModules()
window.localStorage.clear()
window.sessionStorage.clear()

const dirs: string[] = []

function storeFile(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-ui-defaults-'))
  dirs.push(dir)

  return path.join(dir, 'ui-preferences.json')
}

function realBridge(file: string) {
  let changed: ((snapshot: UiPreferencesSnapshot) => void) | null = null

  const bridge: UiPreferencesBridge = {
    read: async () => readUiPreferences(file),
    write: payload => writeUiPreferences(file, parseUiPreferencesWrite(payload)),
    onChanged: callback => {
      changed = callback

      return () => (changed = null)
    }
  }

  return { bridge, push: () => changed?.(readUiPreferences(file)) }
}

function storageKeys() {
  return Array.from({ length: window.localStorage.length }, (_, index) => window.localStorage.key(index) ?? '')
}

function expectNoPendingDefaults(ownKeys: Set<string>) {
  // A leaked provisional write is queued synchronously. Checking the outbox
  // catches it even before the debounce fires; stopping sync must not hide it.
  // A value this client already owned may legitimately be queued during join.
  const pending = storageKeys()
    .filter(key => key.startsWith('hermes:ui-preferences:outbox:'))
    .flatMap(key => Object.keys(JSON.parse(window.localStorage.getItem(key) ?? '{}').values ?? {}))
    .filter(key => !ownKeys.has(key))

  expect(pending).toEqual([])
}

/** One page load, as boot.ts + main.tsx run it: capture, hydrate, then the app's stores. */
async function load(file: string, app: () => Promise<void>) {
  vi.resetModules()
  const sync = await import('./sync')
  sync.captureOwnPreferences()
  sync.hydrateFromSnapshot(readUiPreferences(file))
  const ownKeys = new Set(storageKeys())
  const { bridge, push } = realBridge(file)
  const client = observeSync(sync.startUiPreferencesSync, bridge)
  await app()
  await client.connected()
  expectNoPendingDefaults(ownKeys)

  return { ...client, push }
}

/** One page load in production order: the app's stores evaluate before main.tsx starts the sync. */
async function boot(file: string) {
  vi.resetModules()
  const sync = await import('./sync')
  sync.captureOwnPreferences()
  sync.hydrateFromSnapshot(readUiPreferences(file))
  const ownKeys = new Set(storageKeys())

  for (const load of LOAD_WRITERS) {
    await load()
  }

  const client = observeSync(sync.startUiPreferencesSync, realBridge(file).bridge)
  await client.connected()
  expectNoPendingDefaults(ownKeys)
  client.stop()
}

/** Native Desktop's localStorage is another origin's: switch to it. */
function becomeNative(values: Record<string, string>) {
  window.localStorage.clear()
  window.sessionStorage.clear()
  Object.entries(values).forEach(([key, value]) => window.localStorage.setItem(key, value))
}

afterEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()

  for (const dir of dirs.splice(0)) {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

it('a fresh browser’s translucency default never replaces native’s custom translucency', async () => {
  const file = storeFile()
  const browser = await load(file, async () => void (await import('@/store/translucency')))
  browser.stop()

  const custom = JSON.stringify({ mode: 'clear', base: { intensity: 61 }, light: {}, dark: {} })
  becomeNative({ [TRANSLUCENCY]: custom })
  const native = await load(file, async () => void (await import('@/store/translucency')))
  native.stop()

  expect(window.localStorage.getItem(TRANSLUCENCY)).toBe(custom)
  expect(readUiPreferences(file).values[TRANSLUCENCY]).toBe(custom)
})

it('a fresh browser’s auto-speak migration never replaces native’s choice, across a browser reload too', async () => {
  const file = storeFile()

  const migrate = async () => {
    const prefs = await import('@/store/voice-prefs')
    prefs.applyAutoSpeakFromConfig({ voice: { auto_tts: false } })
  }

  ;(await load(file, migrate)).stop()
  // The reload finds its migrated value already in localStorage.
  ;(await load(file, migrate)).stop()
  expect(window.localStorage.getItem(AUTO_SPEAK)).toBe('false')

  becomeNative({ [AUTO_SPEAK]: 'true' })
  const native = await load(file, migrate)
  native.stop()

  expect(window.localStorage.getItem(AUTO_SPEAK)).toBe('true')
  expect(readUiPreferences(file).values[AUTO_SPEAK]).toBe('true')
})

it('a migrated auto-speak value stays put here while the store lacks it, and a real choice is shared', async () => {
  const file = storeFile()
  becomeNative({ [THEME]: 'nous' })
  const choose: { autoSpeak?: (enabled: boolean) => Promise<void> } = {}

  const native = await load(file, async () => {
    const prefs = await import('@/store/voice-prefs')
    prefs.applyAutoSpeakFromConfig({ voice: { auto_tts: true } })
    choose.autoSpeak = prefs.setAutoSpeakReplies
  })

  // Another client's change reaches this joined window: nothing it lacks is pruned.
  await writeUiPreferences(file, { values: { [THEME]: 'mono' } })
  native.push()
  expect(window.localStorage.getItem(AUTO_SPEAK)).toBe('true')
  expect(readUiPreferences(file).values).toEqual({ [THEME]: 'mono' })

  await native.writeAfter(() => choose.autoSpeak?.(false))
  native.stop()

  expect(readUiPreferences(file).values).toEqual({ [AUTO_SPEAK]: 'false', [THEME]: 'mono' })
})

it('a fresh browser that reloads before native connects never fills the store with its defaults', async () => {
  const file = storeFile()
  await boot(file)
  await boot(file)

  expect(window.localStorage.getItem(KEYBINDS)).toBe('{}')
  expect(readUiPreferences(file).values).toEqual({})

  const keybinds = JSON.stringify({ 'composer.modelPicker': ['mod+shift+k'] })
  becomeNative({ [HAPTICS_MUTED]: 'true', [KEYBINDS]: keybinds })
  await boot(file)

  expect(window.localStorage.getItem(KEYBINDS)).toBe(keybinds)
  expect(window.localStorage.getItem(HAPTICS_MUTED)).toBe('true')
  expect(readUiPreferences(file).values).toEqual({ [HAPTICS_MUTED]: 'true', [KEYBINDS]: keybinds })
})
