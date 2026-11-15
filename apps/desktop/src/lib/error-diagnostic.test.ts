import { expect, test, vi } from 'vitest'

import { errorDiagnostic, errorField } from './error-diagnostic'

test('preserves legacy strings and Error messages', () => {
  expect(errorDiagnostic('text', 'fallback')).toEqual({ text: 'text', codes: '' })
  expect(errorDiagnostic(new Error('error'), 'fallback')).toEqual({ text: 'error', codes: '' })
})

test('ignores nonobjects, inherited fields, empty text, and unsafe code payloads', () => {
  for (const value of [null, undefined, 0, true, () => {}, Object.create({ message: 'inherited' })]) {
    expect(errorDiagnostic(value, 'fallback')).toEqual({ text: 'fallback', codes: '' })
    expect(errorField(value, 'message')).toBeUndefined()
  }

  expect(errorDiagnostic({ detail: ' ', message: ' useful ', code: 'secret' }, 'fallback')).toEqual({
    text: 'useful',
    codes: ''
  })
  expect(errorDiagnostic({ code: { token: 'secret' } }, 'fallback').codes).toBe('')
  expect(errorDiagnostic({ code: `storage_${'x'.repeat(1000)}` }, 'fallback').codes).toBe('')
  expect(errorDiagnostic({ code: 42 }, 'fallback').codes).toBe('')
})

test.each([
  { depth: 6, expected: 'boundary detail' },
  { depth: 7, expected: 'fallback' }
])('extracts detail through depth 6 but not beyond it (depth $depth)', ({ depth, expected }) => {
  let value: unknown = 'boundary detail'

  // The root is depth 0; each detail edge adds one to the string's depth.
  for (let level = 0; level < depth; level++) {
    value = { detail: value }
  }

  expect(errorDiagnostic(value, 'fallback')).toEqual({ text: expected, codes: '' })
})

test('visits a cycle only once', () => {
  const cycle: { detail?: unknown; code: string } = { code: 'storage_locked' }
  cycle.detail = cycle
  expect(errorDiagnostic(cycle, 'fallback').codes).toBe('{"code":"storage_locked"}')
})

test('does not invoke accessors and tolerates descriptor lookup refusal', () => {
  const getter = vi.fn(() => {
    throw new Error('unsafe')
  })

  const object = Object.defineProperty({}, 'message', { get: getter })
  expect(errorDiagnostic(object, 'fallback').text).toBe('fallback')
  expect(getter).not.toHaveBeenCalled()

  const proxy = new Proxy(
    {},
    {
      getOwnPropertyDescriptor() {
        throw new Error('denied')
      }
    }
  )

  expect(errorDiagnostic(proxy, 'fallback').text).toBe('fallback')
})

test('bounds breadth independently of depth while retaining earlier codes', () => {
  let reads = 0

  const tree = (depth: number): object =>
    new Proxy(
      {},
      {
        getOwnPropertyDescriptor(_target, key) {
          reads++

          return { configurable: true, value: key === 'code' ? 'storage_locked' : depth ? tree(depth - 1) : undefined }
        }
      }
    )

  const result = errorDiagnostic(tree(5), 'fallback')
  expect(reads).toBe(32 * 5)
  expect(result.codes.split(' ')).toHaveLength(32)
  expect(result.text).toBe('fallback')
})
