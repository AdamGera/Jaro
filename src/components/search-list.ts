import type { KeyboardEvent } from "react"

export function pagedSearchIndex(event: KeyboardEvent<HTMLInputElement>, viewport: HTMLElement | null, current: number) {
  if (!viewport || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
  const { key } = event
  const boundary = key === "Home" || key === "End"
  if (boundary ? event.currentTarget.value !== "" : key !== "PageUp" && key !== "PageDown") return

  const options = viewport.querySelectorAll<HTMLElement>('[role="option"]:not([aria-disabled="true"])')
  if (!options[current]) return
  event.preventDefault()
  if (boundary) return key === "Home" ? 0 : options.length - 1

  const direction = key === "PageDown" ? 1 : -1
  const limit = options[current].getBoundingClientRect().top + direction * viewport.clientHeight
  let result = current
  for (let index = current + direction; index >= 0 && index < options.length; index += direction) {
    if (result !== current && direction * (options[index].getBoundingClientRect().top - limit) > 0) break
    result = index
  }
  return result
}
