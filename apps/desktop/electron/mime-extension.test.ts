import { describe, expect, it } from 'vitest'

import { extensionForMimeType } from './mime-extension'

describe('extensionForMimeType', () => {
  it('maps image MIME types (parameters and case ignored) to extensions', () => {
    expect(extensionForMimeType('image/png')).toBe('.png')
    expect(extensionForMimeType('IMAGE/JPEG; charset=binary')).toBe('.jpg')
    expect(extensionForMimeType('image/gif')).toBe('.gif')
    expect(extensionForMimeType('image/webp')).toBe('.webp')
    expect(extensionForMimeType('image/bmp')).toBe('.bmp')
    expect(extensionForMimeType('image/svg+xml')).toBe('.svg')
  })

  it('returns an empty string for anything else', () => {
    expect(extensionForMimeType('text/plain')).toBe('')
    expect(extensionForMimeType(undefined)).toBe('')
  })
})
