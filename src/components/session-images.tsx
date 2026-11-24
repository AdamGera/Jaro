import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react"
import { AppWindow, ChevronDown, Crop, ImagePlus, Images, ScreenShare, X } from "lucide-react"
import { Dialog, DropdownMenu } from "radix-ui"
import { desktopBridge, type DesktopScreenshotSource } from "@/desktop"
import { useInterfaceLanguage } from "@/interface-language"
import { portableScreenTargets, type CaptureTarget, type ScreenArea } from "@/portable-capture"
import { clock } from "@/speaker-activity"
import type { SavedSession } from "@/audio"
import { AnnotationAgentPicker, AnnotationDeliveryLog } from "./annotation-destinations"
import { Button } from "./ui/button"
import { ActionTooltip } from "./ui/tooltip"
import { ScreenAreaDialog } from "./screen-area-dialog"

type ImageAsset = { id: string; url: string }
type Tile = ImageAsset & { label: string; crop?: CSSProperties; removable?: boolean }
type ScreenshotSource = DesktopScreenshotSource | "picker"
type ScreenshotPreview = { target: CaptureTarget; file: File; url: string; width: number; height: number }
const screenshotOptions = [
  { source: "area", icon: Crop, label: "Capture area…" },
  { source: "window", icon: AppWindow, label: "Capture window…" },
  { source: "display", icon: ScreenShare, label: "Capture display…" },
  { source: "picker", icon: ScreenShare, label: "Capture screenshot…" },
] as const
const hasPortableScreenshots = document.documentElement.hasAttribute("data-jaro-screen-recording")
  || document.documentElement.hasAttribute("data-jaro-linux-audio")

async function jpeg(file: File, crop?: ScreenArea) {
  if (file.size > 30 * 1024 * 1024) throw new Error("Choose an image smaller than 30 MB.")
  const image = await createImageBitmap(file)
  try {
    const { x, y, width, height } = crop ?? { x: 0, y: 0, width: image.width, height: image.height }
    const scale = Math.min(1, 1600 / Math.max(width, height))
    const canvas = document.createElement("canvas")
    canvas.width = Math.max(1, Math.round(width * scale))
    canvas.height = Math.max(1, Math.round(height * scale))
    const context = canvas.getContext("2d", { alpha: false })
    if (!context) throw new Error("Could not prepare the image.")
    context.fillStyle = "#fff"
    context.fillRect(0, 0, canvas.width, canvas.height)
    context.drawImage(image, x, y, width, height, 0, 0, canvas.width, canvas.height)
    for (const quality of [.9, .68, .46, .28]) {
      const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, "image/jpeg", quality))
      if (blob && blob.size <= 512 * 1024) return blob
    }
    throw new Error("This image is too large after compression.")
  } finally {
    image.close()
  }
}

function TileImage({ tile }: { tile: Tile }) {
  return tile.crop
    ? <span aria-hidden="true" className="session-image-crop" style={tile.crop} />
    : <img src={tile.url} alt={tile.label} draggable={false} loading="lazy" decoding="async" />
}

export function SessionImages({ session, children }: { session: SavedSession; children?: ReactNode }) {
  const { t } = useInterfaceLanguage()
  const native = desktopBridge()
  const endpoint = `/api/sessions/${session.id}/images`
  const input = useRef<HTMLInputElement>(null)
  const previousTile = useRef<HTMLElement | null>(null)
  const [assets, setAssets] = useState<ImageAsset[]>([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState(false)
  const [imageMenuOpen, setImageMenuOpen] = useState(false)
  const [viewer, setViewer] = useState<Tile>()
  const [error, setError] = useState("")
  const [status, setStatus] = useState("")
  const [mutating, setMutating] = useState(false)
  const [revision, setRevision] = useState(0)
  const [managed, setManaged] = useState<boolean>()
  const [screenshots, setScreenshots] = useState<ScreenshotSource[]>([])
  const [windowsPicker, setWindowsPicker] = useState(false)
  const [sourcePicker, setSourcePicker] = useState<{ source: DesktopScreenshotSource; targets: CaptureTarget[] }>()
  const [capturePreview, setCapturePreview] = useState<ScreenshotPreview>()
  const captureRequest = useRef<AbortController>(undefined)
  const complete = session.status !== "processing" && !session.finalizationPending

  useEffect(() => {
    const controller = new AbortController()
    void fetch(endpoint, { cache: "no-store", signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error()
      setAssets(await response.json())
    }).catch(() => { if (!controller.signal.aborted) setError("Could not load the saved images.") })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [endpoint])

  useEffect(() => {
    if (hasPortableScreenshots) {
      const controller = new AbortController()
      void fetch("/api/portable/screenshots", { cache: "no-store", signal: controller.signal }).then(async response => {
        if (!response.ok) return
        const { targets, picker } = await response.json()
        if (!controller.signal.aborted && Array.isArray(targets)) {
          setScreenshots(screenshotOptions.map(option => option.source).filter(source => targets.includes(source)))
          setWindowsPicker(picker === "workspace")
        }
      }).catch(() => {})
      return () => controller.abort()
    }
    if (!native) return
    let active = true
    void native.request("annotationRoutingPolicy").then(result => { if (active) setManaged(result.managed) })
      .catch(() => { if (active) setError("Could not check whether OpenCode is available.") })
    void native.request("annotation", { action: "status" }).then(result => {
      if (active) setScreenshots(!result.managed && result.enabled !== false ? ["area", "window", "display"] : [])
    }).catch(() => {})
    return () => { active = false }
  }, [native])

  useEffect(() => () => captureRequest.current?.abort(), [])
  const capturePreviewUrl = capturePreview?.url
  useEffect(() => () => { if (capturePreviewUrl) URL.revokeObjectURL(capturePreviewUrl) }, [capturePreviewUrl])

  const sheet = session.annotation?.contactSheetUrl
  const frames = session.annotation?.frames ?? []
  const columns = Math.min(2, frames.length)
  const rows = columns ? Math.ceil(frames.length / columns) : 0
  const tiles: Tile[] = [
    ...(sheet ? frames.map((seconds, index) => {
      const column = index % columns
      const row = Math.floor(index / columns)
      return {
        id: `frame-${index}`, url: sheet, label: `Screen capture at ${clock(seconds)}`,
        crop: {
          backgroundImage: `url("${sheet}")`, backgroundSize: `${100 * columns}% ${100 * rows}%`,
          backgroundPosition: `${columns === 1 ? 0 : column * 100 / (columns - 1)}% ${rows === 1 ? 0 : row * 100 / (rows - 1)}%`,
        },
      }
    }) : []),
    ...assets.map((asset, index) => ({ ...asset, label: `Image ${index + 1}`, removable: true })),
  ]

  async function attach(body: ArrayBuffer, signal?: AbortSignal) {
    const response = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "image/jpeg" }, body, signal })
    if (!response.ok) {
      const failure = await response.json().catch(() => ({}))
      throw new Error(failure.detail || "Could not save the image.")
    }
    const image: ImageAsset = await response.json()
    setAssets(current => current.some(item => item.id === image.id) ? current : [...current, image])
  }

  async function add(files: File[]) {
    if (!files.length || mutating) return
    setMutating(true)
    setError("")
    setExpanded(true)
    try {
      for (const file of files) {
        const blob = await jpeg(file)
        await attach(await blob.arrayBuffer())
      }
    } catch (failure) {
      setError(failure instanceof Error && failure.message ? failure.message : "Could not add the images.")
    } finally {
      setMutating(false)
    }
  }

  async function capture(source: ScreenshotSource) {
    if ((!native && !hasPortableScreenshots) || mutating) return
    setMutating(true)
    setError("")
    setStatus("")
    try {
      if (windowsPicker && source !== "picker") {
        const controller = captureRequest.current = new AbortController()
        const targets = await portableScreenTargets(source === "area" ? "display" : source, controller.signal)
        if (!controller.signal.aborted) setSourcePicker({ source, targets })
        return
      }
      if (native && source !== "picker") {
        const { jpeg } = await native.request("sessionScreenshot", { source })
        if (!jpeg) return
        const bytes = Uint8Array.from(atob(jpeg), character => character.charCodeAt(0))
        await attach(bytes.buffer)
      } else if (hasPortableScreenshots) {
        const response = await fetch(`/api/portable/screenshots?source=${source}`, { method: "POST" })
        if (response.status === 204) return
        if (!response.ok) {
          const failure = await response.json().catch(() => ({}))
          throw new Error(failure.detail || "Could not take a screenshot.")
        }
        const image = await response.blob()
        const normalized = await jpeg(new File([image], "screenshot", { type: image.type }))
        await attach(await normalized.arrayBuffer())
      }
      setExpanded(true)
      setStatus(t("Screenshot added."))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not add the screenshot.")
    } finally { setMutating(false) }
  }

  async function captureFromWindows(source: DesktopScreenshotSource, target: CaptureTarget) {
    const controller = captureRequest.current = new AbortController()
    setMutating(true)
    setError("")
    try {
      const query = new URLSearchParams({ source: source === "area" ? "display" : source, target: target.id })
      const response = await fetch(`/api/portable/screenshots?${query}`, { method: "POST", signal: controller.signal })
      if (!response.ok) {
        const failure = await response.json().catch(() => ({}))
        throw new Error(typeof failure.detail === "string" ? failure.detail : "Could not take a screenshot.")
      }
      const bytes = await response.blob()
      const file = new File([bytes], "screenshot.jpg", { type: bytes.type })
      if (source === "area") {
        const bitmap = await createImageBitmap(file)
        const { width, height } = bitmap
        bitmap.close()
        if (controller.signal.aborted) return
        setCapturePreview({ target, file, url: URL.createObjectURL(file), width, height })
      } else {
        const normalized = await jpeg(file)
        await attach(await normalized.arrayBuffer(), controller.signal)
        setExpanded(true)
        setStatus(t("Screenshot added."))
      }
      setSourcePicker(undefined)
    } catch (failure) {
      if (!controller.signal.aborted)
        setError(failure instanceof Error ? failure.message : "Could not add the screenshot.")
    } finally { setMutating(false) }
  }

  async function attachScreenshotArea(crop: ScreenArea) {
    if (!capturePreview || mutating) return
    const controller = captureRequest.current = new AbortController()
    setMutating(true)
    setError("")
    try {
      const normalized = await jpeg(capturePreview.file, crop)
      await attach(await normalized.arrayBuffer(), controller.signal)
      setCapturePreview(undefined)
      setExpanded(true)
      setStatus(t("Screenshot added."))
    } catch (failure) {
      if (!controller.signal.aborted)
        setError(failure instanceof Error ? failure.message : "Could not add the screenshot.")
    } finally { setMutating(false) }
  }

  async function remove(tile: Tile) {
    setMutating(true)
    setError("")
    try {
      const response = await fetch(tile.url, { method: "DELETE" })
      if (!response.ok) throw new Error("Could not remove the image.")
      setAssets(current => current.filter(asset => asset.id !== tile.id))
      setViewer(undefined)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not remove the image.")
    } finally {
      setMutating(false)
    }
  }

  return <section className="session-images" aria-label={t("Session images")}>
    <div className="session-images-bar">
      <Button variant="ghost" size="sm" aria-controls={`images-${session.id}`} aria-expanded={expanded}
        onClick={() => setExpanded(value => !value)}>
        <Images aria-hidden="true" />{t("Images")} <span className="session-images-count">{tiles.length}</span>
        <ChevronDown aria-hidden="true" className="session-images-chevron" />
      </Button>
      <div className="session-images-actions">
        {complete && <>
          <input ref={input} type="file" accept="image/*" multiple hidden onChange={event => {
            const files = Array.from(event.target.files ?? [])
            event.target.value = ""
            void add(files)
          }} />
          {screenshots.length > 0 ? <DropdownMenu.Root open={imageMenuOpen} onOpenChange={setImageMenuOpen}>
            <ActionTooltip label={t("Add images")}>
              <DropdownMenu.Trigger asChild>
                <Button variant="outline" size="icon-sm" aria-label={t("Add images")}
                  disabled={loading || mutating} onClick={event => {
                    // Accessibility can activate a button with a click that has no pointer or key event.
                    if (event.detail === 0) setImageMenuOpen(open => !open)
                  }}><ImagePlus aria-hidden="true" /></Button>
              </DropdownMenu.Trigger>
            </ActionTooltip>
            <DropdownMenu.Portal>
              <DropdownMenu.Content align="end" className="session-image-menu" sideOffset={5}>
                <DropdownMenu.Item onSelect={() => input.current?.click()}><ImagePlus aria-hidden="true" />{t("Choose image files…")}</DropdownMenu.Item>
                <DropdownMenu.Separator />
                {screenshotOptions.filter(({ source }) => screenshots.includes(source)).map(({ source, icon: Icon, label }) =>
                  <DropdownMenu.Item key={source} onSelect={() => void capture(source)}><Icon aria-hidden="true" />{t(label)}</DropdownMenu.Item>)}
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root> : <ActionTooltip label={t("Add images")}>
            <Button variant="outline" size="icon-sm" aria-label={t("Add images")}
              disabled={loading || mutating} onClick={() => input.current?.click()}><ImagePlus aria-hidden="true" /></Button>
          </ActionTooltip>}
        </>}
        {native && complete && <AnnotationAgentPicker native={native} id={session.id}
          annotation={!!session.annotation} disabled={mutating} allowOpenCode={managed === false}
          onChanged={(agent, message) => {
            setStatus(message ?? "")
            if (agent === "opencode") setRevision(value => value + 1)
          }} />}
      </div>
    </div>
    {!expanded && tiles[0] && <button className="session-images-preview" type="button"
      aria-label={t("Expand images")} onClick={() => setExpanded(true)}><TileImage tile={tiles[0]} /></button>}
    <div id={`images-${session.id}`} hidden={!expanded}>
      {!!tiles.length && expanded && <div className="session-images-grid">
        {tiles.map(tile => <button key={tile.id} className="session-image-tile" type="button"
          aria-label={tile.label} onClick={event => { previousTile.current = event.currentTarget; setViewer(tile) }}>
          <TileImage tile={tile} />
        </button>)}
      </div>}
      {expanded && !tiles.length && <p className="session-image-message">{t("No images attached.")}</p>}
    </div>
    {status && <p className="session-image-message" role="status">{status}</p>}
    {managed && <p className="session-image-message" role="status">{t("OpenCode processing is managed by your organization.")}</p>}
    {error && !sourcePicker && !capturePreview && <p className="session-image-message" role="alert">{error}</p>}
    {children}
    {native && session.annotation && <AnnotationDeliveryLog native={native} id={session.id} revision={revision} remoteEnabled={managed === false} />}
    <Dialog.Root open={!!sourcePicker} onOpenChange={open => { if (!open && !mutating) setSourcePicker(undefined) }}>
      <Dialog.Portal>
        <Dialog.Overlay className="note-language-scrim" />
        <Dialog.Content className="note-language-confirm" onInteractOutside={event => event.preventDefault()}
          onEscapeKeyDown={event => { if (mutating) event.preventDefault() }}>
          <Dialog.Title>{sourcePicker?.source === "window" ? t("Choose a window to capture") : t("Choose a display to capture")}</Dialog.Title>
          <Dialog.Description>{sourcePicker?.source === "window" ? t("Choose the window to add to this session.")
            : t("You have three seconds to switch windows after choosing a display.")}</Dialog.Description>
          <div className="portable-screen-targets">
            {sourcePicker?.targets.map(target => <Button key={target.id} size="sm" variant="ghost" title={target.label} disabled={mutating}
              onClick={() => { if (sourcePicker) void captureFromWindows(sourcePicker.source, target) }}><span>{target.label}</span></Button>)}
            {!sourcePicker?.targets.length && <p role="status">{t("No sources are available.")}</p>}
          </div>
          {mutating && <p role="status">{t("Taking screenshot…")}</p>}
          {error && <p role="alert">{error}</p>}
          <div className="note-language-actions">
            <Button variant="ghost" size="sm" disabled={mutating} onClick={() => setSourcePicker(undefined)}>{t("Cancel")}</Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
    {capturePreview && <ScreenAreaDialog key={capturePreview.url} size={capturePreview} imageUrl={capturePreview.url} action="capture"
      pending={mutating} error={error} onCancel={() => { setCapturePreview(undefined); setError("") }}
      onRefresh={() => void captureFromWindows("area", capturePreview.target)} onConfirm={area => void attachScreenshotArea(area)} />}
    <Dialog.Root open={!!viewer} onOpenChange={open => { if (!open) setViewer(undefined) }}>
      <Dialog.Portal>
        <Dialog.Overlay className="note-language-scrim" />
        <Dialog.Content className="note-language-confirm session-image-viewer" onCloseAutoFocus={event => {
          event.preventDefault()
          if (previousTile.current?.isConnected) previousTile.current.focus()
        }}>
          <div className="session-image-viewer-header">
            <Dialog.Title>{viewer?.label}</Dialog.Title>
            <Dialog.Close asChild>
              <Button variant="ghost" size="icon-sm" aria-label={t("Close image")}><X aria-hidden="true" /></Button>
            </Dialog.Close>
          </div>
          <Dialog.Description className="sr-only">{t("Image from this session")}</Dialog.Description>
          {viewer && <TileImage tile={viewer} />}
          {viewer?.removable && <Button size="sm" variant="outline" disabled={mutating}
            onClick={() => void remove(viewer)}>{t("Remove image")}</Button>}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  </section>
}
