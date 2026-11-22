import Foundation
import Darwin

enum Source: String { case mic, system }

struct MeetingAudioLevel {
    private var latest = [Source: (end: Double, rms: Double)]()

    mutating func observe(_ samples: [Float], from source: Source, endingAt end: Double) {
        guard !samples.isEmpty, end >= (latest[source]?.end ?? -.infinity) else { return }
        let energy = samples.reduce(0.0) { $0 + Double($1) * Double($1) }
        let rms = sqrt(energy / Double(samples.count))
        latest[source] = (end, rms.isFinite ? min(1, rms) : 0)
    }

    func rms(at now: Double) -> Double {
        latest.values.reduce(0) { level, sample in
            now - sample.end <= 0.30 ? max(level, sample.rms) : level
        }
    }
}
enum CaptureMode: String { case mic, system, both }
typealias MicrophonePacket = (samples: [Float], time: Double)

func microphonePreRoll(_ packets: [MicrophonePacket], from boundary: Double) -> [MicrophonePacket] {
    packets.compactMap { packet in
        let offset = max(0, (boundary - packet.time) * Double(Timeline.rate))
        guard offset < Double(packet.samples.count) else { return nil }
        let skip = Int(offset.rounded(.up))
        guard skip < packet.samples.count else { return nil }
        if skip == 0 { return packet }
        return (Array(packet.samples.dropFirst(skip)), packet.time + Double(skip) / Double(Timeline.rate))
    }
}

struct AudioWindow {
    let mixed: [Int16]
    let microphone: [Int16]
    let system: [Int16]
}
struct CaptureError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// Timestamps from both producers use the host clock. Render in short windows, allowing late
/// callbacks to arrive before committing their portion of the mix to disk.
struct Timeline {
    static let rate = 48_000
    static let limit = 3 * 3600 * rate
    struct Block {
        let start: Int
        let samples: [Float]
        let source: Source
        var end: Int { start + samples.count }
    }
    private var blocks = [Block]()
    private var lastEnd = [Source: Int]()
    private(set) var cursor = 0
    private(set) var lateFrames = 0
    let mode: CaptureMode

    init(mode: CaptureMode) { self.mode = mode }

    mutating func add(_ samples: [Float], at frame: Int, from source: Source) {
        guard !samples.isEmpty, frame < Self.limit else { return }
        var start = frame
        var aligned = samples
        if let previous = lastEnd[source] {
            let delta = frame - previous
            // Small differences between the device and host clocks accumulate.
            // Spread the correction through this packet to keep its edges continuous.
            if delta != 0, abs(delta) <= min(Self.rate / 1000, samples.count / 16) {
                start = previous
                let count = samples.count + delta
                let scale = Double(samples.count - 1) / Double(count - 1)
                aligned = (0..<count).map { i in
                    let position = Double(i) * scale
                    let left = min(Int(position), samples.count - 1)
                    let right = min(left + 1, samples.count - 1)
                    return samples[left] + Float(position - Double(left)) * (samples[right] - samples[left])
                }
            }
        }
        lastEnd[source] = start + aligned.count
        lateFrames += max(0, min(aligned.count, cursor - max(0, start)))
        if start + aligned.count > cursor {
            blocks.append(Block(start: start, samples: aligned, source: source))
        }
    }

    mutating func render(through frame: Int) -> [Int16] {
        renderWindow(through: frame, includeSources: false).mixed
    }

    var micEnd: Int { lastEnd[.mic] ?? 0 }
    var systemEnd: Int { lastEnd[.system] ?? 0 }

    func microphoneSamples(in range: Range<Int>) -> [Float] {
        var samples = [Float](repeating: 0, count: range.count)
        for block in blocks where block.source == .mic {
            let start = max(range.lowerBound, block.start)
            let end = min(range.upperBound, block.end)
            guard start < end else { continue }
            for frame in start..<end { samples[frame - range.lowerBound] += block.samples[frame - block.start] }
        }
        return samples
    }

    mutating func renderWindow(through frame: Int, includeSources: Bool) -> AudioWindow {
        let end = min(max(frame, cursor), Self.limit)
        guard end > cursor else { return AudioWindow(mixed: [], microphone: [], system: []) }
        var mix = [Float](repeating: 0, count: end - cursor)
        var mic = [Float](repeating: 0, count: includeSources ? mix.count : 0)
        var mac = [Float](repeating: 0, count: mic.count)
        for block in blocks {
            let lo = max(cursor, block.start), hi = min(end, block.end)
            if hi <= lo { continue }
            for i in lo..<hi { mix[i - cursor] += block.samples[i - block.start] }
            if includeSources {
                if block.source == .mic {
                    for i in lo..<hi { mic[i - cursor] += block.samples[i - block.start] }
                } else {
                    for i in lo..<hi { mac[i - cursor] += block.samples[i - block.start] }
                }
            }
        }
        blocks.removeAll { $0.end <= end }
        cursor = end
        let gain: Float = mode == .both ? 0.5 : 1
        func sample(_ value: Float) -> Int16 { Int16((max(-1, min(1, value)) * 32767).rounded()) }
        return AudioWindow(mixed: mix.map { sample($0 * gain) }, microphone: mic.map(sample), system: mac.map(sample))
    }
}

/// A short history of 20 ms voice decisions on the same clock as the recorded WAV.
struct DictationVoice {
    static let frameSize = Timeline.rate / 50
    static let base = 10 * frameSize
    static let cap = 30 * frameSize
    private var floor: Double?
    private var speech = [Bool]()
    private var first = 0
    private var end: Int { first + speech.count }

    mutating func observe(_ timeline: Timeline) {
        let through = min(timeline.micEnd, Timeline.limit) / Self.frameSize
        let from = max(end, (timeline.cursor + Self.frameSize - 1) / Self.frameSize, through - 150)
        guard through > from else { return }
        if from > end {
            speech.removeAll(keepingCapacity: true)
            first = from
        }
        let samples = timeline.microphoneSamples(in: from * Self.frameSize..<through * Self.frameSize)
        for index in stride(from: 0, to: samples.count, by: Self.frameSize) {
            let frame = samples[index..<index + Self.frameSize]
            let mean = frame.reduce(0.0) { $0 + Double($1) } / Double(frame.count)
            let energy = frame.reduce(0.0) { total, sample in
                let centered = Double(sample) - mean
                return total + centered * centered
            } / Double(frame.count)
            let level = 10 * log10(max(1e-18, energy))
            let noise = max(-90, min(level, (floor ?? level) + 0.06))
            floor = noise
            speech.append(level > max(-55, noise + 12))
        }
        if speech.count > 150 {
            let overflow = speech.count - 150
            speech.removeFirst(overflow)
            first += overflow
        }
    }

    func cutoff(after release: Int, through captured: Int) -> Int? {
        let base = release + Self.base
        let cap = release + Self.cap
        guard captured >= base else { return nil }
        let baseIndex = base / Self.frameSize
        var active = 0
        var gap = 0
        for index in (min(first, baseIndex)..<baseIndex).reversed() {
            if isSpeech(index) {
                active += 1
                gap = 0
                if active == 5 { break }
            } else {
                gap += 1
                if gap > 2 { break }
            }
        }
        guard active == 5 else { return base }
        var quiet = 0
        for index in baseIndex..<max(baseIndex, min(end, cap / Self.frameSize)) {
            quiet = isSpeech(index) ? 0 : quiet + 1
            if quiet == 8 { return max(base, (index + 1) * Self.frameSize) }
        }
        return captured >= cap ? cap : nil
    }

    private func isSpeech(_ index: Int) -> Bool {
        (first..<end).contains(index) && speech[index - first]
    }
}

func dictationReleaseTime(_ command: String) -> Double? {
    if command == "finish" { return hostSeconds() }
    guard command.hasPrefix("finish "), let seconds = Double(command.dropFirst(7)), seconds.isFinite else { return nil }
    return seconds
}

/// Keep a standard 44-byte PCM WAV on disk. A checkpointed length also makes an interrupted
/// recording playable. The last uncheckpointed payload can be recovered from the file size.
final class WaveSink {
    private let file: FileHandle
    private(set) var frames = 0
    private var lastHeader = 0

    init(path: String, create: Bool = true) throws {
        // The engine supplies a private, pre-created empty staging file. O_NOFOLLOW plus
        // fstat avoids accidentally replacing an existing recording or following a symlink.
        let flags = O_WRONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC | (create ? O_CREAT : 0)
        let fd = open(path, flags, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        var info = stat()
        guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_size == 0 else {
            close(fd)
            throw CaptureError("Choose an empty recording file.")
        }
        file = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        try checkpoint()
    }

    func append(_ samples: [Int16]) throws {
        guard !samples.isEmpty else { return }
        try samples.withUnsafeBytes { try file.write(contentsOf: $0) }
        frames += samples.count
        if frames - lastHeader >= Timeline.rate { try checkpoint() }
    }

    private func checkpoint() throws {
        var header = Data()
        func tag(_ string: String) { header.append(contentsOf: string.utf8) }
        func number<T: FixedWidthInteger>(_ value: T) {
            var le = value.littleEndian
            withUnsafeBytes(of: &le) { header.append(contentsOf: $0) }
        }
        let bytes = UInt32(frames * 2)
        tag("RIFF"); number(bytes + 36); tag("WAVE"); tag("fmt "); number(UInt32(16))
        number(UInt16(1)); number(UInt16(1)); number(UInt32(Timeline.rate))
        number(UInt32(Timeline.rate * 2)); number(UInt16(2)); number(UInt16(16))
        tag("data"); number(bytes)
        let count = header.withUnsafeBytes { pwrite(file.fileDescriptor, $0.baseAddress, $0.count, 0) }
        guard count == 44 else { throw CaptureError("Could not checkpoint the recording header.") }
        if frames == 0 { try file.seek(toOffset: 44) }
        lastHeader = frames
    }

    func finish() throws {
        // Discard any tail from a partially failed write before advertising the final length.
        try file.truncate(atOffset: UInt64(44 + frames * 2))
        try checkpoint()
        try file.synchronize()
        try file.close()
    }
}

/// Writes a second view of the microphone timeline without making the audio tap
/// wait for disk. Discarded packets leave silence at the corresponding times.
final class AnnotationNarration: @unchecked Sendable {
    private let start: Double
    private let writer = DispatchQueue(label: "jaro.annotation.narration", qos: .utility)
    private let slots = DispatchSemaphore(value: 24)
    private let sink: WaveSink
    private var timeline = Timeline(mode: .mic)
    private var failure: Error?

    init(path: String, start: Double) throws {
        self.start = start
        self.sink = try WaveSink(path: path)
    }

    func enqueue(_ samples: [Float], at time: Double) {
        guard time.isFinite, slots.wait(timeout: .now()) == .success else { return }
        writer.async { [self] in
            defer { slots.signal() }
            guard failure == nil else { return }
            let frame = frames(at: time)
            guard frame < Timeline.limit else { return }
            timeline.add(samples, at: frame, from: .mic)
            // Leave room to align adjacent packets and trim the display's tail.
            do { try flush(through: frame - Timeline.rate / 4) }
            catch { failure = error }
        }
    }

    private func frames(at time: Double) -> Int {
        Int(max(-Double(Timeline.limit), min(Double(Timeline.limit), (time - start) * Double(Timeline.rate))).rounded())
    }

    private func flush(through frame: Int) throws {
        while timeline.cursor < frame {
            let next = min(frame, timeline.cursor + Timeline.rate)
            try sink.append(timeline.render(through: next))
        }
    }

    func finish(at end: Double?) async throws {
        try await withCheckedThrowingContinuation { (done: CheckedContinuation<Void, Error>) in
            writer.async { [self] in
                do {
                    if let failure { throw failure }
                    if let end { try flush(through: max(1, frames(at: end))) }
                    try sink.finish()
                    done.resume()
                } catch {
                    done.resume(throwing: error)
                }
            }
        }
    }
}
