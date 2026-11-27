import Foundation
import XCTest
@testable import Jaro

final class JaroWordsTests: XCTestCase {
    func testMobileFileRoundTripsVocabularyReplacementsAndLearnedFixes() throws {
        let written = JaroWordsWriter.phone(
            vocabulary: ["Åland", "Žilina"],
            replacements: [WordReplacement(heard: "prater rose", write: "Prater Rose")],
            learnedFixes: [
                .init(heard: "jean", meant: "Jeanne", active: true),
                .init(heard: "john", meant: "Jon", active: false),
            ],
            includeLearned: true,
            appVersion: "1.0"
        )
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: written.data) as? [String: Any])
        XCTAssertEqual(json["format"] as? String, "jaro.words")
        XCTAssertEqual(json["version"] as? Int, 1)
        guard case .read(let restored) = JaroWordsReader.read(written.data) else {
            return XCTFail("The reader refused its own export.")
        }
        XCTAssertEqual(restored.vocabulary.map(\.term), ["Åland", "Žilina"])
        XCTAssertEqual(restored.replacements, [
            .init(heard: "prater rose", write: "Prater Rose"),
            .init(heard: "jean", write: "Jeanne", learned: true),
        ])
        XCTAssertTrue(restored.skipped.isEmpty)
    }

    func testImportWaitsForReviewKeepsExistingRulesAndCanBeUndone() throws {
        let suite = "ai.jard.jaro.tests.words.\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = JaroWordsImport(defaults: defaults)
        let vocabulary = CustomVocabularySettings(defaults: defaults)
        let replacements = WordReplacementsSettings(defaults: defaults)
        vocabulary.terms = ["Jaro"]
        replacements.entries = [.init(heard: "lean in", write: "Lean In")]
        let file = JaroWordsFile(
            vocabulary: [.init(term: "Zoë", soundsLike: ["zoh ee"])],
            replacements: [.init(heard: "lean in", write: "Linen"), .init(heard: "skip me", write: "")]
        )
        let before = store.lists
        let preview = store.preview(file)
        XCTAssertEqual(store.lists, before)
        XCTAssertFalse(vocabulary.isEnabled)
        XCTAssertEqual(preview.summary.addedTerms, 1)
        XCTAssertEqual(preview.summary.addedReplacements, 1)
        XCTAssertEqual(preview.summary.keptYours, 1)
        XCTAssertEqual(preview.notApplied.first?.reason, .deletionNotSupported)
        let conflict = try XCTUnwrap(preview.clashes.first)
        store.add(file, useTheirs: [conflict.path], turnOnCustomVocabulary: true)
        XCTAssertEqual(store.lists.vocabulary, ["Jaro", "Zoë"])
        XCTAssertEqual(replacements.entries, [
            .init(heard: "lean in", write: "Linen"),
            .init(heard: "zoh ee", write: "Zoë"),
        ])
        XCTAssertTrue(vocabulary.isEnabled)
        XCTAssertTrue(store.undo())
        XCTAssertEqual(store.lists, before)
        XCTAssertFalse(vocabulary.isEnabled)
        XCTAssertNil(store.availableUndo)
    }

    func testReaderRejectsDuplicateKeysAndUnrelatedFiles() {
        let duplicate = Data(#"{"format":"jaro.words","version":1,"version":2}"#.utf8)
        let unrelated = Data(#"{"format":"clipboard","version":1}"#.utf8)
        XCTAssertEqual(JaroWordsReader.read(duplicate), .refused(.duplicateKey))
        XCTAssertEqual(JaroWordsReader.read(unrelated), .refused(.notJaroWords))
    }

    @MainActor
    func testSnapshotOnlyAppliesEnabledReplacementsAheadOfLearnedFixes() async throws {
        let suite = "ai.jard.jaro.tests.words.\(UUID())"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let vocabulary = CustomVocabularySettings(defaults: defaults)
        let replacements = WordReplacementsSettings(defaults: defaults)
        replacements.entries = [.init(heard: "lean in", write: "Linen")]
        let learned = [DictationCorrection(heard: "lean in", meant: "LeanIn")]
        let off = DictationPipeline.Preferences.current(
            style: .verbatim, vocabulary: vocabulary, corrections: learned, replacements: replacements
        )
        XCTAssertTrue(off.replacements.isEmpty)
        vocabulary.isEnabled = true
        let on = DictationPipeline.Preferences.current(
            style: .verbatim, vocabulary: vocabulary, corrections: learned, replacements: replacements
        )
        vocabulary.isEnabled = false
        let steps = await DictationPipeline(preferences: on, language: "en-US")
            .run("I said lean in, and lean inside.")
        XCTAssertEqual(steps.fixed, "I said Linen, and lean inside.")
    }
}
