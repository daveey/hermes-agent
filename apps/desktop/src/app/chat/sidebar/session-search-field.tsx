import { useEffect, useRef, useState } from 'react'

import { SearchField } from '@/components/ui/search-field'
import { useI18n } from '@/i18n'
import { SESSION_SEARCH_FOCUS_EVENT } from '@/store/layout'

/** Search is a disclosure of the existing session.focusSearch action, not a
 * second global key handler. Keep the query in ChatSidebar for its filters. */
export function SessionSearchField({
  query,
  onQueryChange
}: {
  query: string
  onQueryChange: (query: string) => void
}) {
  const { t } = useI18n()
  const [visible, setVisible] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const onFocus = () => {
      setVisible(true)
      inputRef.current?.focus({ preventScroll: true })
    }

    window.addEventListener(SESSION_SEARCH_FOCUS_EVENT, onFocus)

    return () => window.removeEventListener(SESSION_SEARCH_FOCUS_EVENT, onFocus)
  }, [])

  useEffect(() => {
    if (visible) {
      inputRef.current?.focus({ preventScroll: true })
    }
  }, [visible])

  if (!visible) {
    return null
  }

  return (
    <div
      className="shrink-0 px-2 pb-1 pt-1"
      onBlur={() => {
        if (!query.trim()) {
          onQueryChange('')
          setVisible(false)
        }
      }}
    >
      <SearchField
        aria-label={t.sidebar.searchAria}
        inputRef={inputRef}
        onChange={onQueryChange}
        onClear={() => {
          onQueryChange('')
          setVisible(false)
        }}
        onKeyDown={event => {
          if (event.key === 'Escape') {
            event.preventDefault()
            event.stopPropagation()
            onQueryChange('')
            setVisible(false)
          }
        }}
        placeholder={t.sidebar.searchPlaceholder}
        value={query}
      />
    </div>
  )
}
