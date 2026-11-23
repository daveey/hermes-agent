/**
 * [daveey] Display transcript reads: slim + gzip for GET /api/sessions/<id>/messages.
 *
 * A 120-message page of a long hub session was 1.4 MB, mostly provider replay data the Desktop never shows
 * (`reasoning_details` signatures, and `reasoning_content` repeating `reasoning`). Over the SSH tunnel to the hub
 * that took 1.5-2.5 s per open. The Desktop marks its transcript reads with a header; a backend that knows it
 * drops those fields and gzips the page (168 KB for the same page). An older backend ignores the header and
 * answers the full, uncompressed page, which still decodes here.
 */
import { gunzipSync } from 'node:zlib'

export const DISPLAY_TRANSCRIPT_HEADER = 'X-Hermes-Display-Transcript'

const MESSAGES_PATH = /^\/api\/sessions\/[^/]+\/messages$/

export function isDisplayTranscriptRead(method: string | undefined, url: string): boolean {
  if ((method || 'GET').toUpperCase() !== 'GET') {
    return false
  }

  try {
    return MESSAGES_PATH.test(new URL(url).pathname)
  } catch {
    return false
  }
}

export function displayTranscriptHeaders(method: string | undefined, url: string): Record<string, string> {
  return isDisplayTranscriptRead(method, url) ? { [DISPLAY_TRANSCRIPT_HEADER]: '1', 'Accept-Encoding': 'gzip' } : {}
}

/** The response body as text, gunzipped when the server compressed it. */
export function decodeResponseBody(body: Buffer, contentEncoding: string | string[] | undefined): string {
  const encoding = String(Array.isArray(contentEncoding) ? contentEncoding[0] : contentEncoding || '').trim().toLowerCase()

  return (encoding === 'gzip' ? gunzipSync(body, { maxOutputLength: 16 * 1024 * 1024 }) : body).toString('utf8')
}
