import { createHash } from 'node:crypto'
import { createServer, type Server } from 'node:http'

import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { identifyBytes, readServedAssets } from './loaded-assets'

let server: Server, base:string
let files:Record<string,string>
let redirect=false
beforeEach(async () => {
  redirect=false
  files={ '/renderer-manifest.json':JSON.stringify({entry:{file:'assets/root.js',imports:['react']},react:{file:'assets/react.js'},lazy:{file:'assets/lazy.js'}}), '/assets/root.js':'import "./react.js"; // SDK source', '/assets/react.js':'export const react="reviewed dependency"' }
  server=createServer((req,res) => {
    if(redirect && req.url==='/assets/root.js'){res.writeHead(302,{Location:'/assets/react.js'});res.end();

return}

    const body=files[req.url!];res.writeHead(body === undefined ? 404 : 200);res.end(body)
  })
  await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve))
  base=`http://127.0.0.1:${(server.address() as {port:number}).port}`
  const nativeFetch=globalThis.fetch
  vi.spyOn(globalThis,'fetch').mockImplementation((url,init)=>{
    if(new URL(String(url)).origin!==base) {throw new Error('EXTERNAL_IO_FORBIDDEN')}

    return nativeFetch(url,init)
  })
})
afterEach(async()=>{vi.restoreAllMocks();server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()))})
it('hashes the actual served static module closure and manifest, not lazy/unloaded assets',async()=>{
  files['/assets/react.js']='\uFEFF'+files['/assets/react.js']
  files['/renderer-manifest.json']='\uFEFF'+files['/renderer-manifest.json']
  const assets=await readServedAssets(base+'/assets/root.js')
  expect(assets.length).toBe(3)

  for(const asset of assets){const text=files[new URL(asset.url).pathname];expect(asset).toMatchObject({sha256:createHash('sha256').update(text).digest('hex'),byteLength:Buffer.byteLength(text)})}
})
it('dependency and helper byte changes change the observed digest with identical module names',async()=>{
  const first=await readServedAssets(base+'/assets/root.js')
  files['/assets/react.js']+='\n// changed helper'
  const second=await readServedAssets(base+'/assets/root.js')
  expect(first.find(a=>a.url.endsWith('react.js'))!.sha256).not.toBe(second.find(a=>a.url.endsWith('react.js'))!.sha256)
})
it('exact UTF8 original bytes include unicode and newline differences',async()=>{
  expect(await identifyBytes('λ\r\n')).toEqual({sha256:createHash('sha256').update('λ\r\n').digest('hex'),byteLength:4})
  expect(await identifyBytes('λ\r\n')).not.toEqual(await identifyBytes('λ\n'))
})
it.each(['ftp://example.invalid/assets/root.js','https://example.invalid/runtime.ts'])('unbundled root %s fails closed',async root=>{await expect(readServedAssets(root)).rejects.toThrow('UNBUNDLED_SDK_ASSETS')})
it('missing manifest or chunk fails closed',async()=>{
  delete files['/renderer-manifest.json'];await expect(readServedAssets(base+'/assets/root.js')).rejects.toThrow('SERVED_ASSET_READ_FAILED')
  files['/renderer-manifest.json']=JSON.stringify({entry:{file:'assets/root.js'}});delete files['/assets/root.js']
  await expect(readServedAssets(base+'/assets/root.js')).rejects.toThrow('SERVED_ASSET_READ_FAILED')
})
it('redirected chunk bytes are not accepted under the original URL',async()=>{
  redirect=true
  await expect(readServedAssets(base+'/assets/root.js')).rejects.toThrow('SERVED_ASSET_READ_FAILED')
})
it('deadline abort is forwarded to actual HTTP IO (controlled already-aborted deadline)',async()=>{
  const timeout=vi.spyOn(AbortSignal,'timeout').mockReturnValue(AbortSignal.abort())
  await expect(readServedAssets(base+'/assets/root.js')).rejects.toThrow()
  expect(timeout).toHaveBeenCalledWith(5_000)
})
it('unknown root and missing graph entries are rejected',async()=>{
  await expect(readServedAssets(base+'/assets/missing.js')).rejects.toThrow('SDK_ASSET_NOT_IN_MANIFEST')
  files['/renderer-manifest.json']=JSON.stringify({entry:{file:'assets/root.js',imports:['missing']}})
  await expect(readServedAssets(base+'/assets/root.js')).rejects.toThrow('INVALID_SERVED_ASSET_GRAPH')
})
it.each(['../foreign.js','assets/../foreign.js','https://foreign.invalid/assets/x.js'])('unsafe manifest path %s fails closed',async file=>{
  files['/renderer-manifest.json']=JSON.stringify({entry:{file:'assets/root.js',imports:['react']},react:{file}})
  await expect(readServedAssets(base+'/assets/root.js')).rejects.toThrow('INVALID_SERVED_ASSET_GRAPH')
})
it('cyclic static import graphs terminate once per asset',async()=>{
  files['/renderer-manifest.json']=JSON.stringify({entry:{file:'assets/root.js',imports:['react']},react:{file:'assets/react.js',imports:['entry']}})
  expect((await readServedAssets(base+'/assets/root.js')).length).toBe(3)
})
it('oversized graph fails closed instead of issuing an unbounded crawl',async()=>{
  const graph:Record<string,object>={entry:{file:'assets/root.js',imports:['k0']}}

  for(let n=0;n<256;n++){graph[`k${n}`]={file:`assets/k${n}.js`,imports:n<255?[`k${n+1}`]:[]}}
  files['/renderer-manifest.json']=JSON.stringify(graph)
  await expect(readServedAssets(base+'/assets/root.js')).rejects.toThrow('SERVED_ASSET_GRAPH_LIMIT')
})
