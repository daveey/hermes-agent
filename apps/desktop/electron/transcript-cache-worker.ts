/**
 * [daveey] Worker-thread entry that owns the transcript cache's SQLite file, so its
 * synchronous I/O never runs on Electron's main thread (see transcript-cache-client.ts).
 * Bundled on its own as dist/electron-transcript-cache-worker.js; no Electron imports.
 */
import { parentPort, workerData } from 'node:worker_threads'

import { TranscriptCacheStore } from './transcript-cache'
import { answerStoreRequest, type StoreRequest } from './transcript-cache-client'

if (!parentPort) {
  throw new Error('transcript-cache-worker must run as a worker thread')
}

const store = new TranscriptCacheStore(String((workerData as { file?: unknown })?.file ?? ''))
const port = parentPort

port.on('message', (request: StoreRequest) => {
  port.postMessage(answerStoreRequest(store, request))
})
