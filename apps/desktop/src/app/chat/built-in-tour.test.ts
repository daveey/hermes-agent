// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'

import { runtimeTranslations } from '@/i18n'
import { formatCombo } from '@/lib/keybinds/combo'
import { TIP_CATALOG } from '@/lib/tips/catalog'
import { startTour } from '@/lib/tour'
import { $bindings } from '@/store/keybinds'

import { runBuiltInTour } from './built-in-tour'

vi.mock('@/lib/tour', () => ({ startTour: vi.fn().mockResolvedValue({ success: true }) }))
vi.mock('@/store/local-setup-offer', () => ({ readLocalSetupEligibility: vi.fn().mockResolvedValue({ fit: null }) }))

const initialBindings = $bindings.get()
afterEach(() => {
  document.body.replaceChildren()
  $bindings.set(initialBindings)
  vi.clearAllMocks()
})

describe('built-in creation guidance without the removed navigation row', () => {
  it.each(['quick', 'full'] as const)(
    '%s tour teaches the current creation shortcut at the retained composer',
    async preset => {
      for (const handle of ['sessions-sidebar', 'composer', 'model-pill']) {
        const node = document.createElement('div')
        node.dataset.tour = handle
        node.getBoundingClientRect = () => new DOMRect(0, 0, 300, 100)
        document.body.append(node)
      }

      $bindings.set({ ...initialBindings, 'session.new': ['mod+alt+n'] })
      await runBuiltInTour(preset, () => true, 'inactive')
      const steps = vi.mocked(startTour).mock.calls[0][0]
      expect(steps).toContainEqual({
        selector: '[data-tour="composer"]',
        title: runtimeTranslations().appTour.newSession.title,
        text: `${runtimeTranslations().appTour.newSession.text} (${formatCombo('mod+alt+n')})`
      })

      for (const step of steps) {
        expect(step.selector).toBeDefined()
        expect(document.querySelector(step.selector!)).not.toBeNull()
      }

      expect(TIP_CATALOG.find(tip => tip.id === 'new-session')).toMatchObject({
        keybind: 'session.new',
        targets: ['[data-tour="composer"]']
      })
    }
  )

  it('keeps creation guidance when the shortcut is explicitly unbound', async () => {
    const node = document.createElement('div')
    node.dataset.tour = 'composer'
    node.getBoundingClientRect = () => new DOMRect(0, 0, 300, 100)
    document.body.append(node)
    $bindings.set({ ...initialBindings, 'session.new': [] })
    await runBuiltInTour('quick', () => true, 'inactive')
    expect(vi.mocked(startTour).mock.calls[0][0]).toContainEqual({
      selector: '[data-tour="composer"]',
      title: runtimeTranslations().appTour.newSession.title,
      text: runtimeTranslations().appTour.newSession.text
    })
  })
})
