import { gzipSync } from 'node:zlib'

import { describe, expect, it } from 'vitest'

import { decodeResponseBody, displayTranscriptHeaders, isDisplayTranscriptRead } from './display-transcript'

// [daveey] Transcript pages ask the backend for the slim, gzipped display form; every other request is untouched.

describe('display transcript reads', () => {
  it('marks only GETs of a session messages page', () => {
    expect(
      isDisplayTranscriptRead('GET', 'http://127.0.0.1:9147/api/sessions/s1/messages?limit=120&order=latest')
    ).toBe(true)
    expect(isDisplayTranscriptRead(undefined, 'http://127.0.0.1:9147/api/sessions/s1/messages')).toBe(true)
    expect(isDisplayTranscriptRead('POST', 'http://127.0.0.1:9147/api/sessions/s1/messages')).toBe(false)
    expect(isDisplayTranscriptRead('GET', 'http://127.0.0.1:9147/api/sessions/s1/messages/around?row=3')).toBe(false)
    expect(isDisplayTranscriptRead('GET', 'http://127.0.0.1:9147/api/sessions/s1')).toBe(false)
    expect(isDisplayTranscriptRead('GET', 'not a url')).toBe(false)
  })

  it('asks for the display form and gzip only on those reads', () => {
    expect(displayTranscriptHeaders('GET', 'http://h/api/sessions/s1/messages')).toEqual({
      'Accept-Encoding': 'gzip',
      'X-Hermes-Display-Transcript': '1'
    })
    expect(displayTranscriptHeaders('GET', 'http://h/api/sessions')).toEqual({})
  })

  it('matches one exact gzip coding, not a substring or a stacked encoding', () => {
    const body = Buffer.from('{"ok":true}')
    expect(decodeResponseBody(gzipSync(body), ' GZip ')).toBe(body.toString())
    expect(decodeResponseBody(body, 'not-gzip')).toBe(body.toString())
    expect(decodeResponseBody(body, 'gzip, br')).toBe(body.toString())
  })

  it('bounds gunzip output even for ordinary cold gzip responses', () => {
    const bomb = gzipSync(Buffer.alloc(16 * 1024 * 1024 + 1, 65))
    expect(() => decodeResponseBody(bomb, 'gzip')).toThrow()
  })

  it('decodes a gzipped page and passes an uncompressed one through (an older backend)', () => {
    const json = JSON.stringify({ messages: [{ content: 'héllo', role: 'assistant' }] })

    expect(decodeResponseBody(gzipSync(Buffer.from(json)), 'gzip')).toBe(json)
    expect(decodeResponseBody(gzipSync(Buffer.from(json)), ['gzip'])).toBe(json)
    expect(decodeResponseBody(Buffer.from(json), undefined)).toBe(json)
  })
})
