/** Loader-owned Session Tasks provenance; no receipt ingestion or exported refs.
 * This associates reviewed module bytes with references, not mutable closure state
 * or confinement against arbitrary hostile code in the same renderer.
 */
import { randomUuid } from '@hermes/shared'

import { descriptorCustody, diagnosticJson } from './diagnostic-primitives'
const ownDescriptor = Object.getOwnPropertyDescriptor
const ownedDocument = () => document
const ownedLocation = typeof window === 'undefined' ? null : window.location
const locationHref = typeof window === 'undefined' ? undefined : Object.getOwnPropertyDescriptor(window.location, 'href')?.get
const invoke = Reflect.apply
const navigationHref = () => locationHref && ownedLocation ? invoke(locationHref, ownedLocation, []) as string : location.href

import type { ByteIdentity, ServedAsset } from '@/sdk/loaded-assets'

export interface DependencyIdentity {
  specifier: string
  handle: string
  shimUrl: string
  shim: ByteIdentity
  servedAssets: ServedAsset[]
}
export interface CapturedDependency extends DependencyIdentity {
  namespace: object
  importedNamespace: object
}
export interface SessionTasksLoadedIdentity {
  schemaVersion: 1
  status: 'MATCH' | 'NOT_READY'
  reason: string
  pluginId: string
  source: { origin: string; path: string | null }
  original: ByteIdentity
  rewritten: ByteIdentity
  activationGeneration: number
  realm: string
  navigation: string
  dependencies: DependencyIdentity[]
  currentSurfaceMatches: boolean
}
interface Input {
  pluginId: string
  origin: string
  path?: string
  original: ByteIdentity
  rewritten: ByteIdentity
  module: object
  sourceText: { original: string; rewritten: string }
  dependencies: CapturedDependency[]
  dependencyReason: string | null
  sdkCurrent: () => boolean
}
interface Activation {
  input: Input
  generation: number
  document: Document
  navigation: string
  reason: string
  surface?: object
  surfaceCurrent?: () => boolean
}

export function createSessionTasksIdentity() {
const realm = randomUuid()
let generation = 0
let current: Activation | undefined
let registering: Activation | undefined

function invalidate(activation: Activation, reason: string) { activation.reason = reason }

const navigate = () => { if (current) {invalidate(current, 'NAVIGATION_CHANGED')} }

if (typeof window !== 'undefined') {
  for (const event of ['pagehide', 'beforeunload', 'popstate', 'hashchange']) {window.addEventListener(event, navigate)}
}

function sameSurface(activation: Activation): boolean {
  if (!activation.surface || !activation.surfaceCurrent) {return false}
  const descriptor = ownDescriptor(window, '__sessionTasks')

  return descriptor?.value === activation.surface && activation.surfaceCurrent()
}

function sessionTasksActivation(input: Input) {
  const activation: Activation = { input, generation: ++generation, document, navigation: location.href, reason: 'REGISTRATION_PENDING' }

  if (input.pluginId === 'session-tasks') {current = activation}
  let called = false

  const bind = (surface: object) => {
    if (activation.reason === 'REENTRANT_REGISTRATION') {throw new Error('Reentrant diagnostic binding')}

    if (registering !== activation) { invalidate(activation, 'OUTSIDE_REGISTRATION'); throw new Error('Session Tasks binding outside synchronous registration') }

    if (called) { invalidate(activation, 'DUPLICATE_BINDING'); throw new Error('Session Tasks binding already supplied') }
    called = true

    if (input.pluginId !== 'session-tasks' || typeof surface !== 'object' || surface === null) {
      invalidate(activation, 'INVALID_SURFACE'); throw new Error('Invalid Session Tasks binding')
    }

    activation.surface = surface
    activation.surfaceCurrent = descriptorCustody(surface)
  }

  return {
    bind,
    run<T>(run: () => T): T {
      if (registering) {
        invalidate(registering, 'REENTRANT_REGISTRATION'); invalidate(activation, 'REENTRANT_REGISTRATION')
      }

      const previous = registering
      registering = activation

      try { return run() } finally { registering = previous }
    },
    settle() {
      if (activation.reason !== 'REGISTRATION_PENDING') {return}
      activation.reason = input.dependencyReason ?? (activation.surface ? 'MATCH' : 'MISSING_BINDING')
    },
    invalidate(reason = 'UNLOADED') { invalidate(activation, reason) }
  }
}

/** Synchronous and by-value: getters on the companion are never invoked. */
function readSessionTasksLoadedIdentity(): SessionTasksLoadedIdentity {
  const active = current
  const zero = { sha256: '', byteLength: 0 }

  if (!active) {return { schemaVersion:1, status:'NOT_READY', reason:'NOT_LOADED', pluginId:'session-tasks', source:{origin:'',path:null}, original:zero, rewritten:zero, activationGeneration:0, realm, navigation:location.href, dependencies:[], currentSurfaceMatches:false }}

  if (active.document !== ownedDocument() || active.navigation !== navigationHref()) {invalidate(active, 'NAVIGATION_CHANGED')}
  const matches = sameSurface(active)
  const reason = active.reason !== 'MATCH' ? active.reason : !active.input.sdkCurrent() ? 'SDK_CHANGED' : !matches ? 'SURFACE_CHANGED' : 'MATCH'
  const dependencies: DependencyIdentity[] = []

  for (let i = 0; i < active.input.dependencies.length; i++) {
    const input = active.input.dependencies[i]
    const servedAssets: ServedAsset[] = []

    for (let j = 0; j < input.servedAssets.length; j++) { servedAssets[j] = {...input.servedAssets[j]} }
    dependencies[i] = {specifier:input.specifier, handle:input.handle, shimUrl:input.shimUrl, shim:{...input.shim}, servedAssets}
  }

  return {
    schemaVersion:1, status: reason === 'MATCH' ? 'MATCH' : 'NOT_READY', reason,
    pluginId:active.input.pluginId, source:{origin:active.input.origin,path:active.input.path ?? null},
    original:{...active.input.original}, rewritten:{...active.input.rewritten}, activationGeneration:active.generation,
    realm, navigation:active.navigation,
    dependencies,
    currentSurfaceMatches:matches
  }
}

/** Supported immutable ESM binding. Primitive return avoids then/toJSON host hooks. */
function readSessionTasksLoadedIdentityJson(): string {
  return diagnosticJson(readSessionTasksLoadedIdentity())
}

return { activate: sessionTasksActivation, read: readSessionTasksLoadedIdentity, readJson: readSessionTasksLoadedIdentityJson }
}
