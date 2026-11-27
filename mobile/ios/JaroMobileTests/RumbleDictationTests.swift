#if JARO_RUMBLE
import Foundation
import XCTest
@testable import Jaro

final class RumbleDictationTests: XCTestCase {
    func testReadyExperimentHandsKeyboardDictationToTheApp() throws {
        let suite = "ai.jard.jaro.tests.rumble.\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let settings = RumbleDictationSettings(defaults: defaults)
        let keyboard = KeyboardSelfDictation(defaults: defaults)
        XCTAssertTrue(settings.isEnabled, "Rumble2 Flash is enabled by default on supported builds")
        XCTAssertFalse(settings.routesToApp)
        XCTAssertNil(keyboard.handoffReason(osMajorVersion: 26))
        settings.isModelReady = true
        XCTAssertEqual(keyboard.handoffReason(osMajorVersion: 26), .rumble)
        keyboard.recordHandedToHex(.rumble)
        settings.isEnabled = false
        XCTAssertFalse(settings.isEnabled)
        XCTAssertNil(keyboard.handoffReason(osMajorVersion: 26))
    }

    func testUnsuitableCaptureLeavesTheExistingEngineAvailable() async {
        let missing = URL(fileURLWithPath: "/missing.wav")
        let engine = RumbleTranscriber.shared
        let long = await engine.transcribe(missing, duration: 11, model: RumbleModel.fileURL)
        let unreadable = await engine.transcribe(missing, duration: 4, model: RumbleModel.fileURL)
        XCTAssertNil(long)
        XCTAssertNil(unreadable)
    }

    func testCorruptSameSizeWeightsAreRejectedRemovedAndClearReadiness() throws {
        let suite = "ai.jard.jaro.tests.rumble.corrupt.\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }

        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("rumble-corrupt-\(UUID())", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        let corrupt = dir.appendingPathComponent(RumbleModel.file.path)
        FileManager.default.createFile(atPath: corrupt.path, contents: nil)
        let handle = try FileHandle(forWritingTo: corrupt)
        try handle.write(contentsOf: Data("GGUF".utf8))
        try handle.truncate(atOffset: UInt64(RumbleModel.file.size))
        try handle.close()
        SpeechModelStore.markComplete(dir)

        let settings = RumbleDictationSettings(defaults: defaults)
        settings.isEnabled = true
        settings.isModelReady = true

        XCTAssertNil(RumbleModel.verifiedFileURL(at: corrupt, removeCorrupt: true, defaults: defaults))
        XCTAssertFalse(FileManager.default.fileExists(atPath: corrupt.path))
        XCTAssertFalse(SpeechModelStore.isComplete(dir))
        XCTAssertFalse(settings.isModelReady)
    }

    func testNativeDetectedLanguageOverridesShortTextAmbiguity() {
        XCTAssertEqual(RumbleTranscriber.languageCode(forDetectedName: "Spanish", transcript: "No"), "es")
        XCTAssertEqual(RumbleTranscriber.languageCode(forDetectedName: "French", transcript: "Oui"), "fr")
        XCTAssertEqual(RumbleTranscriber.languageCode(forDetectedName: "Norwegian Bokmål", transcript: "Hei"), "nb")
        XCTAssertTrue(AcknowledgementsView.shown.contains(where: { $0.title == "Rumble-2-Flash" }))
        XCTAssertTrue(AcknowledgementsView.shown.contains(where: { $0.title == "CrispASR and ggml" }))
    }

    func testPinnedModelRecognizesTwoLanguagesOffline() async throws {
        guard let paths = ProcessInfo.processInfo.environment["JARO_RUMBLE_FIXTURES"] else {
            throw XCTSkip("Set JARO_RUMBLE_FIXTURES to the pinned model and the 16 kHz es.wav and fr.wav directory.")
        }
        let fixture = URL(fileURLWithPath: paths, isDirectory: true)
        let model = fixture.appendingPathComponent(RumbleModel.file.path)
        let expected: [(String, String)] = [("es", "cuaderno rojo"), ("fr", "cahier rouge")]
        for (file, expectedText) in expected {
            let result = await RumbleTranscriber.shared.transcribeDetected(
                fixture.appendingPathComponent(file + ".wav"), duration: 5, model: model
            )
            XCTAssertTrue(result?.text.localizedCaseInsensitiveContains(expectedText) == true, result?.text ?? "No transcript")
            XCTAssertEqual(result?.language, file)
        }
        await RumbleTranscriber.shared.release()
    }
}
#endif
