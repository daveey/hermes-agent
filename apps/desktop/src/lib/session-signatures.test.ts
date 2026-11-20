import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/hermes'

import { sameCronSignature, sessionMessagesSignature } from './session-signatures'

const session = (id: string, title: string | null, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, title, ...extra }) as SessionInfo

describe('sameCronSignature', () => {
  it('is false when the lengths differ', () => {
    expect(sameCronSignature([session('a', 't')], [])).toBe(false)
  })

  it.each(['cwd', 'git_repo_root', 'git_branch'] as const)('compares %s including cleared metadata', field => {
    const populated = [session('a', 't', { [field]: 'workspace' })]
    const cleared = [session('a', 't', { [field]: null })]
    expect(sameCronSignature(populated, cleared)).toBe(false)
    expect(sameCronSignature(cleared, populated)).toBe(false)
    expect(sameCronSignature(cleared, [session('a', 't', { [field]: null })])).toBe(true)
    expect(sameCronSignature(populated, [session('a', 't', { [field]: 'workspace' })])).toBe(true)
  })

  it('is true when ids and titles match in order', () => {
    const a = [session('a', 'one'), session('b', 'two')]
    const b = [session('a', 'one'), session('b', 'two')]
    expect(sameCronSignature(a, b)).toBe(true)
  })

  it('is false when a title changed', () => {
    const a = [session('a', 'one')]
    const b = [session('a', 'renamed')]
    expect(sameCronSignature(a, b)).toBe(false)
  })

  it('is false when order differs', () => {
    const a = [session('a', 't'), session('b', 't')]
    const b = [session('b', 't'), session('a', 't')]
    expect(sameCronSignature(a, b)).toBe(false)
  })

  // A pin-only page must reach $sessions: session-pin-sync treats the row as
  // authoritative and releases its write guard when a page confirms the value
  // it wrote. Gating that page out froze the row and re-pinned what the user
  // had just unpinned (#76919).
  it('is false when only the pinned flag changed', () => {
    const a = [session('a', 't', { pinned: true })]
    const b = [session('a', 't', { pinned: false })]
    expect(sameCronSignature(a, b)).toBe(false)
  })

  // The backend stamps live activity on list rows (kanban counts, goal, cron,
  // background jobs). A card finishing elsewhere moves only those fields, so a
  // gate that ignored them kept the sidebar badge and stripe frozen on the old
  // counts until the row's own messages moved. [daveey]
  it.each([
    ['kanban_active', 2, 1],
    ['kanban_blocked', 1, undefined],
    ['kanban_blocked_on', ['remote/default/t_one'], ['remote/default/t_two']],
    ['needs_you', [{ kind: 'card', question: '', ref: 'default/t_one' }], undefined],
    ['kanban_unseen', 1, undefined],
    ['goal_active', true, undefined],
    ['cron_active', true, undefined],
    ['background_active', 1, 2]
  ] as const)('is false when only %s changed', (field, before, after) => {
    const a = [session('a', 't', { [field]: before })]
    const b = [session('a', 't', { [field]: after })]
    expect(sameCronSignature(a, b)).toBe(false)
    expect(sameCronSignature(a, [session('a', 't', { [field]: before })])).toBe(true)
  })

  it('is false when only the archived flag changed', () => {
    const a = [session('a', 't', { archived: false })]
    const b = [session('a', 't', { archived: true })]
    expect(sameCronSignature(a, b)).toBe(false)
  })
})

describe('sessionMessagesSignature', () => {
  const msg = (role: string, content: string) =>
    ({ role, content }) as Parameters<typeof sessionMessagesSignature>[0][number]

  it('is stable for identical transcripts', () => {
    expect(sessionMessagesSignature([msg('user', 'hi')])).toBe(sessionMessagesSignature([msg('user', 'hi')]))
  })

  it('changes when content changes', () => {
    expect(sessionMessagesSignature([msg('user', 'hi')])).not.toBe(sessionMessagesSignature([msg('user', 'yo')]))
  })

  it('changes when a message is appended', () => {
    const one = [msg('user', 'hi')]
    expect(sessionMessagesSignature(one)).not.toBe(sessionMessagesSignature([...one, msg('assistant', 'hey')]))
  })
})
