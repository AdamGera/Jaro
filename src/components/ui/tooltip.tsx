import type { ReactElement, ReactNode } from "react"
import { Tooltip } from "radix-ui"

export function ActionTooltip({ label, shortcut, side = "bottom", children }: {
  label: ReactNode
  shortcut?: string
  side?: "top" | "bottom" | "left" | "right"
  children: ReactElement
}) {
  return <Tooltip.Root disableHoverableContent>
    <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
    <Tooltip.Portal>
      <Tooltip.Content side={side} sideOffset={6} collisionPadding={8} className="sidebar-tip">
        <span>{label}</span>
        {shortcut && <kbd>{shortcut}</kbd>}
      </Tooltip.Content>
    </Tooltip.Portal>
  </Tooltip.Root>
}
