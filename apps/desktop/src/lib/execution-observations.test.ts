import { describe, expect, it } from 'vitest'

import fixture from '../../../../schemas/fixtures/execution-observations-v1.json'

import {
  executionIdentityKey,
  projectExecutionObservation,
  selectExecutionObservation,
  summarizeExecutionObservations
} from './execution-observations'

// Verdicts live only in the shared fixture, which tests/hermes_cli/test_execution_observation_golden.py also loads.
type Patch = { set?: Record<string, unknown>; delete?: string[] }
type Item = Patch & { raw?: unknown }
interface Case extends Patch {
  id: string
  now?: unknown
  source_class?: string
  expected: Record<string, unknown>
  expected_absent?: string[]
  full_output?: boolean
  expected_identity?: unknown
  identity?: unknown
  items?: Item[]
  repeat_count?: number
  outstanding?: number
  values_raw?: unknown
}

const clone = <T>(v: T): T => structuredClone(v)
const isObject = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v)

function apply(value: Record<string, unknown>, patch: Patch) {
  for (const [path, replacement] of Object.entries(patch.set ?? {})) {
    const keys = path.split('.')
    let parent = value

    for (const key of keys.slice(0, -1)) {parent = parent[key] as Record<string, unknown>}

    parent[keys.at(-1)!] = clone(replacement)
  }

  for (const path of patch.delete ?? []) {
    const keys = path.split('.')
    let parent = value

    for (const key of keys.slice(0, -1)) {parent = parent[key] as Record<string, unknown>}

    delete parent[keys.at(-1)!]
  }

  return value
}

const caseInput = (patch: Patch) => apply(clone(fixture.base) as Record<string, unknown>, patch)
const itemInput = (item: Item) => ('raw' in item ? clone(item.raw) : caseInput(item))

it.each([false, true])('expires cached nested reason independently (missing identity %s)', missingIdentity => {
  const raw = caseInput({})

  if (missingIdentity) {delete (raw.identity as Record<string, unknown>).run_id}
  raw.non_running_reason = {
    code: 'usage_accounting_pending', family: 'accounting', actor: 'agent', disposition: 'blocked',
    summary: 'Run usage accounting is unsettled', next_action: 'Recover the original source accounting evidence, then retry normal admission',
    freshness: 'current', observed_at: 990, valid_until: 1010,
    evidence_ref: 'dispatch:original', source_revision: 'original'
  }
  const before = clone(raw)
  const first = projectExecutionObservation(raw, 1005, 'sidebar')
  expect((first.non_running_reason as Record<string, unknown>).freshness).toBe('current')
  const stale = projectExecutionObservation(first, 1010, 'sidebar').non_running_reason as Record<string, unknown>
  expect(stale).toMatchObject({ freshness: 'unknown', disposition: 'unknown', observed_at: 990, unknown_reason: 'dispatcher_evidence_stale' })
  expect(raw).toEqual(before)
})

/** Recursive subset: bool is distinct from number, missing is not null. */
function subset(actual: unknown, expected: unknown): boolean {
  if (isObject(expected)) {
    return isObject(actual) && Object.entries(expected).every(([k, v]) => Object.hasOwn(actual, k) && subset(actual[k], v))
  }

  if (Array.isArray(expected)) {
    return Array.isArray(actual) && actual.length === expected.length && expected.every((e, i) => subset(actual[i], e))
  }

  return Object.is(actual, expected) || (typeof actual === 'number' && actual === expected)
}

function present(value: unknown, path: string): boolean {
  for (const key of path.split('.')) {
    if (Array.isArray(value) && /^\d+$/.test(key)) {
      if (Number(key) >= value.length) {return false}
      value = value[Number(key)]

      continue
    }

    if (!isObject(value) || !Object.hasOwn(value, key)) {return false}

    value = value[key]
  }

  return true
}

function check(actual: Record<string, unknown>, test: Case) {
  if (test.full_output) {expect(actual).toEqual(test.expected)}
  const observations = (actual.observations ?? []) as { state: string }[]
  const view = { ...actual, states: observations.map(o => o.state) }

  expect(subset(view, test.expected), JSON.stringify({ expected: test.expected, actual: view })).toBe(true)

  for (const path of test.expected_absent ?? []) {expect(present(actual, path), path).toBe(false)}
}

// Passed through verbatim: an invalid class must be rejected by the projector, not normalised by the test.
const cls = (test: { source_class?: string }) => (test.source_class ?? 'sidebar') as 'sidebar' | 'imported_owner'
const cases = fixture.cases as unknown as Case[]
const selectCases = fixture.select_cases as unknown as Case[]
const summarizeCases = fixture.summarize_cases as unknown as Case[]

describe('shared execution observation semantic contract', () => {
  it('loads the shared fixture sections', () => {
    const ids = [...cases, ...selectCases, ...summarizeCases].map(c => c.id)

    expect(new Set(ids).size).toBe(ids.length)
    expect(cases.length).toBeGreaterThan(150)
    expect(selectCases.length).toBeGreaterThan(0)
    expect(summarizeCases.length).toBeGreaterThan(0)
  })

  for (const test of cases) {
    it(test.id, () => {
      const value = caseInput(test)
      const before = clone(value)
      const result = projectExecutionObservation(value, (test.now ?? 1001) as number, cls(test), test.expected_identity)

      check(result, test)
      expect(value).toEqual(before)

      if ('delivery_receipt' in value && test.expected.state === 'done') {
        expect(result.delivery_receipt).toEqual(value.delivery_receipt)
      }
    })
  }

  for (const test of selectCases) {
    it(`select: ${test.id}`, () => {
      const items = 'values_raw' in test ? clone(test.values_raw) : test.items!.map(itemInput)
      const values = test.repeat_count ? Array.from({ length: test.repeat_count }, () => (items as unknown[])[0]) : items
      const before = clone(values)
      const identity = test.identity ?? fixture.base.identity
      const result = selectExecutionObservation(values as unknown[], clone(identity), test.now as number, cls(test))

      check(result, test)
      expect(values).toEqual(before)

      if (Array.isArray(values) && !test.values_raw) {
        const again = selectExecutionObservation([...values].reverse(), clone(identity), test.now as number, cls(test))

        expect(again).toEqual(result)
      }

      if (test.outstanding !== undefined) {
        for (const observation of [result, projectExecutionObservation(result, test.now as number),
          selectExecutionObservation([result], identity, test.now as number)]) {
          expect(observation.retained_human_requests).toEqual(result.retained_human_requests)
          expect(observation.historical_delivery_receipts).toEqual(result.historical_delivery_receipts)

          for (const request of observation.retained_human_requests as Record<string, unknown>[]) {
            expect(request).not.toHaveProperty('resolved_check')
            expect(request).not.toHaveProperty('resolved_check_every')
          }

          const summary = summarizeExecutionObservations([observation], test.now as number)
          expect(summary.outstanding_human_requests).toBe(test.outstanding)
          expect(summary.counts.unknown).toBe(1)
          expect(summary.counts.done).toBe(0)
          expect(summarizeExecutionObservations(summary.observations, test.now as number)).toEqual(summary)
          const doubled = summarizeExecutionObservations([observation, observation], test.now as number)
          expect(doubled.outstanding_human_requests).toBe(2 * test.outstanding)
          expect(doubled.counts.unknown).toBe(2)
        }
      }
    })
  }

  for (const test of summarizeCases) {
    it(`summarize: ${test.id}`, () => {
      const values = 'values_raw' in test ? clone(test.values_raw) : test.items!.map(itemInput)
      const before = clone(values)

      check(summarizeExecutionObservations(values as unknown[], test.now as number) as unknown as Record<string, unknown>, test)
      expect(values).toEqual(before)
    })
  }

  it.each(['gateway', 'profile', 'board', 'run_id', 'work_id'])('does not alias %s', field => {
    const id = { ...fixture.base.identity, [field]: field === 'run_id' ? 8 : 'different' }

    expect(executionIdentityKey(id)).not.toEqual(executionIdentityKey(fixture.base.identity))
    expect(projectExecutionObservation(fixture.base, 1001, 'sidebar', id).state).toBe('unknown')
  })

  it('treats 7 and 7.0 as one identity but "7" as another', () => {
    expect(executionIdentityKey({ ...fixture.base.identity, run_id: 7.0 })).toBe(executionIdentityKey(fixture.base.identity))
    expect(executionIdentityKey({ ...fixture.base.identity, run_id: '7' })).not.toBe(executionIdentityKey(fixture.base.identity))
  })

  it.each([NaN, Infinity, -Infinity, true, '1000'])('rejects invalid clock %s', observed_at => {
    expect(projectExecutionObservation({ ...fixture.base, observed_at }, 1001).state).toBe('unknown')
  })

  it('returns a deep copy: mutating output never touches the input', () => {
    const value = { ...clone(fixture.base), human_request: { action: 'a', nested: { deep: [1] } } } as Record<string, any>
    const before = clone(value)
    const result = projectExecutionObservation(value, 1001) as Record<string, any>

    result.identity.work_id = 'mutated'
    result.human_request.nested.deep.push(2)
    result.owner.gateway = 'mutated'
    expect(value).toEqual(before)
  })

  it('ignores inherited properties for every lookup', () => {
    const inherited = Object.create({ source_class: 'imported_owner', successful: true })

    Object.assign(inherited, { observed_at: 900, valid_until: 1600 })
    // Inherited source_class is not an own property, so the envelope's class applies.
    expect(projectExecutionObservation({ ...clone(fixture.base), required_sources: [inherited] }, 1001).state).toBe('running')

    const polluted = Object.create({ version: 1, state: 'running' })

    expect(projectExecutionObservation(polluted, 1001).state).toBe('unknown')
  })

  it('copies an own __proto__ key as data without changing a prototype', () => {
    const value = JSON.parse(JSON.stringify(fixture.base).replace('"version":1', '"version":1,"__proto__":{"state":"failed","x":1}'))
    const result = projectExecutionObservation(value, 1001)

    expect(result.state).toBe('running')
    expect(Object.getPrototypeOf(result)).toBe(Object.prototype)
    expect((result as Record<string, unknown>).x).toBeUndefined()
  })

  it('does not throw on cyclic or non-JSON input', () => {
    const cyclic = clone(fixture.base) as Record<string, unknown>

    cyclic.self = cyclic
    cyclic.fn = () => 1
    expect(projectExecutionObservation(cyclic, 1001).state).toBe('running')
  })

  it('retains partial uncertainty next to fresh running and rejects empty certainty', () => {
    const result = summarizeExecutionObservations([fixture.base, { ...fixture.base, state: 'unknown' }], 1001)

    expect(result.counts.running).toBe(1)
    expect(result.counts.unknown).toBe(1)
    expect(result.uncertain).toBe(true)
    expect(summarizeExecutionObservations([], 1001).uncertain).toBe(true)
  })
})
