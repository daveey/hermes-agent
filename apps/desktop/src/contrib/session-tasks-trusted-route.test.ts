import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { host } from '@/sdk'
import type * as Assets from '@/sdk/loaded-assets'

import { readK01Companion } from './k01-companion.test-helpers'
import { dropPlugin, setPluginEnabled } from './plugins-store'
import { loadRuntimePlugin, readSessionTasksLoadedIdentityJson, unloadRuntimePlugin } from './runtime-loader'
import { readSessionTasksLoadedIdentity } from './runtime-loader'
import { routeBlobImportsToDataUrls } from './runtime-loader.test-helpers'

vi.mock('@/sdk/loaded-assets', async original => {
  const actual = await original<typeof Assets>()

  return { ...actual, readServedAssets: async () => [{ url: 'https://fixture.invalid/assets/sdk.js', ...await actual.identifyBytes('pinned SDK fixture transport') }] }
})
const companion = readK01Companion()
const load = () => loadRuntimePlugin(companion, 'session-tasks', { file: '/plugins/session-tasks/plugin.js' })
let restore: () => void
beforeEach(() => { restore = routeBlobImportsToDataUrls(); dropPlugin('session-tasks'); void setPluginEnabled('session-tasks', true) })
afterEach(() => { unloadRuntimePlugin('session-tasks'); restore(); vi.restoreAllMocks() })

it.each(['before load', 'after admission'])('genuine nested SDK mutation %s refuses live identity', async when => {
  if (when === 'after admission') {
    await load()
    expect(readSessionTasksLoadedIdentity().status).toBe('MATCH')
  }

  const original = host.sessions.beginWorkSourceRead
  let result: ReturnType<typeof readSessionTasksLoadedIdentity>

  try {
    host.sessions.beginWorkSourceRead = (() => { throw new Error('foreign session method') }) as typeof original

    if (when === 'before load') { await load() }
    result = readSessionTasksLoadedIdentity()
  } finally { host.sessions.beginWorkSourceRead = original }

  expect(result!).toMatchObject({ status: 'NOT_READY', reason: 'SDK_CHANGED' })
})

it('prototype every replacement cannot bless refresh=warm', async () => {
  await load()
  const surface = Object.getOwnPropertyDescriptor(window, '__sessionTasks')!.value
  surface.refresh = surface.warm
  const original = Array.prototype.every
  let result: ReturnType<typeof readSessionTasksLoadedIdentity>

  try {
    Array.prototype.every = (() => true) as unknown as typeof original
    result = readSessionTasksLoadedIdentity()
  } finally { Array.prototype.every = original }

  expect(result!).toMatchObject({ status: 'NOT_READY', reason: 'SURFACE_CHANGED' })
})

it('primitive JSON diagnostic ignores inherited serialization and await hooks', async () => {
  await load()
  const surface = Object.getOwnPropertyDescriptor(window, '__sessionTasks')!.value
  surface.refresh = surface.warm
  const every = Array.prototype.every, map = Array.prototype.map, json = JSON.stringify
  let hooks = 0, text = ''

  try {
    Array.prototype.every = (() => true) as unknown as typeof every
    Array.prototype.map = (() => { hooks++;

 return [] }) as typeof map
    Object.defineProperty(Object.prototype, 'toJSON', { configurable: true, value: () => { hooks++;

 return { status: 'MATCH' } } })
    Object.defineProperty(Object.prototype, 'then', { configurable: true, value: () => { hooks++ } })
    JSON.stringify = (() => { hooks++;

 return 'forged' }) as typeof json
    text = readSessionTasksLoadedIdentityJson()
  } finally {
    Array.prototype.every = every; Array.prototype.map = map; JSON.stringify = json
    delete (Object.prototype as { toJSON?: unknown }).toJSON
    delete (Object.prototype as { then?: unknown }).then
  }

  expect(typeof text).toBe('string')
  expect(JSON.parse(text)).toMatchObject({ status: 'NOT_READY', reason: 'SURFACE_CHANGED' })
  expect(hooks).toBe(0)
})

it('published SDK replacement is detected without invoking a foreign accessor', async () => {
  await load()
  const original = Object.getOwnPropertyDescriptor(globalThis, '__HERMES_PLUGIN_SDK__')!
  const get = vi.fn(() => ({ host }))
  let text = ''

  try {
    Object.defineProperty(globalThis, '__HERMES_PLUGIN_SDK__', { get, configurable: true })
    text = readSessionTasksLoadedIdentityJson()
  } finally { Object.defineProperty(globalThis, '__HERMES_PLUGIN_SDK__', original) }

  expect(JSON.parse(text)).toMatchObject({ status: 'NOT_READY', reason: 'SDK_CHANGED' })
  expect(get).not.toHaveBeenCalled()
})

it('replacing the genuine exported host diagnostic does not replace the direct reader', async () => {
  await load()
  const old = readSessionTasksLoadedIdentity()
  const original = host.diagnostics.readSessionTasksLoadedIdentity
  const surface = Object.getOwnPropertyDescriptor(window, '__sessionTasks')!.value
  surface.refresh = surface.warm
  let result: ReturnType<typeof readSessionTasksLoadedIdentity>

  try {
    host.diagnostics.readSessionTasksLoadedIdentity = () => old
    expect(host.diagnostics.readSessionTasksLoadedIdentity().status).toBe('MATCH')
    result = readSessionTasksLoadedIdentity()
  } finally { host.diagnostics.readSessionTasksLoadedIdentity = original }

  expect(result!.status).toBe('NOT_READY')
})
