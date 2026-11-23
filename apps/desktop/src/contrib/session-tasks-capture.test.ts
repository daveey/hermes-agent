
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type * as Sdk from '@/sdk'
import type * as Assets from '@/sdk/loaded-assets'
import type * as Runtime from '@/sdk/runtime'

import { readK01Companion } from './k01-companion.test-helpers'
import type * as Loader from './runtime-loader'
import { routeBlobImportsToDataUrls } from './runtime-loader.test-helpers'
const companion=readK01Companion()
const assetRead=vi.hoisted(()=>vi.fn())
vi.mock('@/sdk/loaded-assets',async importOriginal=>({ ...await importOriginal<typeof Assets>(),readServedAssets:assetRead }))
let restore:()=>void, loader:typeof Loader, sdk:typeof Sdk, runtime:typeof Runtime
beforeEach(async()=>{
  vi.resetModules();assetRead.mockReset();assetRead.mockResolvedValue([{url:'https://fixture.invalid/assets/real-chunk.js',sha256:'a'.repeat(64),byteLength:10}])
  restore=routeBlobImportsToDataUrls();sdk=await import('@/sdk');runtime=await import('@/sdk/runtime');loader=await import('./runtime-loader')
})
afterEach(()=>{loader.unloadRuntimePlugin('session-tasks');restore();delete (window as any).__sessionTasks})
const read=()=>sdk.host.diagnostics.readSessionTasksLoadedIdentity()
const load=(source=companion)=>loader.loadRuntimePlugin(source,'session-tasks',{file:'/fixture/plugin.js'})
it('before any module loads, supported SDK diagnostic explicitly reports NOT_LOADED',()=>{
  expect(read()).toMatchObject({schemaVersion:1,status:'NOT_READY',reason:'NOT_LOADED',activationGeneration:0,currentSurfaceMatches:false,dependencies:[]})
  expect(Object.getOwnPropertyDescriptor(globalThis,'__HERMES_CAPTURE_PLUGIN_SHIM__')).toBeUndefined()
})
it('references and cached shim captures belong to evaluation, not replaced registration globals',async()=>{
  await load();expect(read().status).toBe('MATCH')
  const prior=await runtime.capturedSdkDependencies(['@hermes/plugin-sdk','react','react/jsx-runtime'])
  const sdkRef=(globalThis as any).__HERMES_PLUGIN_SDK__, reactRef=(globalThis as any).__HERMES_REACT__

  try {
    await load(companion.replace('export default', 'globalThis.__HERMES_PLUGIN_SDK__={foreign:true}; globalThis.__HERMES_REACT__={foreign:true}; export default'));expect(read().status).toBe('MATCH')
    expect((globalThis as any).__HERMES_PLUGIN_SDK__.foreign).toBe(true)
    const next=await runtime.capturedSdkDependencies(['@hermes/plugin-sdk','react','react/jsx-runtime'])
    expect(next.dependencies[0].namespace).toBe(sdkRef)
    expect(next.dependencies[1].namespace).toBe(reactRef)
    expect(next.dependencies[0].importedNamespace).toBe(prior.dependencies[0].importedNamespace)
    expect(read().dependencies).toEqual(prior.dependencies.map(({namespace,importedNamespace,...rest})=>rest))
    expect(assetRead).toHaveBeenCalledTimes(3)
  } finally {Object.assign(globalThis,{__HERMES_PLUGIN_SDK__:sdkRef,__HERMES_REACT__:reactRef})}
})
it('asset-read rejection is retained at evaluation and never replaced by forged registration evidence',async()=>{
  assetRead.mockRejectedValue(new Error('missing served bytes'))
  await load(companion.replace('ctx.bindSessionTasksDiagnostic?.(surface)', 'ctx.bindSessionTasksDiagnostic?.(surface, {status:"MATCH",sha256:"forged"})'))
  expect(read()).toMatchObject({status:'NOT_READY',reason:'SERVED_ASSETS_UNAVAILABLE'})
  assetRead.mockResolvedValue([{url:'https://fixture.invalid/fake',sha256:'b'.repeat(64),byteLength:9}])
  await load();expect(read().status).toBe('NOT_READY');expect(assetRead).toHaveBeenCalledTimes(3)
})
it('unknown and duplicate shim capture calls cannot replace actual evaluation references',async()=>{
  await load();expect(read().status).toBe('MATCH')
  const prior=await runtime.capturedSdkDependencies(['@hermes/plugin-sdk'])
  const end=runtime.beginSdkCapture()

  try {
    ;(globalThis as any).__HERMES_CAPTURE_PLUGIN_SHIM__('foreign-url',{})
    ;(globalThis as any).__HERMES_CAPTURE_PLUGIN_SHIM__(prior.dependencies[0].shimUrl,{foreign:true})
  } finally {end()}

  const next=await runtime.capturedSdkDependencies(['@hermes/plugin-sdk'])
  expect(next.dependencies[0].namespace).toBe(prior.dependencies[0].namespace)
  expect(assetRead).toHaveBeenCalledTimes(3)
  const missing=await runtime.capturedSdkDependencies(['not-a-supported-shim'])
  expect(missing.reason).toBe('MISSING_DEPENDENCY_CAPTURE')
  expect(missing.dependencies).toEqual([])
})
it('capture hook lifetime is refcounted, end is idempotent and prior descriptor is restored',()=>{
  const key='__HERMES_CAPTURE_PLUGIN_SHIM__',prior=Object.getOwnPropertyDescriptor(globalThis,key)

  const foreign=()=>{}
  Object.defineProperty(globalThis,key,{value:foreign,writable:true,configurable:true,enumerable:true})
  const expected=Object.getOwnPropertyDescriptor(globalThis,key)
  const end1=runtime.beginSdkCapture(), end2=runtime.beginSdkCapture()
  expect((globalThis as any)[key]).not.toBe(foreign)
  end1();end1();expect((globalThis as any)[key]).not.toBe(foreign)
  end2();expect(Object.getOwnPropertyDescriptor(globalThis,key)).toEqual(expected)

  if(prior){Object.defineProperty(globalThis,key,prior);}else {delete (globalThis as any)[key]}
})
it('disk refresh invalidates before replacement file read settles',async()=>{
  const root='/fixture/desktop-plugins', file=`${root}/session-tasks/plugin.js`

  let source=companion, reads=0, release:((result:{text:string})=>void)|undefined, started:()=>void=()=>{}

  const began=new Promise<void>(resolve=>{started=resolve})

  ;(window as any).hermesDesktop={
    desktopPluginsRoot:async()=>root,
    readDir:async(dir:string)=>({entries:dir===root?[{isDirectory:true,name:'session-tasks',path:`${root}/session-tasks`}]:[{isDirectory:false,name:'plugin.js',path:file}]}),
    readFileText:async()=>{reads++;

if(reads===3){started();

return new Promise<{text:string}>(resolve=>{release=resolve})}

return {text:source}},
    logLine:()=>{},watchPreviewFile:async()=>({id:'fixture-watch'}),stopPreviewFileWatch:async()=>true
  }

  try {
    await loader.discoverRuntimePlugins();expect(read().status).toBe('MATCH')
    source+='\n// actual disk replacement\n'
    const pending=loader.refreshChangedDiskPlugins('fixture')
    await began;expect(read().status).toBe('NOT_READY')
    release!({text:source});await pending
    expect(read().status).toBe('MATCH')
  } finally {delete (window as any).hermesDesktop}
})
it('original and rewritten module byte changes are observed, not replaced by caller claims',async()=>{
  await load();const before=read()
  await load(companion+'\n// different helper module bytes\n');const after=read()
  expect(after.status).toBe('MATCH');expect(after.original.sha256).not.toBe(before.original.sha256)
  expect(after.rewritten.sha256).not.toBe(before.rewritten.sha256)
  expect(after.original.byteLength).toBeGreaterThan(before.original.byteLength)
})
