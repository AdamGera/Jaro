import AppKit

@objc(JardDictationMeter)
final class JardDictationMeter: NSView {
    private var tuning = DotScopeTuning.hud
    private let scope = DotScopeSimulation()
    private var stage = DotScopeStage.recording
    private var previousTick: TimeInterval?
    private var visible = false
    private var anchorRight = false
    private var announcedStatus: String?
    var reduceMotion = { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }
    var announceAccessibility: (NSView, String) -> Void = { view, status in
        NSAccessibility.post(element: view, notification: .announcementRequested, userInfo: [
            .announcement: status,
            .priority: NSAccessibilityPriorityLevel.medium.rawValue
        ])
    }
    @objc var embedded = false {
        didSet {
            tuning = .hud
            if embedded {
                tuning.columns = 31
                tuning.width = 78
                tuning.idleWidth = 78
                tuning.fitsBounds = true
            }
            scope.tune(tuning, jumpLayout: true)
            setAccessibilityElement(visible && !embedded && accessibilityLabel() != nil)
            if embedded { announcedStatus = nil }
            needsDisplay = true
        }
    }

    @objc(fitEmbeddedWidth:anchorRight:)
    func fitEmbeddedWidth(_ width: CGFloat, anchorRight: Bool) {
        guard embedded else { return }
        let pitch = CGFloat(tuning.spacing)
        let columns = max(1, Int((width / pitch).rounded(.up)))
        guard columns != tuning.columns || anchorRight != self.anchorRight else { return }
        let fitted = Float(CGFloat(columns) * pitch)
        self.anchorRight = anchorRight
        tuning.columns = columns
        tuning.width = fitted
        tuning.idleWidth = fitted
        scope.tune(tuning, jumpLayout: true)
        needsDisplay = true
    }

    @objc(setStage:label:)
    func setStage(_ value: Int, label: String) {
        let next: DotScopeStage
        switch value {
        case 1: next = .processing
        case 2: next = .done
        case 3: next = .failed
        default: next = .recording
        }
        if next == .recording && (!visible || stage != .recording) {
            scope.restart(reduceMotion: reduceMotion())
            previousTick = nil
        }
        visible = true
        stage = next
        let status = next != .done && !label.isEmpty ? label : nil
        setAccessibilityElement(!embedded && status != nil)
        setAccessibilityLabel(status)
        setAccessibilityRole(.staticText)
        if status == nil { announcedStatus = nil }
        needsDisplay = true
    }

    @objc func announceStatus() {
        guard isAccessibilityElement(), let status = accessibilityLabel(), status != announcedStatus else { return }
        announcedStatus = status
        announceAccessibility(self, status)
    }

    @objc override var isFlipped: Bool { true }

    override func hitTest(_ point: NSPoint) -> NSView? {
        embedded ? nil : super.hitTest(point)
    }

    @objc(advanceAt:rms:)
    func advance(at time: TimeInterval, rms: Double) {
        let delta = time - (previousTick ?? time - 1.0 / 60.0)
        previousTick = time
        let loudness = 20 * log10(max(0, rms) + 1e-6)
        scope.advance(dt: Float(delta), stage: stage, visible: visible, loudness: Float(loudness),
                      reduceMotion: reduceMotion())
        needsDisplay = true
    }

    @objc func beginDismissal() {
        visible = false
        announcedStatus = nil
        setAccessibilityElement(false)
        setAccessibilityLabel(nil)
    }

    @objc var isSettled: Bool { scope.settled }

    @objc func dismiss() {
        beginDismissal()
        previousTick = nil
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let context = NSGraphicsContext.current?.cgContext else { return }
        let pose = scope.pose
        guard pose.opacity > 0.002 else { return }
        let palette = DotScopePalette.dark(pose: pose)
        let pitch = tuning.spacing
        let width = pose.width * 0.5, height = pose.height * 0.5
        let plate = CGRect(x: CGFloat(-width), y: CGFloat(-height), width: CGFloat(pose.width), height: CGFloat(pose.height))
        let path = CGPath(roundedRect: plate, cornerWidth: CGFloat(height), cornerHeight: CGFloat(height), transform: nil)
        let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!
        func color(_ rgb: SIMD3<Float>, _ alpha: Float) -> CGColor {
            CGColor(colorSpace: sRGB, components: [CGFloat(rgb.x), CGFloat(rgb.y), CGFloat(rgb.z), CGFloat(alpha)])!
        }
        func fill(_ rgb: SIMD4<Float>) { context.setFillColor(color(SIMD3<Float>(rgb.x, rgb.y, rgb.z), rgb.w)) }

        context.saveGState()
        if embedded { context.clip(to: bounds) }
        let halfWidth = CGFloat(width * pose.scale)
        let center = embedded ? (anchorRight ? bounds.maxX - halfWidth : bounds.minX + halfWidth) : bounds.midX
        context.translateBy(x: center, y: bounds.midY)
        context.scaleBy(x: CGFloat(pose.scale), y: CGFloat(pose.scale))
        context.setAlpha(CGFloat(pose.opacity))
        if !embedded {
            context.saveGState()
            context.setShadow(offset: CGSize(width: 0, height: 2), blur: CGFloat(5 + pose.softness),
                              color: color(.zero, palette.shadow.w))
            fill(palette.plate)
            context.addPath(path)
            context.fillPath()
            context.restoreGState()
            context.setStrokeColor(color(SIMD3<Float>(palette.rim.x, palette.rim.y, palette.rim.z), palette.rim.w))
            context.setLineWidth(0.6)
            context.addPath(path)
            context.strokePath()
        }
        context.addPath(path)
        context.clip()

        if pose.glow > 0.01 {
            let shade = (palette.base + palette.core) * 0.5
            if let glow = CGGradient(colorsSpace: sRGB, colors: [color(shade, 0.165 * min(1, pose.glow)), color(shade, 0)] as CFArray,
                                     locations: [0, 1]) {
                context.drawRadialGradient(glow, startCenter: .zero, startRadius: 0, endCenter: .zero,
                                           endRadius: CGFloat(34 + 20 * pose.glow), options: [])
            }
        }

        let presence = 0.5 + 0.5 * max(pose.live, pose.cool)
        let centerX = Float(scope.columns - 1) * 0.5, centerY = Float(scope.rows - 1) * 0.5
        func smooth(_ lo: Float, _ hi: Float, _ value: Float) -> Float {
            let t = min(1, max(0, (value - lo) / (hi - lo)))
            return t * t * (3 - 2 * t)
        }
        for (index, dot) in scope.dots.enumerated() {
            let x = (Float(index % scope.columns) - centerX) * pitch
            let y = (Float(index / scope.columns) - centerY) * pitch
            let cap = height - hypot(max(0, abs(x) - width + height), y)
            let mask = smooth(1.4, 4.6, cap)
            if mask <= 0 { continue }
            let power = max(dot.x, 1e-5), fresh = dot.y
            let floorAlpha = 0.07 * (1 - 0.7 * smooth(0.35, 0.95, abs(y) / height)) * presence * pose.grid
            let alpha = floorAlpha + (0.96 - floorAlpha) * pow(power, 0.9 * (1.4 - 0.4 * fresh))
            let core = min(0.9, power * power * 1.1) * (0.3 + 0.7 * fresh)
            let rgb = palette.base + (palette.core - palette.base) * core
            let radius = (0.25 + 0.17 * pow(power, 0.6)) * (1 - 0.18 * (1 - fresh) * smooth(0.03, 0.2, power)) * pitch
            func drawDot(_ radius: Float, _ alpha: Float) {
                context.setFillColor(color(rgb, alpha * mask))
                context.fillEllipse(in: CGRect(x: CGFloat(x - radius), y: CGFloat(y - radius),
                                              width: CGFloat(2 * radius), height: CGFloat(2 * radius)))
            }
            if power > 0.05 { drawDot(2 * radius, 0.132 * power * power) }
            drawDot(radius, alpha)
        }
        context.restoreGState()
    }
}
