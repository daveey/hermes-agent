// Feed bytes (stdin) through the Desktop's REAL READY-sentinel parser.
//
// Imports apps/desktop/electron/backend-ready.ts itself (Node >= 22.18 strips the
// types natively) and drives `waitForDashboardPort` with a child-shaped emitter
// whose stdout emits exactly the bytes the Python test captured from the backend's
// stdout. Prints {"port": N} or {"error": "..."}. The Python side therefore never
// re-implements or regexes the TS contract: if the parser changes, this follows.
import { EventEmitter } from 'node:events'
import { registerHooks } from 'node:module'
import { extname } from 'node:path'
import { pathToFileURL } from 'node:url'

// Electron's bundler resolves extensionless TS imports; native Node does not.
// Keep executing the real dependency graph rather than copying the parser.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('.') && !extname(specifier)) {
      try {
        return nextResolve(specifier, context)
      } catch (error) {
        if (error.code !== 'ERR_MODULE_NOT_FOUND') { throw error }
        return nextResolve(`${specifier}.ts`, context)
      }
    }
    return nextResolve(specifier, context)
  },
})

const [parserPath, timeoutMs] = process.argv.slice(2)
const { waitForDashboardPort } = await import(pathToFileURL(parserPath).href)

const chunks = []
for await (const chunk of process.stdin) {
  chunks.push(chunk)
}

const child = new EventEmitter()
child.stdout = new EventEmitter()
const pending = waitForDashboardPort(child, Number(timeoutMs) || 5000)
child.stdout.emit('data', Buffer.concat(chunks))
// Nothing more will arrive: the captured stream ended here.
child.emit('exit', 0, null)

try {
  console.log(JSON.stringify({ port: await pending }))
} catch (err) {
  console.log(JSON.stringify({ error: String(err && err.message ? err.message : err) }))
}
