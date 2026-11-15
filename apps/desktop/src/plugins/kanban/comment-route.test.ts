import { describe, expect, it, vi } from 'vitest'

vi.mock('@hermes/plugin-sdk', () => ({ connectionForHubGateway: (name: string) => name === 'hub' ? 'hub' : undefined }))

const { commentOnOwner } = await import('./comment-route')
const owner = { gateway: 'local', profile: 'research', board: 'ops' }
const refused = new Error('HTTP 409: foreign_card: t_x was created on hub and lives there; kanban_supervisor_request hub-comment')

describe('comment retry safety', () => {
  it.each([undefined, { ...owner, profile: undefined }, { ...owner, board: '' }, { ...owner, gateway: 'hub' }])('refuses unsafe or looping identity %j', async target => {
    const send = vi.fn().mockRejectedValue(refused)
    await expect(commentOnOwner('t_x', target, send)).rejects.toThrow('Your comment was not saved')
    expect(send).toHaveBeenCalledTimes(1)
  })

  it.each([new Error('timeout'), new Error('foreign_card: t_other was created on hub and lives there;')])('does not retry uncertain or mismatched failures %s', async error => {
    const send = vi.fn().mockRejectedValue(error)
    await expect(commentOnOwner('t_x', owner, send)).rejects.toBe(error)
    expect(send).toHaveBeenCalledTimes(1)
  })

  it('never retries an uncertain owner write and tells the user to check first', async () => {
    const send = vi.fn().mockRejectedValueOnce(refused).mockRejectedValueOnce(new Error('timeout'))
    await expect(commentOnOwner('t_x', owner, send)).rejects.toThrow('check its comments before trying again')
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('does not echo malformed owner text or worker instructions', async () => {
    const send = vi.fn().mockRejectedValue(new Error('foreign_card: t_x was created on kanban_supervisor_request(...)'))
    await expect(commentOnOwner('t_x', owner, send)).rejects.toThrow('This card belongs to another gateway.')
    expect(send).toHaveBeenCalledTimes(1)
  })
})
