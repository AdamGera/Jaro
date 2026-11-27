import { useLayoutEffect, useRef, type ReactNode } from "react"

export function LiveTranscriptScroll({ className, following, hasText, onScroll, children }: {
  className: string; following: boolean; hasText: boolean; onScroll?: (scrollTop: number) => void; children: ReactNode
}) {
  const viewport = useRef<HTMLDivElement>(null)
  const content = useRef<HTMLDivElement>(null)
  const onScrollRef = useRef(onScroll)
  useLayoutEffect(() => { onScrollRef.current = onScroll }, [onScroll])

  useLayoutEffect(() => {
    const area = viewport.current
    const body = content.current
    if (!area || !body) return
    if (!following) {
      const scrolled = () => onScrollRef.current?.(area.scrollTop)
      area.addEventListener("scroll", scrolled, { passive: true })
      scrolled()
      return () => area.removeEventListener("scroll", scrolled)
    }
    let atEnd = true
    let end = 0

    const layout = () => {
      if (!area.clientHeight) return
      end = Math.max(0, area.scrollHeight - area.clientHeight)
      if (atEnd) area.scrollTop = end
      onScrollRef.current?.(area.scrollTop)
    }
    const scrolled = () => {
      if (area.clientHeight) atEnd = end - area.scrollTop <= 2
      onScrollRef.current?.(area.scrollTop)
    }

    const observer = new ResizeObserver(layout)
    observer.observe(area)
    observer.observe(body)
    area.addEventListener("scroll", scrolled, { passive: true })
    layout()
    return () => {
      observer.disconnect()
      area.removeEventListener("scroll", scrolled)
    }
  }, [following])

  return <div className={className} ref={viewport} data-live-transcript tabIndex={hasText || following ? 0 : undefined}>
    <div ref={content} className="live-transcript-content">{children}</div>
  </div>
}
