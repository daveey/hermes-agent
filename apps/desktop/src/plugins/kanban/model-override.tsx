/** Kanban class requirements are independent of interactive chat model selection. */
import { reasoningEffortLabel } from '@hermes/plugin-sdk'

import { useKanban } from './ui'

export interface TaskModelOverride {
  effort: string
  model: string
  provider: string
}

export const EMPTY_OVERRIDE: TaskModelOverride = { effort: '', model: '', provider: '' }
const CLASSES = ['class:fast', 'class:standard', 'class:deep'] as const

export const isInherited = (value: TaskModelOverride): boolean =>
  !value.model.trim() && !value.provider.trim() && !value.effort.trim()

function classModel(value: TaskModelOverride): string {
  const model = value.model.trim()

  if ((model && !(CLASSES as readonly string[]).includes(model)) ||
    (value.provider.trim() && value.provider.trim() !== 'auto')) {
    throw new Error('Kanban requires dynamic class routing')
  }

  return model
}

export function overrideLabel(value: TaskModelOverride, inheritCopy: string): string {
  const model = value.model.trim()
  const base = (CLASSES as readonly string[]).includes(model) ? model : inheritCopy
  const effort = value.effort.trim() ? reasoningEffortLabel(value.effort) : ''

  return effort ? `${base} · ${effort}` : base
}

/** Legacy concrete values are displayed as dynamic routing and never resubmitted. */
export function ModelOverrideField({ onChange, value }: {
  onChange: (next: TaskModelOverride) => void
  value: TaskModelOverride
}) {
  const k = useKanban()
  const model = (CLASSES as readonly string[]).includes(value.model) ? value.model : ''

  return (
    <div className="flex gap-2">
      <select
        aria-label={k.modelInherit}
        className="h-8 min-w-0 flex-1 rounded border bg-background px-2 text-xs"
        onChange={event => onChange({ ...value, model: event.target.value, provider: event.target.value ? 'auto' : '' })}
        value={model}
      >
        <option value="">{k.modelInherit}</option>
        {CLASSES.map(level => <option key={level} value={level}>{level}</option>)}
      </select>
      <select
        aria-label={reasoningEffortLabel('high')}
        className="h-8 rounded border bg-background px-2 text-xs"
        onChange={event => onChange({ effort: event.target.value, model, provider: model ? 'auto' : '' })}
        value={value.effort}
      >
        <option value="">{k.modelInherit}</option>
        {['none', 'low', 'medium', 'high', 'ultra'].map(effort =>
          <option key={effort} value={effort}>{reasoningEffortLabel(effort)}</option>)}
      </select>
    </div>
  )
}

export function overridePatch(value: TaskModelOverride): Record<string, unknown> {
  const model = classModel(value)
  const effort = value.effort.trim()

  return {
    ...(model ? { model_override: model, provider_override: 'auto' } : { clear_model_override: true }),
    ...(effort ? { reasoning_effort: effort } : { clear_reasoning_effort: true })
  }
}

export function overrideCreateFields(value: TaskModelOverride): Record<string, unknown> {
  const model = classModel(value)
  const effort = value.effort.trim()

  return {
    ...(model ? { model_override: model, provider_override: 'auto' } : {}),
    ...(effort ? { reasoning_effort: effort } : {})
  }
}
