/**
 * Runtime SDK injection — the other half of the vscode-module model. Bundled
 * plugins resolve `@hermes/plugin-sdk` through the vite alias; RUNTIME-loaded
 * plugins (disk / fetched) import the same specifier and get the same object:
 * the loader rewrites bare specifiers to shim modules that re-export the
 * live namespaces installed here. React ships as the app's singletons —
 * a second React instance would break hooks.
 */

import * as React from 'react'
import * as jsxDevRuntime from 'react/jsx-dev-runtime'
import * as jsxRuntime from 'react/jsx-runtime'

import type { CapturedDependency } from '@/contrib/session-tasks-identity'

import { identifyBytes, readServedAssets } from './loaded-assets'

import * as sdk from './index'
const ownDescriptor = Object.getOwnPropertyDescriptor

/** SDK objects are mutable for compatibility; this predicate never trusts their published copies. */
export function sessionTasksRuntimeCurrent(): boolean {
  const published = ownDescriptor(globalThis, '__HERMES_PLUGIN_SDK__')

  return published?.value === sdk && sdk.sessionTasksSdkCurrent()
}

// Resolved LAZILY, never as a module-scope literal. This module sits in an
// import cycle — `sdk/index` → `contrib/*` → `contrib/runtime-loader` →
// `sdk/runtime` — so a module-scope `{ __HERMES_PLUGIN_SDK__: sdk, … }` is
// evaluated BEFORE `sdk/index`'s own body runs. In the bundled app that read
// yields `undefined` (the bundler emits the namespace as a hoisted `var`), so
// `Object.keys(GLOBALS.__HERMES_PLUGIN_SDK__)` threw
// "Cannot convert undefined or null to object" and EVERY runtime (disk)
// plugin failed to load. Reading them at call time — installPluginSdk() and
// the shim builder only ever run once the app is up — gets the live
// namespaces.
function pluginNamespaces() {
  return {
    __HERMES_PLUGIN_SDK__: sdk,
    __HERMES_REACT__: React,
    __HERMES_REACT_JSX__: jsxRuntime,
    __HERMES_REACT_JSX_DEV__: jsxDevRuntime
  }
}

type PluginGlobalKey = keyof ReturnType<typeof pluginNamespaces>

export function installPluginSdk(): void {
  Object.assign(globalThis, pluginNamespaces())
}

/** Build a shim ESM blob that re-exports a global namespace's live members.
 *  Export names come from the namespace itself, so the list can't drift. */
function shimUrl(globalKey: PluginGlobalKey): string {
  const names = Object.keys(pluginNamespaces()[globalKey]).filter(
    name => name !== 'default' && /^[A-Za-z_$][\w$]*$/.test(name)
  )

  const source =
    `const m = globalThis.${globalKey};\n` +
    `globalThis.__HERMES_CAPTURE_PLUGIN_SHIM__?.(import.meta.url, m);\n` +
    `export default m.default ?? m;\n` +
    // Guard the destructuring: `export const {  } = m` is a syntax error, so
    // only emit it when the namespace actually has named exports.
    (names.length ? `export const { ${names.join(', ')} } = m;\n` : '')

  const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }))
  shimSources.set(url, source)

  return url
}

let cached: Record<string, string> | null = null
const shimSources = new Map<string, string>()
const captures = new Map<
  string,
  {
    handle: string
    namespace: object
    evidence: Promise<{
      shim: Awaited<ReturnType<typeof identifyBytes>>
      servedAssets: Awaited<ReturnType<typeof readServedAssets>>
      reason: string | null
    }>
  }
>()
let captureUsers = 0
let priorCapture: PropertyDescriptor | undefined
const captureKey = '__HERMES_CAPTURE_PLUGIN_SHIM__'

const capture = (url: string, namespace: object) => {
  if (!shimSources.has(url) || captures.has(url)) {
    return
  }

  // Start IO at evaluation, never from whatever globals happen to exist at register().
  const evidence = Promise.all([identifyBytes(shimSources.get(url)!), readServedAssets(import.meta.url)])
    .then(([shim, servedAssets]) => ({ shim, servedAssets, reason: null as string | null }))
    .catch(() => ({ shim: { sha256: '', byteLength: 0 }, servedAssets: [], reason: 'SERVED_ASSETS_UNAVAILABLE' }))

  captures.set(url, { handle: `dependency:${captures.size + 1}`, namespace, evidence })
}

/** Internal evaluation scope. The shim reports references, never a caller receipt.
 * It is removed after import; cached ESMs retain the original capture forever. */
export function beginSdkCapture(): () => void {
  if (captureUsers++ === 0) {
    priorCapture = Object.getOwnPropertyDescriptor(globalThis, captureKey)
    Object.defineProperty(globalThis, captureKey, {
      value: capture,
      configurable: true,
      writable: false,
      enumerable: false
    })
  }

  let ended = false

  return () => {
    if (ended) {
      return
    }
    ended = true

    if (--captureUsers === 0) {
      if (priorCapture) {
        Object.defineProperty(globalThis, captureKey, priorCapture)
      } else {
        Reflect.deleteProperty(globalThis, captureKey)
      }
    }
  }
}

export async function capturedSdkDependencies(
  specifiers: string[]
): Promise<{ dependencies: CapturedDependency[]; reason: string | null }> {
  const map = sdkImportMap()
  const dependencies: CapturedDependency[] = []

  for (const specifier of [...new Set(specifiers)].sort()) {
    const url = map[specifier],
      captured = captures.get(url)

    if (!captured) {
      return { dependencies, reason: 'MISSING_DEPENDENCY_CAPTURE' }
    }
    const evidence = await captured.evidence

    if (evidence.reason) {
      return { dependencies, reason: evidence.reason }
    }
    dependencies.push({
      specifier,
      handle: captured.handle,
      shimUrl: url,
      namespace: captured.namespace,
      importedNamespace: await import(/* @vite-ignore */ url),
      shim: evidence.shim,
      servedAssets: evidence.servedAssets
    })
  }

  return { dependencies, reason: null }
}

/** Specifier -> shim URL map for the runtime loader (longest keys first). */
export function sdkImportMap(): Record<string, string> {
  cached ??= {
    '@hermes/plugin-sdk': shimUrl('__HERMES_PLUGIN_SDK__'),
    'react/jsx-dev-runtime': shimUrl('__HERMES_REACT_JSX_DEV__'),
    'react/jsx-runtime': shimUrl('__HERMES_REACT_JSX__'),
    react: shimUrl('__HERMES_REACT__')
  }

  return cached
}
