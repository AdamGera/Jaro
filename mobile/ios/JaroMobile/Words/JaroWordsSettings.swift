import SwiftUI
import UniformTypeIdentifiers

/// Custom vocabulary's own rows: Word replacements and Words file while it's on, and the last
/// import's Undo while there is one.
struct JaroWordsRows: View {
    let isOn: Bool

    @State private var count = WordReplacementsSettings().entries.count
    @State private var canUndo = JaroWordsImport().availableUndo != nil
    @State private var showsReplacements = false
    @State private var showsFile = false

    var body: some View {
        VStack(spacing: 0) {
            if isOn {
                SettingsDivider()
                row(
                    String(localized: "Word replacements"),
                    count == 0 ? String(localized: "Words Jaro hears, and what to write instead") : JaroWordsReviewSheet.replacements(count)
                ) { showsReplacements = true }
                SettingsDivider()
                row(String(localized: "Words file"), String(localized: "Open, share or save your words")) { showsFile = true }
            }
            if canUndo {
                SettingsDivider()
                JaroWordsUndoRow()
            }
        }
        .navigationDestination(isPresented: $showsReplacements) {
            SettingsPageScaffold(title: "Word replacements") { WordReplacementsPage() }
        }
        .navigationDestination(isPresented: $showsFile) {
            SettingsPageScaffold(title: "Words file") { WordsFilePage() }
        }
        .onAppear(perform: reload)
        .onChange(of: showsReplacements) { _, _ in reload() }
        .onChange(of: showsFile) { _, _ in reload() }
        .onReceive(NotificationCenter.default.publisher(for: .jaroWordsListsDidChange)) { _ in reload() }
    }

    private func reload() {
        count = WordReplacementsSettings().entries.count
        canUndo = JaroWordsImport().availableUndo != nil
    }

    private func row(_ title: String, _ detail: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 12) {
                SettingsRowLabel(title: title, detail: detail)
                Spacer(minLength: 8)
                Image(systemName: "chevron.right")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(JaroTheme.textMuted)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .frame(minHeight: 50)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

/// Undo last import: puts both lists back as the last import found them.
struct JaroWordsUndoRow: View {
    var body: some View {
        Button {
            JaroWordsImport().undo()
            NotificationCenter.default.post(name: .jaroWordsListsDidChange, object: nil)
        } label: {
            HStack {
                SettingsRowLabel(title: String(localized: "Undo last import"))
                Spacer(minLength: 8)
            }
            .padding(.horizontal, 16)
            .frame(minHeight: 50)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

/// Settings › Dictation › Custom vocabulary › Word replacements: add and remove the person's
/// own entries. Any edit ends the last import's Undo.
struct WordReplacementsPage: View {
    private let settings = WordReplacementsSettings()

    @State private var entries: [WordReplacement] = []
    @State private var heard = ""
    @State private var write = ""
    @State private var problem: WordReplacementsSettings.Problem?

    var body: some View {
        Text(String(localized: "Jaro writes these instead of the words it hears, matching whole words, up to 8. Yours win over fixes Jaro learned."))
            .font(.system(size: 14))
            .foregroundStyle(JaroTheme.textMuted)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 16)

        SettingsCard {
            field(String(localized: "Words Jaro hears"), text: $heard, axis: .horizontal)
            SettingsDivider()
            field(String(localized: "What to write"), text: $write, axis: .vertical)
            SettingsDivider()
            HStack(spacing: 8) {
                Text(problem.map(Self.text(for:)) ?? "")
                    .font(.system(size: 13))
                    .foregroundStyle(JaroTheme.textMuted)
                    .lineLimit(3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button(String(localized: "Add"), action: add)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(JaroTheme.textBase)
                    .padding(.horizontal, 12)
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                    .buttonStyle(.plain)
            }
            .padding(.leading, 16)
            .padding(.trailing, 4)
            .frame(minHeight: 52)
        }

        if !entries.isEmpty {
            SettingsCard {
                ForEach(Array(entries.enumerated()), id: \.element) { index, entry in
                    if index > 0 { SettingsDivider() }
                    HStack(spacing: 10) {
                        Text(verbatim: entry.heard + " → " + entry.write)
                            .font(.system(size: 15))
                            .foregroundStyle(JaroTheme.textBase)
                            .lineLimit(3)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        Button {
                            settings.entries = entries.filter { $0 != entry }
                            reload()
                        } label: {
                            Image(systemName: "xmark")
                                .font(.system(size: 11, weight: .semibold))
                                .foregroundStyle(JaroTheme.textMuted)
                                .frame(width: 26, height: 26)
                                .background(JaroTheme.layer02, in: Circle())
                                .frame(width: 44, height: 44)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(String(localized: "Remove"))
                    }
                    .padding(.leading, 16)
                    .padding(.trailing, 4)
                    .frame(minHeight: 52)
                }
            }
        }

        Color.clear.frame(height: 0)
            .onAppear(perform: reload)
    }

    private func field(_ prompt: String, text: Binding<String>, axis: Axis) -> some View {
        TextField(prompt, text: text, axis: axis)
            .lineLimit(axis == .vertical ? 1...4 : 1...1)
            .font(.system(size: 16))
            .foregroundStyle(JaroTheme.textBase)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .onChange(of: text.wrappedValue) { _, _ in problem = nil }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
    }

    private func reload() {
        entries = settings.entries
    }

    private func add() {
        if let found = WordReplacementsSettings.problem(heard: heard, write: write, existing: entries) {
            problem = found
            return
        }
        settings.entries = entries + [WordReplacement(heard: heard, write: write)]
        heard = ""
        write = ""
        reload()
    }

    static func text(for problem: WordReplacementsSettings.Problem) -> String {
        switch problem {
        case .noLetters: String(localized: "The words Jaro hears need a letter or digit.")
        case .hiddenCharacters: String(localized: "This has a hidden character in it.")
        case .tooLong: String(localized: "Too long: up to 128 characters heard and 1,000 written.")
        case .tooManyWords: String(localized: "Up to 8 words heard.")
        case .emptyWrite: String(localized: "Write something. Removing words isn’t supported yet.")
        case .duplicate: String(localized: "You already replace these words.")
        case .full: String(localized: "The list holds 1,000 replacements.")
        }
    }
}

/// Settings › Dictation › Custom vocabulary › Words file: open one from Files, or share the
/// terms and Word replacements as `jaro-words.jarowords` (the share sheet's Save to Files saves
/// it), with the learned fixes in use only when the person includes them.
struct WordsFilePage: View {
    @State private var includesLearned = false
    @State private var status: String?
    @State private var opens = false
    @State private var learnedInUse = false
    @State private var canUndo = false

    var body: some View {
        SettingsCard {
            Button {
                opens = true
            } label: {
                HStack(spacing: 12) {
                    SettingsRowLabel(
                        title: String(localized: "Open a words file…"),
                        detail: String(localized: "Check what a .jarowords file adds before anything changes")
                    )
                    Spacer(minLength: 8)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(JaroTheme.textMuted)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .frame(minHeight: 50)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }

        VStack(alignment: .leading, spacing: 8) {
            Text(String(localized: "Export"))
                .font(.system(size: 14))
                .foregroundStyle(JaroTheme.textMuted)
                .padding(.leading, 16)
            SettingsCard {
                if learnedInUse {
                    SettingsToggleRow(
                        title: String(localized: "Include fixes Jaro learned"),
                        detail: String(localized: "Adds the learned fixes in use, marked as learned"),
                        isOn: $includesLearned
                    )
                    SettingsDivider()
                }
                Button(action: share) {
                    SettingsLinkLabel(title: String(localized: "Share…"), trailingIcon: "square.and.arrow.up")
                }
                .buttonStyle(.plain)
            }
            Text(status ?? String(localized: "Share your Custom vocabulary and Word replacements with Jaro on iPhone, iPad, or Android."))
                .font(.system(size: 13))
                .foregroundStyle(JaroTheme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 16)
        }

        if canUndo {
            SettingsCard { JaroWordsUndoRow() }
        }

        Color.clear.frame(height: 0)
            .onAppear(perform: reload)
            .onReceive(NotificationCenter.default.publisher(for: .jaroWordsListsDidChange)) { _ in reload() }
            .fileImporter(isPresented: $opens, allowedContentTypes: [.data]) { result in
                switch result {
                case .success(let url):
                    JaroWordsInbox.review(url, removeInboxCopy: false)
                case .failure:
                    status = String(localized: "Jaro couldn’t open that file.")
                }
            }
    }

    private func reload() {
        learnedInUse = Self.learnedFixes().contains(where: \.active)
        canUndo = JaroWordsImport().availableUndo != nil
    }

    private func share() {
        let written = Self.write(includeLearned: includesLearned && learnedInUse)
        guard let url = try? JaroWordsWriter.temporaryFile(written.data) else {
            status = String(localized: "Jaro couldn’t write the file.")
            return
        }
        ShareSheet.present([url]) {
            try? FileManager.default.removeItem(at: url.deletingLastPathComponent())
        }
        status = Self.exported(written)
    }

    static func write(includeLearned: Bool) -> JaroWordsWriter.Output {
        JaroWordsWriter.phone(
            vocabulary: CustomVocabularySettings().terms,
            replacements: WordReplacementsSettings().entries,
            learnedFixes: learnedFixes(),
            includeLearned: includeLearned,
            appVersion: JaroWordsWriter.appVersion()
        )
    }

    static func exported(_ written: JaroWordsWriter.Output) -> String {
        let counts = String(localized: "Exported \(JaroWordsReviewSheet.terms(written.terms)) and \(JaroWordsReviewSheet.replacements(written.replacements)).")
        guard !written.notExported.isEmpty else { return counts }
        var reasons: [JaroWordsSkipReason] = []
        for skip in written.notExported where !reasons.contains(skip.reason) { reasons.append(skip.reason) }
        let why = reasons.map(JaroWordsReviewSheet.skipped).joined(separator: ", ")
        return counts + " " + String(localized: "Left out \(written.notExported.count): \(why).")
    }

    /// Learn how I type's learned dictation fixes, each in use unless switched off in Typing
    /// insights; none while Learn how I type is off, since only then are they applied.
    static func learnedFixes(settings: TypingJournalSettings = TypingJournalSettings()) -> [JaroWordsWriter.LearnedFix] {
        guard settings.isEnabled else { return [] }
        let folder = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: KeyboardBridge.appGroup)
        let snapshot = TypingJournal.load(from: folder?.appendingPathComponent(TypingJournal.fileName))
        let fixes = settings.learnsPerLanguage ? snapshot.learnedDictationFixesByLanguage() : snapshot.learnedDictationFixes
        let disabled = settings.disabledDictationFixes
        return fixes.map { JaroWordsWriter.LearnedFix(heard: $0.heard, meant: $0.meant, active: !disabled.contains($0.id)) }
    }
}
