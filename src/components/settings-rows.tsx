import { useId, type ReactNode } from "react"
import { ChevronDown, type LucideIcon } from "lucide-react"

export type SettingControlLabels = { "aria-labelledby": string; "aria-describedby"?: string }

type SettingRowProps = {
  icon: LucideIcon; title: string; context?: string; detail?: ReactNode; find?: string | false; anchor?: string
} & (
  | { label?: true; children: (labels: SettingControlLabels) => ReactNode }
  | { label: false; children: ReactNode }
)

export function SettingRow(row: SettingRowProps) {
  const { icon: Icon, title, context, detail, find, anchor } = row
  const Element = row.label === false ? "div" : "label"
  const titleId = useId()
  const contextId = context ? `${titleId}-context` : undefined
  const detailId = detail ? `${titleId}-detail` : undefined
  return <Element id={anchor} tabIndex={anchor ? -1 : undefined}
    className="dictation-option dictation-setting-row" data-setting-title={find === false ? undefined : title}
    data-setting-description={find || undefined}>
    <span className="dictation-setting-label"><Icon aria-hidden="true" />
      <span>
        <strong id={titleId}>{title}</strong>
        {contextId && <small id={contextId}>{context}</small>}
        {detailId && <small id={detailId}>{detail}</small>}
      </span>
    </span>
    {row.label === false ? row.children : row.children({
      "aria-labelledby": contextId ? `${titleId} ${contextId}` : titleId,
      "aria-describedby": detailId,
    })}
  </Element>
}

export function SettingGroup({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return <section className="dictation-preference-section" aria-labelledby={id} data-setting-group={title}>
    <h3 id={id}>{title}</h3>
    <div className="dictation-preference-group">{children}</div>
  </section>
}

export function SettingDisclosure({ icon: Icon, title, openInitially, onToggle, children }: {
  icon: LucideIcon; title: string; openInitially?: boolean; onToggle?: (open: boolean) => void; children: ReactNode
}) {
  return <details className="dictation-preference-disclosure" data-setting-group={title} open={openInitially}
    onToggle={onToggle && (event => onToggle(event.currentTarget.open))}>
    <summary><Icon aria-hidden="true" /><span>{title}</span><ChevronDown aria-hidden="true" /></summary>
    <div className="dictation-preference-group">{children}</div>
  </details>
}
