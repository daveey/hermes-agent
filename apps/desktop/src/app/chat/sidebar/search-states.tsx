import { SidebarSessionSkeletons } from './section-states'

export function SidebarSearchFailure({
  visible,
  retry,
  label
}: {
  visible: boolean
  retry: () => void
  label: string
}) {
  if (!visible) {
    return null
  }

  return (
    <div className="px-2 py-1 text-xs text-(--ui-text-tertiary)" role="alert">
      <button className="underline" onClick={retry} type="button">
        {label}
      </button>
    </div>
  )
}

export function SidebarSearchEmpty({ pending, label }: { pending: boolean; label: string }) {
  return pending ? (
    <SidebarSessionSkeletons />
  ) : (
    <div className="wrap-anywhere grid min-h-24 place-items-center rounded-lg px-2 text-center text-xs text-(--ui-text-tertiary)">
      {label}
    </div>
  )
}
