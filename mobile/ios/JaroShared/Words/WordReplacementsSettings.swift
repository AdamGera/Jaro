import Foundation

/// Word replacements (Settings › Dictation › Custom vocabulary): heard words and what to write
/// instead, in the shared App Group so in-keyboard dictation applies them too. They apply only
/// while Custom vocabulary is on, before the learned dictation fixes, and win over a learned fix
/// with the same heard words.
nonisolated struct WordReplacementsSettings {
    static let maxCount = 1000
    /// Heard phrases match up to 8 words, the words file's limit.
    static let maxWords = JaroWordsReader.Limit.words

    private enum Key {
        static let entries = "wordReplacements.entries"
    }

    private let defaults: UserDefaults?

    init(defaults: UserDefaults? = UserDefaults(suiteName: KeyboardBridge.appGroup)) {
        self.defaults = defaults
    }

    /// The list in the order it was added, cleaned and within the words file's limits.
    var entries: [WordReplacement] {
        get {
            let stored = defaults?.array(forKey: Key.entries) as? [[String: String]] ?? []
            return Self.sanitize(stored.compactMap { item in
                guard let heard = item["heard"], let write = item["write"] else { return nil }
                return WordReplacement(heard: heard, write: write)
            })
        }
        nonmutating set {
            defaults?.set(Self.sanitize(newValue).map { ["heard": $0.heard, "write": $0.write] }, forKey: Key.entries)
        }
    }

    /// Empty while Custom vocabulary is off, so dictation stays exactly as it is.
    var activeCorrections: [DictationCorrection] {
        guard CustomVocabularySettings(defaults: defaults).isEnabled else { return [] }
        return entries.map { DictationCorrection(heard: $0.heard, meant: $0.write) }
    }

    enum Problem: Equatable {
        case noLetters, hiddenCharacters, tooLong, tooManyWords, emptyWrite, duplicate, full
    }

    /// Why `heard` → `write` can't join `existing`, by the words file's rules; empty or blank
    /// outputs aren't allowed on the iPhone yet.
    static func problem(heard: String, write: String, existing: [WordReplacement]) -> Problem? {
        let heard = JaroWordsText.cleanLine(heard)
        let write = JaroWordsText.cleanWrite(write)
        if existing.count >= maxCount { return .full }
        if JaroWordsText.isBlank(write) { return .emptyWrite }
        switch JaroWordsReader.replacementProblem(heard: heard, write: write) {
        case .hiddenCharacters: return .hiddenCharacters
        case .noLetters: return .noLetters
        case .tooLong: return .tooLong
        case .tooManyWords: return .tooManyWords
        default: break
        }
        let key = JaroWordsText.key(heard)
        if existing.contains(where: { JaroWordsText.key($0.heard) == key }) { return .duplicate }
        return nil
    }

    /// Cleans each entry and drops any the editor would refuse.
    static func sanitize(_ entries: [WordReplacement]) -> [WordReplacement] {
        var result: [WordReplacement] = []
        var keys: Set<String> = []
        for entry in entries {
            guard result.count < maxCount else { break }
            let heard = JaroWordsText.cleanLine(entry.heard)
            let write = JaroWordsText.cleanWrite(entry.write)
            guard !JaroWordsText.isBlank(write),
                  JaroWordsReader.replacementProblem(heard: heard, write: write) == nil,
                  keys.insert(JaroWordsText.key(heard)).inserted else { continue }
            result.append(WordReplacement(heard: heard, write: write))
        }
        return result
    }
}
