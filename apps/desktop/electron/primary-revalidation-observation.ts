import { AsyncLocalStorage } from 'node:async_hooks'
import { createHmac, randomBytes } from 'node:crypto'

export type PrimaryRevalidationTrigger = 'prewarm' | 'registry' | 'renderer' | 'direct'
export type PrimaryRevalidationEvent = 'registry_start' | 'registry_end' | 'registry_error' | 'matched_primary' | 'registry_match' | 'invocation' | 'no_connection'
  | 'round_start' | 'round_end' | 'round_error' | 'connection_rejected' | 'stale_connection' | 'not_remote'
  | 'probe_start' | 'probe_success' | 'probe_failure' | 'retirement_requested' | 'reset'
  | 'ssh_capture' | 'drain_start' | 'drain_end' | 'teardown_start' | 'teardown_end' | 'teardown_error'
  | 'missing_scope' | 'stale_scope' | 'stale_scope_after_drain'
export interface PrimaryRevalidationFields {
  invocation?: number
  round?: number
  generation?: number
  state?: number
  scope?: string
  port?: number
  trigger?: PrimaryRevalidationTrigger
  joined?: boolean
  matched?: boolean
  force_probe?: boolean
  passive?: boolean
  skipped?: boolean
  failures?: number
  should_reset?: boolean
  rebuilt?: boolean
  duration_ms?: number
}
export type PrimaryRevalidationObserver = (event: PrimaryRevalidationEvent, fields?: PrimaryRevalidationFields) => void

const identities = new WeakMap<object, number>()
const scopeKey = randomBytes(32)
export const revalidationScopeIdentity = (scope: string): string => createHmac('sha256', scopeKey).update(scope).digest('hex')
let nextIdentity = 0

/** Process-local opaque identity, never scope names, remote hosts or credentials. */
export function revalidationIdentity(value: object): number {
  let identity = identities.get(value)

  if (identity === undefined) { identity = ++nextIdentity; identities.set(value, identity) }

  return identity
}

const invocations = new AsyncLocalStorage<{ invocation: number; trigger: PrimaryRevalidationTrigger }>()
const rounds = new AsyncLocalStorage<{ round: number; generation: number }>()
export const currentRevalidationRound = () => ({ ...invocations.getStore(), ...rounds.getStore() })
export const withRevalidationInvocation = <T>(context: { invocation: number; trigger: PrimaryRevalidationTrigger }, run: () => T): T => invocations.run(context, run)
export const withRevalidationRound = <T>(context: { round: number; generation: number }, run: () => T): T => rounds.run(context, run)

export function revalidationPort(baseUrl?: string | null): number {
  try { return Number(new URL(baseUrl || '').port) || 0 } catch { return 0 }
}
