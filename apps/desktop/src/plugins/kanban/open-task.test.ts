import { describe, expect, it, vi } from 'vitest'

vi.mock('@hermes/plugin-sdk', async () => ({ atom: (await import('nanostores')).atom }))

const { $openTaskRequest, installOpenTaskHook, OPEN_TASK_HOOK } = await import('./open-task')

describe('installOpenTaskHook', () => {
  it('parks the request on the named board and navigates to the board page', () => {
    const target: Record<string, unknown> = {}
    const navigate = vi.fn()
    installOpenTaskHook({ navigate }, target)

    const hook = target[OPEN_TASK_HOOK] as (id: string, board?: string) => boolean
    expect(hook('t_1a2b3c4d', 'bb')).toBe(true)
    expect($openTaskRequest.get()).toEqual({ board: 'bb', taskId: 't_1a2b3c4d' })
    expect(navigate).toHaveBeenCalledWith('/kanban')
  })

  it('keeps the current board when none is named and rejects an empty id', () => {
    const target: Record<string, unknown> = {}
    const navigate = vi.fn()
    $openTaskRequest.set(null)
    installOpenTaskHook({ navigate }, target)
    const hook = target[OPEN_TASK_HOOK] as (id: string, board?: null | string) => boolean

    expect(hook('t_00000000', null)).toBe(true)
    expect($openTaskRequest.get()).toEqual({ board: '', taskId: 't_00000000' })
    $openTaskRequest.set(null)
    expect(hook('')).toBe(false)
    expect($openTaskRequest.get()).toBeNull()
    expect(navigate).toHaveBeenCalledTimes(1)
  })

  it('seeds the owning gateway for a card on another gateway', () => {
    const target: Record<string, unknown> = {}
    const navigate = vi.fn()
    const rememberOwner = vi.fn()
    installOpenTaskHook({ navigate, rememberOwner }, target)
    const hook = target[OPEN_TASK_HOOK] as (id: string, board?: string, gateway?: string) => boolean

    expect(hook('t_abcdef01', 'bb', 'mini')).toBe(true)
    expect(rememberOwner).toHaveBeenCalledWith('t_abcdef01', 'bb', 'mini')
    expect($openTaskRequest.get()).toEqual({ board: 'bb', taskId: 't_abcdef01' })

    rememberOwner.mockClear()
    hook('t_abcdef02', 'bb')
    expect(rememberOwner).not.toHaveBeenCalled()
  })

  it('retains a qualified owner in the parked request and forwards its profile', () => {
    const target: Record<string, unknown> = {}
    const rememberOwner = vi.fn()
    installOpenTaskHook({ navigate: vi.fn(), rememberOwner }, target)
    const hook = target[OPEN_TASK_HOOK] as (...args: string[]) => boolean

    expect(hook('t_abcdef01', 'ops', 'remote', 'worker')).toBe(true)
    expect(rememberOwner).toHaveBeenCalledWith('t_abcdef01', 'ops', 'remote', 'worker')
    expect($openTaskRequest.get()).toEqual({
      board: 'ops',
      taskId: 't_abcdef01',
      owner: { gateway: 'remote', profile: 'worker', board: 'ops' }
    })
    expect(Object.isFrozen($openTaskRequest.get()?.owner)).toBe(true)
    expect((hook as unknown as { supportsQualifiedOwners?: boolean }).supportsQualifiedOwners).toBe(true)
  })

  it.each([null, '', 'worker'])('rejects incomplete explicit owner routes (%s)', profile => {
    const target: Record<string, unknown> = {}
    const navigate = vi.fn()
    const rememberOwner = vi.fn()
    installOpenTaskHook({ navigate, rememberOwner }, target)
    $openTaskRequest.set(null)
    const hook = target[OPEN_TASK_HOOK] as (
      id: string,
      board: string,
      gateway: string | null,
      profile: string | null
    ) => boolean

    expect(hook('t_abcdef01', 'ops', profile === 'worker' ? null : 'remote', profile)).toBe(false)
    expect(navigate).not.toHaveBeenCalled()
    expect(rememberOwner).not.toHaveBeenCalled()
    expect($openTaskRequest.get()).toBeNull()
  })

  it('rejects a qualified request without an explicit board', () => {
    const target: Record<string, unknown> = {}
    const navigate = vi.fn()
    installOpenTaskHook({ navigate }, target)
    const hook = target[OPEN_TASK_HOOK] as (...args: string[]) => boolean
    expect(hook('t_x', '', 'remote', 'work')).toBe(false)
    expect(navigate).not.toHaveBeenCalled()
  })

  it('disposer removes only its own hook', () => {
    const target: Record<string, unknown> = {}
    const dispose = installOpenTaskHook({ navigate: vi.fn() }, target)
    const other = () => true
    target[OPEN_TASK_HOOK] = other
    dispose()
    expect(target[OPEN_TASK_HOOK]).toBe(other)

    const dispose2 = installOpenTaskHook({ navigate: vi.fn() }, target)
    dispose2()
    expect(OPEN_TASK_HOOK in target).toBe(false)
  })
})
