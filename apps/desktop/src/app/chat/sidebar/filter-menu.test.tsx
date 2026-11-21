import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'

import { I18nProvider, TRANSLATIONS, useI18n } from '@/i18n'
import type { I18nContextValue } from '@/i18n'
import { $interfaceMode } from '@/store/interface-mode'
import {
  $sidebarListGroupIds,
  $sidebarOrdering,
  $sidebarRowMeta,
  $sidebarStatusFilter,
  resetSidebarView,
  setSidebarGrouping,
  setSidebarOrdering
} from '@/store/layout'

import { SidebarFilterMenu } from './filter-menu'

let i18n: I18nContextValue

it('drops age, tokens and cost from Show without removing their sorting keys or legacy persistence', () => {
  $interfaceMode.set('advanced')
  $sidebarRowMeta.set(['updated', 'tokens', 'cost'])
  render(
    <I18nProvider configClient={null} initialLocale="en">
      <Menu />
    </I18nProvider>
  )
  const f = TRANSLATIONS.en.sidebar.filter
  fireEvent.keyDown(screen.getByRole('button', { name: f.filters }), { key: 'Enter' })
  fireEvent.keyDown(screen.getByRole('menuitem', { name: f.show }), { key: 'ArrowRight' })

  for (const name of [f.updated, f.tokens, f.cost]) {
    expect(screen.queryByRole('menuitemcheckbox', { name })).toBeNull()
  }

  expect(screen.getByRole('menuitemcheckbox', { name: f.profile })).toBeTruthy()
  expect($sidebarRowMeta.get()).toEqual(['updated', 'tokens', 'cost'])
  fireEvent.keyDown(screen.getByRole('menuitemcheckbox', { name: f.profile }), { key: 'Escape' })
  fireEvent.keyDown(screen.getByRole('button', { name: f.filters }), { key: 'Enter' })
  // Selecting cost retains the existing conditional sort visibility even without cost-bearing sessions.
  act(() => setSidebarOrdering('cost'))
  fireEvent.keyDown(screen.getByRole('menuitem', { name: f.ordering }), { key: 'ArrowRight' })

  for (const name of [f.updated, f.tokens, f.cost]) {
    expect(screen.getByRole('menuitemradio', { name })).toBeTruthy()
  }
})

function Menu() {
  i18n = useI18n()

  return <SidebarFilterMenu />
}

afterEach(() => {
  cleanup()
  resetSidebarView()
})

it('translates the live filter menu while preserving selected values and the current grouping order', async () => {
  $interfaceMode.set('advanced')
  setSidebarGrouping('date')
  setSidebarOrdering('manual')
  $sidebarListGroupIds.set(['today'])
  render(
    <I18nProvider configClient={null} initialLocale="zh">
      <Menu />
    </I18nProvider>
  )
  const zh = TRANSLATIONS.zh.sidebar.filter
  fireEvent.keyDown(screen.getByRole('button', { name: zh.filters }), { key: 'Enter' })
  const group = screen.getByRole('menuitem', { name: new RegExp(zh.grouping) })
  fireEvent.keyDown(group, { key: 'ArrowRight' })
  expect(screen.getByRole('menuitemradio', { name: zh.updated }).getAttribute('aria-checked')).toBe('true')
  fireEvent.keyDown(screen.getByRole('menuitemradio', { name: zh.updated }), { key: 'Escape' })
  fireEvent.keyDown(screen.getByRole('button', { name: zh.filters }), { key: 'Enter' })
  fireEvent.keyDown(screen.getByRole('menuitem', { name: zh.status }), { key: 'ArrowRight' })
  fireEvent.click(screen.getByRole('menuitemcheckbox', { name: zh.needsInput }))
  expect($sidebarStatusFilter.get()).toContain('needs-input')
  await act(() => i18n.setLocale('ja'))
  const ja = TRANSLATIONS.ja.sidebar.filter
  expect(screen.getByText(TRANSLATIONS.ja.sidebar.profileRail)).toBeTruthy()
  expect(screen.getByText(TRANSLATIONS.ja.sidebar.markAllRead)).toBeTruthy()
  expect(screen.queryByText(TRANSLATIONS.en.sidebar.profileRail)).toBeNull()
  expect(screen.queryByText(TRANSLATIONS.en.sidebar.markAllRead)).toBeNull()
  fireEvent.keyDown(screen.getByRole('menuitem', { name: ja.status }), { key: 'ArrowRight' })
  expect(screen.getByRole('menuitemcheckbox', { name: ja.needsInput }).getAttribute('aria-checked')).toBe('true')
  expect(screen.queryByRole('menuitemcheckbox', { name: zh.needsInput })).toBeNull()
  expect($sidebarOrdering.get()).toBe('manual')
})
