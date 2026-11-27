import Foundation

/// One Word replacement: when dictation hears `heard`, it writes `write`.
nonisolated struct WordReplacement: Codable, Hashable, Sendable {
    var heard: String
    var write: String
}

/// The two lists a words file changes on the iPhone: Custom vocabulary's terms and Word
/// replacements.
nonisolated struct JaroWordsLists: Codable, Equatable, Sendable {
    var vocabulary: [String] = []
    var replacements: [WordReplacement] = []
}

nonisolated struct JaroWordsSummary: Equatable, Sendable {
    var addedTerms = 0
    var updatedTerms = 0
    var alreadyHaveTerms = 0
    var addedReplacements = 0
    var changedReplacements = 0
    var keptYours = 0
    var alreadyHaveReplacements = 0

    var changesAnything: Bool {
        addedTerms + updatedTerms + addedReplacements + changedReplacements > 0
    }
}

/// Why an entry the file holds can't be used on this iPhone.
nonisolated enum JaroWordsNotAppliedReason: String, Sendable, CaseIterable {
    case tooShortForThisDevice, deletionNotSupported, overDeviceLimit, duplicate
}

nonisolated struct JaroWordsNotApplied: Hashable, Sendable {
    var path: String
    var reason: JaroWordsNotAppliedReason
}

/// A heard phrase both lists replace, with different words: yours stays unless the person
/// picks the file's.
nonisolated struct JaroWordsClash: Equatable, Identifiable, Sendable {
    var path: String
    var heard: String
    var yours: String
    var theirs: String

    var id: String { path }
}

nonisolated struct JaroWordsMergeResult: Equatable, Sendable {
    var after: JaroWordsLists
    var summary: JaroWordsSummary
    var notApplied: [JaroWordsNotApplied]
    var clashes: [JaroWordsClash]
}

/// A phone's merge : terms are added without their
/// spellings, each spelling becomes a replacement to its term after the file's own
/// replacements, and nothing is ever removed. It reads and changes only the two lists.
nonisolated enum JaroWordsMerge {
    struct Limits: Equatable, Sendable {
        var terms = CustomVocabularySettings.maxTermCount
        var replacements = WordReplacementsSettings.maxCount
    }

    struct Parts: Equatable, Sendable {
        var vocabulary = true
        var replacements = true
    }

    static func phone(
        before: JaroWordsLists,
        incoming: JaroWordsFile,
        limits: Limits = Limits(),
        parts: Parts = Parts(),
        useTheirs: Set<String> = []
    ) -> JaroWordsMergeResult {
        var summary = JaroWordsSummary()
        var notApplied: [JaroWordsNotApplied] = []
        var clashes: [JaroWordsClash] = []
        var vocabulary = before.vocabulary
        var replacements = before.replacements

        var queue: [(path: String, heard: String, write: String)] = []
        if parts.replacements {
            for (index, replacement) in incoming.replacements.enumerated() {
                queue.append(("replacements[\(index)]", replacement.heard, replacement.write))
            }
        }
        if parts.vocabulary {
            var termKeys = Set(vocabulary.map(JaroWordsText.phoneTermKey))
            for (index, entry) in incoming.vocabulary.enumerated() {
                let path = "vocabulary[\(index)]"
                let key = JaroWordsText.phoneTermKey(entry.term)
                // The phone's own list judges a term by its letters and digits too
                // (`CustomVocabularySettings.sanitize`), so both must allow it.
                if key.unicodeScalars.count < 2 || VocabularySpelling.normalizedKey(entry.term).count < 2 {
                    notApplied.append(JaroWordsNotApplied(path: path, reason: .tooShortForThisDevice))
                } else if termKeys.contains(key) {
                    summary.alreadyHaveTerms += 1
                } else if vocabulary.count >= limits.terms {
                    notApplied.append(JaroWordsNotApplied(path: path, reason: .overDeviceLimit))
                } else {
                    vocabulary.append(entry.term)
                    termKeys.insert(key)
                    summary.addedTerms += 1
                }
            }
        }
        if parts.replacements {
            for (index, entry) in incoming.vocabulary.enumerated() {
                for (spellingIndex, spelling) in entry.soundsLike.enumerated() {
                    queue.append(("vocabulary[\(index)].soundsLike[\(spellingIndex)]", spelling, entry.term))
                }
            }
        }

        var indexByKey: [String: Int] = [:]
        for (index, replacement) in replacements.enumerated() {
            indexByKey[JaroWordsText.key(replacement.heard)] = indexByKey[JaroWordsText.key(replacement.heard)] ?? index
        }
        var seen: Set<String> = []
        for item in queue {
            if JaroWordsText.isBlank(item.write) {
                notApplied.append(JaroWordsNotApplied(path: item.path, reason: .deletionNotSupported))
                continue
            }
            let key = JaroWordsText.key(item.heard)
            guard seen.insert(key).inserted else {
                notApplied.append(JaroWordsNotApplied(path: item.path, reason: .duplicate))
                continue
            }
            if let index = indexByKey[key] {
                let yours = replacements[index].write
                if yours.precomposedStringWithCanonicalMapping.unicodeScalars.elementsEqual(item.write.unicodeScalars) {
                    summary.alreadyHaveReplacements += 1
                    continue
                }
                clashes.append(JaroWordsClash(path: item.path, heard: replacements[index].heard, yours: yours, theirs: item.write))
                if useTheirs.contains(item.path) {
                    replacements[index].write = item.write
                    summary.changedReplacements += 1
                } else {
                    summary.keptYours += 1
                }
                continue
            }
            guard replacements.count < limits.replacements else {
                notApplied.append(JaroWordsNotApplied(path: item.path, reason: .overDeviceLimit))
                continue
            }
            indexByKey[key] = replacements.count
            replacements.append(WordReplacement(heard: item.heard, write: item.write))
            summary.addedReplacements += 1
        }

        return JaroWordsMergeResult(
            after: JaroWordsLists(vocabulary: vocabulary, replacements: replacements),
            summary: summary,
            notApplied: notApplied,
            clashes: clashes
        )
    }
}
