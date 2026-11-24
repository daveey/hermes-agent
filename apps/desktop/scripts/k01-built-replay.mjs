// Run from apps/desktop: node scripts/k01-built-replay.mjs <companion> <outdir>
// Builds real production loader+SDK, imports built bytes in an owned jsdom realm,
// and loads the WHOLE companion. Only DOM and filesystem transport are fixtures.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'
import { build, loadConfigFromFile } from 'vite'
import { JSDOM } from 'jsdom'
import { EventEmitter } from 'node:events'
import { build as bundle } from 'esbuild'

const [companionPath, outArg] = process.argv.slice(2)
if (!companionPath || !outArg) throw new Error('usage: node scripts/k01-built-replay.mjs companion.js outdir')
const out = path.resolve(outArg), app = path.resolve('.')
await fs.mkdir(out, {recursive:true})
const entry = path.join(out, 'entry.ts')
await fs.writeFile(entry, `export {host} from ${JSON.stringify(path.join(app,'src/sdk/index.ts'))};export {loadRuntimePlugin,unloadRuntimePlugin} from ${JSON.stringify(path.join(app,'src/contrib/runtime-loader.ts'))};`)
const config = await loadConfigFromFile({command:'build',mode:'production'},path.join(app,'vite.config.ts'))
await build({ configFile:false, root:app, base:'./', logLevel:'warn', resolve:config.config.resolve, css:config.config.css,
  define:{'import.meta.env.DEV':'false'}, build:{...config.config.build,outDir:path.join(out,'dist'),emptyOutDir:true,manifest:'renderer-manifest.json',minify:false,rolldownOptions:{...config.config.build.rolldownOptions,input:{product:entry, 'session-tasks-diagnostics':path.join(app,'src/session-tasks-diagnostics.ts')},preserveEntrySignatures:'strict'}} })
const timers = new Set()
const nativeTimeout = globalThis.setTimeout, nativeInterval = globalThis.setInterval
for (const [name, original] of [['setTimeout',nativeTimeout],['setInterval',nativeInterval]]) {
  globalThis[name] = (...args) => { const id=original(...args); timers.add(id); return id }
}
const ports = []
const NativeMessageChannel = globalThis.MessageChannel
if (NativeMessageChannel) globalThis.MessageChannel = class extends NativeMessageChannel {
  constructor() { super(); ports.push(this.port1,this.port2) }
}
const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', {url:'https://owned-fixture.invalid/'})
for (const key of ['window','document','navigator','location','localStorage','sessionStorage','HTMLElement','Element','Node','MutationObserver','Event','CustomEvent','DOMParser']) {
  Object.defineProperty(globalThis,key,{value:dom.window[key],configurable:true,writable:true})
}
window.matchMedia = () => ({matches:false,addEventListener(){},removeEventListener(){}})
globalThis.requestAnimationFrame = fn => setTimeout(fn,0)
globalThis.cancelAnimationFrame = clearTimeout
const NativeBlob = globalThis.Blob
// Same semantic transport as runtime-loader.test-helpers: Node cannot import blob:.
globalThis.Blob = class { constructor(parts) {this.parts=parts} }
URL.createObjectURL = blob => `data:text/javascript;base64,${Buffer.from(blob.parts.join('')).toString('base64')}#${crypto.randomUUID()}`
URL.revokeObjectURL = () => {}
const fetches=[]
globalThis.fetch = async url => {
  if (!String(url).startsWith('file:')) throw new Error(`unexpected external IO: ${url}`)
  fetches.push(String(url))
  return new Response(await fs.readFile(fileURLToPath(url)),{status:200})
}
try {
const manifest=JSON.parse(await fs.readFile(path.join(out,'dist/renderer-manifest.json'),'utf8'))
const root=Object.values(manifest).find(v => v.isEntry && v.file !== manifest['src/session-tasks-diagnostics.ts'].file)
const product=await import(pathToFileURL(path.join(out,'dist',root.file)).href)
const diagnostic=await import(pathToFileURL(path.join(out,'dist',manifest['src/session-tasks-diagnostics.ts'].file)).href)
assert.deepEqual(Object.keys(diagnostic),['readSessionTasksLoadedIdentityJson'])
const trustedRead=()=>JSON.parse(diagnostic.readSessionTasksLoadedIdentityJson())
const source=await fs.readFile(companionPath,'utf8')
assert.equal(await product.loadRuntimePlugin(source,'session-tasks',{file:'/fixture/plugins/session-tasks/plugin.js'}),'session-tasks')
const identity=product.host.diagnostics.readSessionTasksLoadedIdentity()
assert.equal(identity.status,'MATCH',JSON.stringify(identity))
assert.equal(identity.original.sha256,createHash('sha256').update(source).digest('hex'))
for (const dependency of identity.dependencies) {
  assert.ok(dependency.servedAssets.length>1)
  for (const asset of dependency.servedAssets) {
    const bytes=await fs.readFile(fileURLToPath(asset.url))
    assert.equal(asset.sha256,createHash('sha256').update(bytes).digest('hex'))
    assert.equal(asset.byteLength,bytes.length)
  }
}
const st=window.__sessionTasks
// Only Electron transport/window IO is replaced. Execute the actual core main
// route, actual static reader and whole actual companion in this SAME realm.
await bundle({entryPoints:[path.join(app,'electron/session-tasks-diagnostics.ts')],bundle:true,platform:'node',format:'esm',outfile:path.join(out,'main-route.mjs'),plugins:[{
  name:'owned-electron-transport',setup(builder){
    builder.onResolve({filter:/^electron$/},()=>({path:'electron',namespace:'owned'}))
    builder.onLoad({filter:/.*/,namespace:'owned'},()=>({contents:'export const BrowserWindow={fromId:id=>globalThis.__ownedWindows.get(id)}',loader:'js'}))
  }
}]})
const contents = new EventEmitter()
const frame = {routingId:7,processId:8}
contents.mainFrame=frame; contents.getOSProcessId=()=>process.pid
contents.isDestroyed=()=>false
contents.getURL=()=>pathToFileURL(path.join(out,'dist/index.html')).href
const trace=[]
contents.executeJavaScript=async expression=>{
  trace.push({kind:'dispatch',expression})
  return await eval(expression)
}
globalThis.__ownedWindows=new Map([[1,{webContents:contents,isDestroyed:()=>false}]])
const endpoint=await import(pathToFileURL(path.join(out,'main-route.mjs')).href)
const bytePin = bytes => ({sha256:createHash('sha256').update(bytes).digest('hex'),byteLength:bytes.length})
const assets=[]
for(const file of new Set(Object.values(manifest).map(item=>item.file))) {
  if(file.endsWith('.js'))assets.push({path:file,...bytePin(await fs.readFile(path.join(out,'dist',file)))})
}
const inventory={schemaVersion:1,manifest:bytePin(await fs.readFile(path.join(out,'dist/renderer-manifest.json'))),entryAsset:manifest['src/session-tasks-diagnostics.ts'].file,assets}
const admitted=await endpoint.readSessionTasksLoadedIdentityForWindow(1,inventory)
assert.equal(JSON.parse(admitted).status,'MATCH')
assert.equal(await endpoint.evaluateSessionTasksWindowOperation(1,inventory,admitted,'window.__sessionTasks.warm({})'),false)
trace.push({kind:'actual-companion-positive',operation:'warm({})',value:false})
product.unloadRuntimePlugin('session-tasks')
await product.loadRuntimePlugin(source,'session-tasks',{file:'/fixture/plugins/session-tasks/plugin.js'})
globalThis.__ownedOperationCount=0
await assert.rejects(endpoint.evaluateSessionTasksWindowOperation(1,inventory,admitted,'(globalThis.__ownedOperationCount++, window.__sessionTasks.warm({}))'),/OPERATION_IDENTITY_CHANGED/)
assert.equal(globalThis.__ownedOperationCount,0)
trace.push({kind:'old-generation-refused',ordinaryOperations:globalThis.__ownedOperationCount})
// Continue the original surface tests against the new authentic activation.
const reloaded=window.__sessionTasks
assert.equal(JSON.parse(await endpoint.readSessionTasksLoadedIdentityForWindow(1,inventory)).status,'MATCH')
await fs.writeFile(path.join(out,'main-route-trace.json'),JSON.stringify(trace,null,2))
assert.notEqual(reloaded,st)
const currentIdentity=trustedReadPlaceholder
assert.deepEqual(trustedReadPlaceholder,identity)
assert.equal(await st.warm({}),false) // Genuine whole-companion operation in the diagnostic realm.
assert.equal(trustedRead().status,'MATCH')
const honest=product.host.diagnostics.readSessionTasksLoadedIdentity
st.refresh=st.warm
assert.equal(honest().reason,'SURFACE_CHANGED')
product.host.diagnostics.readSessionTasksLoadedIdentity=()=>identity
const reimport=await import(pathToFileURL(path.join(out,'dist',root.file)).href)
assert.equal(reimport.host.diagnostics.readSessionTasksLoadedIdentity().status,'MATCH') // Old consumer's reproduced false authority.
assert.deepEqual(trustedRead(),{...honest(),reason:'SDK_CHANGED'})
product.host.diagnostics.readSessionTasksLoadedIdentity=honest
assert.equal(trustedRead().reason,'SURFACE_CHANGED')
product.unloadRuntimePlugin('session-tasks')
assert.equal(trustedRead().status,'NOT_READY')
await fs.writeFile(path.join(out,'receipt.json'),JSON.stringify({identity,fetches,builtRoot:root.file,diagnosticRoot:manifest['src/session-tasks-diagnostics.ts'].file,checks:['whole-companion MATCH','independent on-disk asset sha+length','same-realm genuine operation','wrong-name rejected','genuine pinned ESM cached MATCH refused by private facade','restored SDK retains surface refusal','unload invalidated','read-only facade export shape']},null,2))
console.log(JSON.stringify({status:'PASS',checks:8,dependencies:identity.dependencies.length,assets:identity.dependencies[0].servedAssets.length,fetches:fetches.length}))
} finally {
globalThis.Blob=NativeBlob
dom.window.close()
for (const id of timers) { clearTimeout(id); clearInterval(id) }
globalThis.setTimeout = nativeTimeout; globalThis.setInterval = nativeInterval
for (const port of ports) port.close()
globalThis.MessageChannel = NativeMessageChannel
// Libraries imported before transport setup may have created a scheduler port.
// This is a dedicated fixture process; close its MessagePorts after disposal.
for (const handle of process._getActiveHandles()) if (handle.constructor.name === 'MessagePort') handle.close()
await new Promise(resolve => setImmediate(resolve))
console.log('fixture remaining resources',process.getActiveResourcesInfo())
}
