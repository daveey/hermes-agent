import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { EMPTY_OVERRIDE, isInherited, ModelOverrideField, overrideCreateFields, overrideLabel, overridePatch } from './model-override'

vi.mock('./ui', () => ({ useKanban: () => ({ modelInherit: 'Dynamic routing' }) }))
afterEach(cleanup)

describe('model-neutral Kanban requirements', () => {
  it('offers classes, not a model catalog, and emits auto provider', () => {
    const onChange = vi.fn()

    render(<ModelOverrideField onChange={onChange} value={EMPTY_OVERRIDE} />)
    const selector = screen.getByRole('combobox', { name: 'Dynamic routing' })

    expect(Array.from(selector.querySelectorAll('option')).every(option => !option.value || option.value.startsWith('class:'))).toBe(true)
    fireEvent.change(selector, { target: { value: 'class:deep' } })
    expect(onChange).toHaveBeenLastCalledWith({ effort: '', model: 'class:deep', provider: 'auto' })
  })

  it('does not display or resubmit a historical concrete pin', () => {
    const onChange = vi.fn()

    render(<ModelOverrideField onChange={onChange} value={{ effort: '', model: 'gpt-6-luna', provider: 'openai-codex' }} />)
    const selector = screen.getByRole('combobox', { name: 'Dynamic routing' }) as HTMLSelectElement

    expect(selector.value).toBe('')
    fireEvent.change(selector, { target: { value: 'class:fast' } })
    expect(onChange).toHaveBeenLastCalledWith({ effort: '', model: 'class:fast', provider: 'auto' })
  })

  it('retains inherited and depth-only semantics', () => {
    expect(isInherited(EMPTY_OVERRIDE)).toBe(true)
    expect(overrideLabel(EMPTY_OVERRIDE, 'Dynamic routing')).toBe('Dynamic routing')
    expect(overridePatch({ effort: 'ultra', model: '', provider: '' })).toEqual({ clear_model_override: true, reasoning_effort: 'ultra' })
    expect(overrideCreateFields(EMPTY_OVERRIDE)).toEqual({})
    expect(overridePatch(EMPTY_OVERRIDE)).toEqual({ clear_model_override: true, clear_reasoning_effort: true })
  })

  it.each([overridePatch, overrideCreateFields])('refuses concrete or provider-only payloads', mapper => {
    expect(() => mapper({ effort: '', model: 'grok-4.6', provider: 'xai-oauth' })).toThrow(/dynamic class/)
    expect(() => mapper({ effort: '', model: '', provider: 'anthropic' })).toThrow(/dynamic class/)
    expect(() => mapper({ effort: '', model: 'class:deep', provider: 'anthropic' })).toThrow(/dynamic class/)
    expect(mapper({ effort: 'high', model: 'class:deep', provider: '' })).toMatchObject({ model_override: 'class:deep', provider_override: 'auto', reasoning_effort: 'high' })
  })
})
