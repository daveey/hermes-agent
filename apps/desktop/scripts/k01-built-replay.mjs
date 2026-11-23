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

const [companionPath, outArg] = process.argv.slice(2)
if (!companionPath || !outArg) throw new Error('usage: node scripts/k01-built-replay.mjs companion.js outdir')
const out = path.resolve(outArg), app = path.resolve('.')
await fs.mkdir(out, {recursive:true})
const entry = path.join(out, 'entry.ts')
await fs.writeFile(entry, `export {host} from ${JSON.stringify(path.join(app,'src/sdk/index.ts'))};export {loadRuntimePlugin,unloadRuntimePlugin} from ${JSON.stringify(path.join(app,'src/contrib/runtime-loader.ts'))};`)
const config = await loadConfigFromFile({command:'build',mode:'production'},path.join(app,'vite.config.ts'))
await build({ configFile:false, root:app, base:'./', logLevel:'warn', resolve:config.config.resolve, css:config.config.css,
  define:{'import.meta.env.DEV':'false'}, build:{...config.config.build,outDir:path.join(out,'dist'),emptyOutDir:true,manifest:'renderer-manifest.json',minify:false,rolldownOptions:{...config.config.build.rolldownOptions,input:entry,preserveEntrySignatures:'strict'}} })
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
const manifest=JSON.parse(await fs.readFile(path.join(out,'dist/renderer-manifest.json'),'utf8'))
const root=Object.values(manifest).find(v => v.isEntry)
const product=await import(pathToFileURL(path.join(out,'dist',root.file)).href)
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
st.refresh=st.warm
assert.equal(product.host.diagnostics.readSessionTasksLoadedIdentity().reason,'SURFACE_CHANGED')
product.unloadRuntimePlugin('session-tasks')
assert.equal(product.host.diagnostics.readSessionTasksLoadedIdentity().status,'NOT_READY')
await fs.writeFile(path.join(out,'receipt.json'),JSON.stringify({identity,fetches,builtRoot:root.file,checks:['whole-companion MATCH','independent on-disk asset sha+length','wrong-name rejected','unload invalidated']},null,2))
console.log(JSON.stringify({status:'PASS',checks:4,dependencies:identity.dependencies.length,assets:identity.dependencies[0].servedAssets.length,fetches:fetches.length}))
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
