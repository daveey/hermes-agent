import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { host } from '@/sdk'
import type * as Assets from '@/sdk/loaded-assets'
import { identifyBytes } from '@/sdk/loaded-assets'

import { dropPlugin, setPluginEnabled } from './plugins-store'
import { loadRuntimePlugin, unloadRuntimePlugin } from './runtime-loader'
import { routeBlobImportsToDataUrls } from './runtime-loader.test-helpers'

// IO boundary only: real loader, SDK, shims, module evaluation and registration.
// A built-bundle replay supplies the actual runtime chunk URL separately.
let assetText = 'reviewed served fixture SDK chunk'
vi.mock('@/sdk/loaded-assets', async original => {
  const actual = await original<typeof Assets>()

  return { ...actual, readServedAssets: async () => [{ url: 'https://fixture.invalid/assets/sdk.js', ...await actual.identifyBytes(assetText) }] }
})
const companion = readFileSync(resolve(process.env.HERMES_K01_COMPANION ?? '../NO_COMPANION_INPUT'), 'utf8')
let restore: () => void
const read = () => host.diagnostics.readSessionTasksLoadedIdentity()
const surface = () => Object.getOwnPropertyDescriptor(window, '__sessionTasks')!.value
const load = (source = companion) => loadRuntimePlugin(source, 'session-tasks', { file: '/plugins/session-tasks/plugin.js' })
beforeEach(() => { restore = routeBlobImportsToDataUrls(); dropPlugin('session-tasks'); void setPluginEnabled('session-tasks', true) })
afterEach(() => { unloadRuntimePlugin('session-tasks'); restore(); vi.unstubAllGlobals() })

it('whole current companion registers through the actual SDK and loader, binds before successful settlement', async () => {
  expect(await load()).toBe('session-tasks')
  const identity = read()
  expect(identity.status).toBe('MATCH')
  expect(identity.currentSurfaceMatches).toBe(true)
  expect(identity.original).toEqual(await identifyBytes(companion))
  expect(identity.rewritten.sha256).toMatch(/^[a-f0-9]{64}$/)
  expect(identity.dependencies.map(d => d.specifier).sort()).toEqual(['@hermes/plugin-sdk','react','react/jsx-runtime'])
  expect(identity.dependencies.every(d => d.handle && d.shim.sha256 && d.servedAssets.length)).toBe(true)
  expect(identity.source).toEqual({ origin: 'session-tasks', path: '/plugins/session-tasks/plugin.js' })
  expect(JSON.stringify(identity)).not.toContain('get snapshot')
})
it('wrong-name refresh=warm fails exact per-name references', async () => {
  await load(); surface().refresh = surface().warm
  expect(read()).toMatchObject({ status: 'NOT_READY', reason: 'SURFACE_CHANGED', currentSurfaceMatches: false })
})
it('identical-text foreign closure fails without invoking getters', async () => {
  await load()
  const st = surface(), text = Object.getOwnPropertyDescriptor(st, 'snapshot')!.get!.toString()
  const get = Function('snapshot', `return Object.getOwnPropertyDescriptor({${text}}, 'snapshot').get`)({ foreign: true })
  Object.defineProperty(st, 'snapshot', { get, enumerable: true, configurable: true })
  expect(read().status).toBe('NOT_READY')
})
it.each(['replace', 'accessor', 'name', 'flag', 'symbol'])('current surface change %s is rejected without invoking accessors', async kind => {
  await load(); const st = surface(); const get = vi.fn(() => st)

  if (kind === 'replace') {(window as any).__sessionTasks = Object.create(null, Object.getOwnPropertyDescriptors(st))}

  if (kind === 'accessor') {Object.defineProperty(window, '__sessionTasks', { get, configurable: true })}

  if (kind === 'name') {st.extra = 1}

  if (kind === 'flag') {Object.defineProperty(st, 'warm', { enumerable: false })}

  if (kind === 'symbol') {st[Symbol('extra')] = true}
  expect(read().status).toBe('NOT_READY'); expect(get).not.toHaveBeenCalled()

  if (kind === 'accessor') {delete (window as any).__sessionTasks}
})
it('helper and whole-module byte changes are visible despite same exports/version', async () => {
  await load(); const first = read()
  await load(companion + '\n// helper revision\n'); const second = read()
  expect(second.status).toBe('MATCH'); expect(second.original.sha256).not.toBe(first.original.sha256)
  expect(second.rewritten.sha256).not.toBe(first.rewritten.sha256)
  expect(second.activationGeneration).not.toBe(first.activationGeneration)
})
it('cached shims retain their evaluation captures when globals change', async () => {
  await load(); const first = read()
  vi.stubGlobal('__HERMES_PLUGIN_SDK__', { foreign: true })
  await load(companion + '\n// second module\n')
  expect(read().status).toBe('MATCH')
  expect(read().dependencies).toEqual(first.dependencies)
})
it('forged global receipt and mutated diagnostic result never qualify a surface', async () => {
  await load(); const forged = read(); (forged as any).status = 'NOT_READY'
  ;(window as any).__sessionTasksLoadedIdentity = { status: 'MATCH' }
  surface().refresh = surface().warm
  expect(read().status).toBe('NOT_READY'); delete (window as any).__sessionTasksLoadedIdentity
})
it('unload invalidates synchronously before plugin disposal, reload has a new generation', async () => {
  await load(); const first = read(); unloadRuntimePlugin('session-tasks')
  expect(read().status).toBe('NOT_READY')
  await load(); expect(read().status).toBe('MATCH'); expect(read().activationGeneration).not.toBe(first.activationGeneration)
})
it('disable and navigation invalidate; history rollback cannot revive a generation', async () => {
  await load(); setPluginEnabled('session-tasks', false); expect(read().status).toBe('NOT_READY')
  setPluginEnabled('session-tasks', true); expect(read().status).toBe('MATCH')
  window.dispatchEvent(new Event('pagehide')); expect(read().status).toBe('NOT_READY')
  window.dispatchEvent(new Event('pageshow')); expect(read().status).toBe('NOT_READY')
})
it('uninstrumented plugin remains functional but cannot self-attest', async () => {
  await load(companion.replace('ctx.bindSessionTasksDiagnostic?.(surface)', 'void surface'))
  expect(surface().version).toBe('wait-visible-v1'); expect(read()).toMatchObject({status:'NOT_READY',reason:'MISSING_BINDING'})
})
it.each(['pagehide','beforeunload','popstate','hashchange'])('navigation event %s permanently retires current generation',async event=>{
  await load();window.dispatchEvent(new Event(event));expect(read()).toMatchObject({status:'NOT_READY',reason:'NAVIGATION_CHANGED'})
})
it('URL changes without events and document replacement retire the activation',async()=>{
  await load();const href=location.href
  history.replaceState(null,'',href+'#k01');expect(read().reason).toBe('NAVIGATION_CHANGED')
  history.replaceState(null,'',href);expect(read().status).toBe('NOT_READY')
  await load();vi.stubGlobal('document',document.implementation.createHTMLDocument('foreign realm document'))
  expect(read().reason).toBe('NAVIGATION_CHANGED');vi.unstubAllGlobals();expect(read().status).toBe('NOT_READY')
})
const wrapping = (body: string) => companion.replace('ctx.bindSessionTasksDiagnostic?.(surface)', body)
it('duplicate binding poisons qualification even if plugin catches the rejection', async () => {
  await load(wrapping('ctx.bindSessionTasksDiagnostic(surface); try { ctx.bindSessionTasksDiagnostic(surface) } catch {}'))
  expect(read()).toMatchObject({status:'NOT_READY', reason:'DUPLICATE_BINDING'})
})
it('late binder cannot run after synchronous register returns', async () => {
  await load(wrapping('globalThis.__lateBind = () => ctx.bindSessionTasksDiagnostic(surface)'))
  expect(() => (globalThis as any).__lateBind()).toThrow(); expect(read().status).toBe('NOT_READY')
  delete (globalThis as any).__lateBind
})
it('async register remains pending, rejection invalidates and old rejection cannot retire replacement', async () => {
  const asyncSource = wrapping('ctx.bindSessionTasksDiagnostic(surface); return new Promise((_, reject) => { globalThis.__rejectRegister = reject })')
  await load(asyncSource); expect(read()).toMatchObject({ status:'NOT_READY', reason:'REGISTRATION_PENDING' })
  const reject = (globalThis as any).__rejectRegister
  await load(); const generation = read().activationGeneration
  reject(new Error('old failure')); await Promise.resolve(); await Promise.resolve()
  expect(read()).toMatchObject({status:'MATCH', activationGeneration:generation})
  await load(asyncSource); (globalThis as any).__rejectRegister(new Error('current failure'))
  await Promise.resolve(); await Promise.resolve(); expect(read()).toMatchObject({status:'NOT_READY',reason:'REGISTRATION_REJECTED'})
  delete (globalThis as any).__rejectRegister
})
it('comment-separated supported SDK import still uses real loader rewrite and captures',async()=>{
  const source=companion.replace(/from\s+(['"])@hermes\/plugin-sdk\1/, 'from/*sdk comment*/"@hermes/plugin-sdk"')
  expect(source).not.toBe(companion)
  await load(source)
  expect(read()).toMatchObject({status:'MATCH',reason:'MATCH'})
})
it.each([
  "import/*gap*/'data:text/javascript,export{}'",
  "import//gap\n'data:text/javascript,export{}'",
  "export {default as foreign} from/*gap*/'data:text/javascript,export%20default%201'",
])('comment-separated unsupported static dependency %s is not qualified', async statement=>{
  expect(await load(companion+'\n'+statement+';\n')).toBeNull()
  expect(read()).toMatchObject({status:'NOT_READY',currentSurfaceMatches:false})
})
it.each(['import("react")', 'import /* comment */ ("react")', 'import(name)'])('dynamic import %s is never provenance-qualified', async expression => {
  await load(companion + `\nconst dynamic = name => ${expression};\n`)
  expect(read()).toMatchObject({status:'NOT_READY', reason:'UNSUPPORTED_DEPENDENCIES'})
})
it('reload invalidates before evaluation yields, even when replacement never registers', async () => {
  await load()
  const pending = load('throw new Error("replacement evaluation failed")')
  expect(read().status).toBe('NOT_READY')
  expect(await pending).toBe(null)
  expect(read().status).toBe('NOT_READY')
})
it('old companion cleanup cannot delete the replacement surface or style', async () => {
  await load(companion.replace('ctx.onDispose?.(dispose)', 'globalThis.__oldDispose = dispose; ctx.onDispose?.(dispose)'))
  const dispose = (globalThis as any).__oldDispose
  await load(); const generation = read().activationGeneration
  dispose()
  expect(read()).toMatchObject({ status:'MATCH', activationGeneration:generation })
  expect(document.getElementById('session-tasks-style')).not.toBe(null)
  delete (globalThis as any).__oldDispose
})
it('reentrant registration is rejected for diagnostic binding', async () => {
  let once = false

  ;(globalThis as any).__nest = () => { if (!once) { once = true; void setPluginEnabled('session-tasks', true) } }
  await load(wrapping('globalThis.__nest(); try { ctx.bindSessionTasksDiagnostic(surface) } catch(error) {globalThis.__reentrantError=error.message}'))
  expect(once).toBe(true)
  expect((globalThis as any).__reentrantError).toBe('Reentrant diagnostic binding')
  delete (globalThis as any).__reentrantError
  expect(read().status).toBe('NOT_READY')
  delete (globalThis as any).__nest
})
it('pending async registration can settle successfully without admitting a late bind', async () => {
  await load(wrapping('ctx.bindSessionTasksDiagnostic(surface); return new Promise(resolve => {globalThis.__resolveRegister = resolve})'))
  expect(read().reason).toBe('REGISTRATION_PENDING')
  ;(globalThis as any).__resolveRegister()
  await Promise.resolve(); await Promise.resolve()
  expect(read().status).toBe('MATCH')
  delete (globalThis as any).__resolveRegister
})
it('same-size renamed descriptor map and setter-only changes are rejected', async () => {
  await load(); const st=surface(), refresh=st.refresh
  delete st.refresh; st.foreign=refresh
  expect(read().status).toBe('NOT_READY')
  await load();Object.defineProperty(surface(),'snapshot',{set:()=>{},get:Object.getOwnPropertyDescriptor(surface(),'snapshot')!.get,enumerable:true,configurable:true})
  expect(read().status).toBe('NOT_READY')
})
it.each(['configurable','writable','enumerable'])('descriptor flag %s is owned',async flag=>{
  await load();Object.defineProperty(surface(),'refresh',{[flag]:false});expect(read().status).toBe('NOT_READY')
})
it('a method changed into an accessor is rejected without invoking it',async()=>{
  await load();const get=vi.fn(()=>surface().warm)
  Object.defineProperty(surface(),'refresh',{get,configurable:true,enumerable:true})
  expect(read().status).toBe('NOT_READY');expect(get).not.toHaveBeenCalled()
})
it.each(['null','42','() => {}'])('invalid surface %s cannot qualify',async value=>{
  await load(wrapping(`try {ctx.bindSessionTasksDiagnostic(${value})} catch(error){globalThis.__bindError=error.message}`))
  expect((globalThis as any).__bindError).toBe('Invalid Session Tasks binding')
  delete (globalThis as any).__bindError
  expect(read().status).toBe('NOT_READY')
})
it('a different plugin cannot use the Session Tasks binding',async()=>{
  const foreign=companion.replace("const ID = 'session-tasks'", "const ID = 'foreign-plugin'")
  await load(foreign.replace('ctx.bindSessionTasksDiagnostic?.(surface)', 'try {ctx.bindSessionTasksDiagnostic(surface)} catch(error){globalThis.__bindError=error.message}'))
  expect((globalThis as any).__bindError).toBe('Invalid Session Tasks binding')
  delete (globalThis as any).__bindError
  unloadRuntimePlugin('foreign-plugin')
  expect(read().pluginId).toBe('session-tasks')
})
it('unrelated uninstrumented plugins preserve the active Session Tasks generation',async()=>{
  await load();const first=read()
  const pending=loadRuntimePlugin('export default {id:"unrelated",register(){}}','test',{file:'/other/plugin.js'})
  expect(read().status).toBe('MATCH');expect(await pending).toBe('unrelated')
  expect(read()).toMatchObject({status:'MATCH',activationGeneration:first.activationGeneration})
  unloadRuntimePlugin('unrelated')
})
it('missing file association is not permission to invalidate another no-file plugin',async()=>{
  await loadRuntimePlugin(companion,'test')
  expect(read()).toMatchObject({status:'MATCH',source:{path:null}})
  const pending=loadRuntimePlugin('export default {id:"unrelated-no-file",register(){}}','test')
  expect(read().status).toBe('MATCH');await pending;expect(read().status).toBe('MATCH')
  unloadRuntimePlugin('unrelated-no-file')
})
it('static dependency outside the reviewed companion set cannot qualify',async()=>{
  await load(companion+"\nimport * as dev from 'react/jsx-dev-runtime';\n")
  expect(read()).toMatchObject({status:'NOT_READY',reason:'UNSUPPORTED_DEPENDENCIES'})
})
it('diagnostic is by-value and exposes neither captured namespaces nor surface references',async()=>{
  await load();const first=read(), serialized=JSON.stringify(first)
  first.original.sha256='forged';first.dependencies[0].shim.sha256='forged';first.dependencies[0].servedAssets[0].sha256='forged'
  expect(JSON.stringify(read())).toBe(serialized)
  expect(Object.hasOwn(read().dependencies[0],'namespace')).toBe(false)
})
it('invalidation precedes every disposer', async () => {
  const observed:string[] = []

  ;(globalThis as any).__observeDispose = () => observed.push(read().status)
  await load(companion.replace('ctx.onDispose?.(dispose)', 'ctx.onDispose?.(() => globalThis.__observeDispose()); ctx.onDispose?.(dispose)'))
  unloadRuntimePlugin('session-tasks')
  expect(observed).toEqual(['NOT_READY'])
  delete (globalThis as any).__observeDispose
})
