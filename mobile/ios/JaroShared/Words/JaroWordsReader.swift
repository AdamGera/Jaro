import CoreFoundation
import Foundation

/// Why a file was refused; nothing of it is kept or shown entry by entry.
nonisolated enum JaroWordsRefusal: String, Sendable, CaseIterable {
    case tooLarge, notUTF8, empty, tooDeep, duplicateKey, notJSON, notAnObject, notJaroWords, badVersion
}

/// Why one part of a file that was read was left out.
nonisolated enum JaroWordsSkipReason: String, Sendable, CaseIterable {
    case badPart, badEntry, badField, unknownField, hiddenCharacters, noLetters, tooLong, tooManyWords,
         duplicate, sameAsTerm, overLimit
}

/// A place in a file (`vocabulary[3].soundsLike[0]`) and why it was skipped.
nonisolated struct JaroWordsSkip: Hashable, Sendable {
    var path: String
    var reason: JaroWordsSkipReason
}

nonisolated struct JaroWordsTerm: Equatable, Sendable {
    var term: String
    var soundsLike: [String] = []
}

nonisolated struct JaroWordsReplacement: Equatable, Sendable {
    var heard: String
    var write: String
    var learned = false
}

/// A file that was read: what it holds, cleaned, and what was skipped.
nonisolated struct JaroWordsFile: Equatable, Sendable {
    var version = 1
    /// `madeBy`: the file's claim, shown, never trusted.
    var madeByApp: String?
    var madeByVersion: String?
    var vocabulary: [JaroWordsTerm] = []
    var replacements: [JaroWordsReplacement] = []
    var skipped: [JaroWordsSkip] = []

    var isNewerVersion: Bool { version > JaroWordsReader.version }
}

nonisolated enum JaroWordsReadResult: Equatable, Sendable {
    case refused(JaroWordsRefusal)
    case read(JaroWordsFile)
}

/// Reads a Jaro words file (`.jarowords`) as version 1 specifies: refuse a file
/// that isn't one, otherwise read what it can and skip each entry that breaks a rule.
nonisolated enum JaroWordsReader {
    static let version = 1
    static let format = "jaro.words"
    static let maxBytes = 8 * 1024 * 1024

    enum Limit {
        static let terms = 500
        static let termLength = 64
        static let spellings = 16
        static let spellingLength = 64
        static let words = 8
        static let replacements = 1000
        static let heardLength = 128
        static let writeLength = 1000
        static let appLength = 64
        static let appVersionLength = 32
    }

    /// Reads at most 8 MiB + 1 byte, refusing anything larger before reading it; nil when the
    /// file can't be opened or read at all.
    static func read(contentsOf url: URL) -> JaroWordsReadResult? {
        if let size = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > maxBytes {
            return .refused(.tooLarge)
        }
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        guard let data = try? handle.read(upToCount: maxBytes + 1) else { return read(Data()) }
        return read(data)
    }

    static func read(_ data: Data) -> JaroWordsReadResult {
        guard data.count <= maxBytes else { return .refused(.tooLarge) }
        var bytes = [UInt8](data)
        if bytes.starts(with: [0xEF, 0xBB, 0xBF]) { bytes.removeFirst(3) }
        // Swift's decoder repairs any bad byte, overlong form or encoded surrogate, so the bytes
        // come back unchanged only when they were valid UTF-8.
        guard Array(String(decoding: bytes, as: UTF8.self).utf8) == bytes else { return .refused(.notUTF8) }
        guard bytes.contains(where: { $0 != 0x20 && $0 != 0x09 && $0 != 0x0A && $0 != 0x0D }) else {
            return .refused(.empty)
        }
        if let refusal = JaroWordsCheck.check(bytes) { return .refused(refusal) }
        guard let root = try? JSONSerialization.jsonObject(with: Data(bytes), options: [.fragmentsAllowed]) else {
            return .refused(.notJSON)
        }
        // NSDictionary, never [String: Any]: two keys that differ only in normalization are two
        // keys here, and bridging them into one Swift dictionary would trap.
        guard let object = root as? NSDictionary else { return .refused(.notAnObject) }
        guard let format = object.value(for: "format") as? String, format == Self.format,
              format.utf8.elementsEqual(Self.format.utf8) else {
            return .refused(.notJaroWords)
        }
        guard let version = wholeNumber(object.value(for: "version")), version >= 1 else {
            return .refused(.badVersion)
        }

        var file = JaroWordsFile(version: version)
        var skipped: [JaroWordsSkip] = []
        let known: Set<String> = ["format", "version", "madeBy", "vocabulary", "replacements"]
        for key in object.stringKeys where !known.contains(key) {
            skipped.append(JaroWordsSkip(path: key, reason: .unknownField))
        }
        if let madeBy = object.value(for: "madeBy") {
            readMadeBy(madeBy, into: &file, skipped: &skipped)
        }
        if let vocabulary = object.value(for: "vocabulary") {
            if let entries = vocabulary as? NSArray {
                file.vocabulary = readVocabulary(entries, skipped: &skipped)
            } else {
                skipped.append(JaroWordsSkip(path: "vocabulary", reason: .badPart))
            }
        }
        if let replacements = object.value(for: "replacements") {
            if let entries = replacements as? NSArray {
                file.replacements = readReplacements(entries, skipped: &skipped)
            } else {
                skipped.append(JaroWordsSkip(path: "replacements", reason: .badPart))
            }
        }
        file.skipped = skipped
        return .read(file)
    }

    // MARK: Entry rules, shared with the writer

    /// The first rule a term breaks, after cleaning.
    static func termProblem(_ term: String) -> JaroWordsSkipReason? {
        if JaroWordsText.hasHidden(term) { return .hiddenCharacters }
        if !JaroWordsText.hasLetterOrDigit(term) { return .noLetters }
        if JaroWordsText.length(term) > Limit.termLength { return .tooLong }
        return nil
    }

    static func spellingProblem(_ spelling: String) -> JaroWordsSkipReason? {
        if JaroWordsText.hasHidden(spelling) { return .hiddenCharacters }
        if !JaroWordsText.hasLetterOrDigit(spelling) { return .noLetters }
        if JaroWordsText.length(spelling) > Limit.spellingLength { return .tooLong }
        if JaroWordsText.wordCount(spelling) > Limit.words { return .tooManyWords }
        return nil
    }

    /// The first rule a replacement breaks, after cleaning `heard` as a line and `write` as an output.
    static func replacementProblem(heard: String, write: String) -> JaroWordsSkipReason? {
        if JaroWordsText.hasHidden(heard) || JaroWordsText.hasHidden(write, inWrite: true) { return .hiddenCharacters }
        if !JaroWordsText.hasLetterOrDigit(heard) { return .noLetters }
        if JaroWordsText.length(heard) > Limit.heardLength || JaroWordsText.length(write) > Limit.writeLength {
            return .tooLong
        }
        if JaroWordsText.wordCount(heard) > Limit.words { return .tooManyWords }
        return nil
    }

    // MARK: Parts

    private static func readMadeBy(_ value: Any, into file: inout JaroWordsFile, skipped: inout [JaroWordsSkip]) {
        guard let madeBy = value as? NSDictionary else {
            skipped.append(JaroWordsSkip(path: "madeBy", reason: .badField))
            return
        }
        for key in madeBy.stringKeys {
            let limit: Int
            switch key {
            case "app": limit = Limit.appLength
            case "version": limit = Limit.appVersionLength
            default:
                skipped.append(JaroWordsSkip(path: "madeBy." + key, reason: .unknownField))
                continue
            }
            guard let raw = madeBy.value(for: key) as? String else {
                skipped.append(JaroWordsSkip(path: "madeBy." + key, reason: .badField))
                continue
            }
            let text = JaroWordsText.cleanLine(raw)
            guard !text.isEmpty, !JaroWordsText.hasHidden(text), JaroWordsText.length(text) <= limit else {
                skipped.append(JaroWordsSkip(path: "madeBy." + key, reason: .badField))
                continue
            }
            if key == "app" { file.madeByApp = text } else { file.madeByVersion = text }
        }
    }

    private static func readVocabulary(_ entries: NSArray, skipped: inout [JaroWordsSkip]) -> [JaroWordsTerm] {
        var terms: [JaroWordsTerm] = []
        var keys: Set<String> = []
        for (index, value) in entries.enumerated() {
            let path = "vocabulary[\(index)]"
            func skip(_ reason: JaroWordsSkipReason) { skipped.append(JaroWordsSkip(path: path, reason: reason)) }
            guard terms.count < Limit.terms else { skip(.overLimit); continue }
            guard let entry = value as? NSDictionary, let raw = entry.value(for: "term") as? String else {
                skip(.badEntry)
                continue
            }
            let term = JaroWordsText.cleanLine(raw)
            if let problem = termProblem(term) { skip(problem); continue }
            let key = JaroWordsText.key(term)
            guard keys.insert(key).inserted else { skip(.duplicate); continue }

            var accepted = JaroWordsTerm(term: term)
            for field in entry.stringKeys where field != "term" && field != "soundsLike" {
                skipped.append(JaroWordsSkip(path: path + "." + field, reason: .unknownField))
            }
            if let soundsLike = entry.value(for: "soundsLike") {
                if let spellings = soundsLike as? NSArray {
                    accepted.soundsLike = readSpellings(spellings, termKey: key, path: path, skipped: &skipped)
                } else {
                    skipped.append(JaroWordsSkip(path: path + ".soundsLike", reason: .badField))
                }
            }
            terms.append(accepted)
        }
        return terms
    }

    private static func readSpellings(_ values: NSArray, termKey: String, path: String, skipped: inout [JaroWordsSkip]) -> [String] {
        var spellings: [String] = []
        var keys: Set<String> = []
        for (index, value) in values.enumerated() {
            let place = path + ".soundsLike[\(index)]"
            func skip(_ reason: JaroWordsSkipReason) { skipped.append(JaroWordsSkip(path: place, reason: reason)) }
            guard spellings.count < Limit.spellings else { skip(.overLimit); continue }
            guard let raw = value as? String else { skip(.badEntry); continue }
            let spelling = JaroWordsText.cleanLine(raw)
            if let problem = spellingProblem(spelling) { skip(problem); continue }
            let key = JaroWordsText.key(spelling)
            guard key != termKey else { skip(.sameAsTerm); continue }
            guard keys.insert(key).inserted else { skip(.duplicate); continue }
            spellings.append(spelling)
        }
        return spellings
    }

    private static func readReplacements(_ entries: NSArray, skipped: inout [JaroWordsSkip]) -> [JaroWordsReplacement] {
        var replacements: [JaroWordsReplacement] = []
        var keys: Set<String> = []
        for (index, value) in entries.enumerated() {
            let path = "replacements[\(index)]"
            func skip(_ reason: JaroWordsSkipReason) { skipped.append(JaroWordsSkip(path: path, reason: reason)) }
            guard replacements.count < Limit.replacements else { skip(.overLimit); continue }
            guard let entry = value as? NSDictionary,
                  let rawHeard = entry.value(for: "heard") as? String,
                  let rawWrite = entry.value(for: "write") as? String else {
                skip(.badEntry)
                continue
            }
            let heard = JaroWordsText.cleanLine(rawHeard)
            let write = JaroWordsText.cleanWrite(rawWrite)
            if let problem = replacementProblem(heard: heard, write: write) { skip(problem); continue }
            guard keys.insert(JaroWordsText.key(heard)).inserted else { skip(.duplicate); continue }

            var accepted = JaroWordsReplacement(heard: heard, write: write)
            for field in entry.stringKeys where !["heard", "write", "learned"].contains(field) {
                skipped.append(JaroWordsSkip(path: path + "." + field, reason: .unknownField))
            }
            if let learned = entry.value(for: "learned") {
                if let flag = boolean(learned) {
                    accepted.learned = flag
                } else {
                    skipped.append(JaroWordsSkip(path: path + ".learned", reason: .badField))
                }
            }
            replacements.append(accepted)
        }
        return replacements
    }

    // MARK: Values

    /// `JSONSerialization` makes booleans and numbers both `NSNumber`; only a CFBoolean is a boolean.
    private static func boolean(_ value: Any) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }

    /// A number (never a boolean) that is whole: `1.0` is 1; `"1"`, `true` and `1.5` aren't.
    private static func wholeNumber(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let double = number.doubleValue
        guard double.isFinite, double == double.rounded() else { return nil }
        return Int(exactly: double) ?? (double > 0 ? Int.max : Int.min)
    }
}

private extension NSDictionary {
    func value(for key: String) -> Any? {
        object(forKey: key as NSString)
    }

    /// Every key, as written (JSON keys are always strings).
    var stringKeys: [String] {
        allKeys.compactMap { ($0 as? NSString).map { String($0) } }
    }
}
