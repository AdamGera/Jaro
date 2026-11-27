use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use regex::Regex;
use unicode_general_category::{get_general_category, GeneralCategory};
use unicode_normalization::UnicodeNormalization;

use crate::error::CliError;
use crate::paths::casefold;

pub static TOKEN_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[^\W_]+(?:['’][^\W_]+)*").unwrap());

static STOP: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
    "a about an and are as at be but by did do for from had has have he i in is it its of on or our she so that the their them there they this to was we were what when where which who with you"
        .split_whitespace()
        .collect()
});

const SUFFIXES: &[(&str, usize, &str)] = &[
    ("ingly", 3, ""),
    ("edly", 3, ""),
    ("ings", 3, ""),
    ("ing", 3, ""),
    ("ied", 2, "y"),
    ("ies", 2, "y"),
    ("ed", 3, ""),
    ("es", 3, ""),
    ("ly", 3, ""),
    ("s", 3, ""),
];

fn sound_code(c: u8) -> Option<u8> {
    match c {
        b'b' | b'f' | b'p' | b'v' => Some(1),
        b'c' | b'g' | b'j' | b'k' | b'q' | b's' | b'x' | b'z' => Some(2),
        b'd' | b't' => Some(3),
        b'l' => Some(4),
        b'm' | b'n' => Some(5),
        b'r' => Some(6),
        _ => None,
    }
}

fn is_combining_mark(c: char) -> bool {
    matches!(
        get_general_category(c),
        GeneralCategory::NonspacingMark
            | GeneralCategory::SpacingMark
            | GeneralCategory::EnclosingMark
    )
}

pub fn fold(word: &str) -> String {
    let folded = casefold(word);
    folded
        .nfkd()
        .filter(|&c| !is_combining_mark(c) && c != '\'' && c != '’')
        .collect()
}

pub fn words(text: &str) -> Vec<String> {
    let nfc: String = text.nfc().collect();
    TOKEN_RE
        .find_iter(&nfc)
        .map(|m| fold(m.as_str()))
        .collect()
}

pub fn count_tokens(text: &str) -> usize {
    TOKEN_RE.find_iter(text).count()
}

pub fn stem(word: &str) -> String {
    if word.len() <= 3 || !word.is_ascii() || !word.bytes().all(|b| b.is_ascii_alphabetic()) {
        return word.to_string();
    }
    let mut w = word.to_string();
    for &(suffix, minimum, replacement) in SUFFIXES {
        if w.len() >= suffix.len() + minimum && w.ends_with(suffix) {
            if suffix != "s" || (!w.ends_with("ss") && !w.ends_with("us")) {
                w.truncate(w.len() - suffix.len());
                w.push_str(replacement);
                break;
            }
        }
    }
    let bytes = w.as_bytes();
    if bytes.len() >= 4
        && bytes[bytes.len() - 1] == bytes[bytes.len() - 2]
        && !matches!(bytes[bytes.len() - 1], b'a' | b'e' | b'i' | b'o' | b'u' | b'l' | b's' | b'z')
    {
        w.pop();
    }
    if w.len() >= 4 && w.ends_with('e') {
        w.pop();
    }
    w
}

pub fn sound_key(word: &str) -> Option<String> {
    if word.is_empty() || !word.is_ascii() || !word.bytes().all(|b| b.is_ascii_alphabetic()) {
        return None;
    }
    let bytes = word.as_bytes();
    let mut last = sound_code(bytes[0]);
    let mut digits = String::with_capacity(bytes.len());
    digits.push(bytes[0] as char);
    for &b in &bytes[1..] {
        if b == b'h' || b == b'w' {
            continue;
        }
        let code = sound_code(b);
        if let Some(c) = code {
            if Some(c) != last {
                digits.push((b'0' + c) as char);
            }
        }
        last = code;
    }
    Some(digits)
}

pub fn soundex(word: &str) -> Option<String> {
    if word.len() < 4 {
        return None;
    }
    let key = sound_key(word)?;
    let mut out: String = key.chars().take(4).collect();
    while out.len() < 4 {
        out.push('0');
    }
    Some(out)
}

pub fn distance(left: &str, right: &str, limit: usize) -> usize {
    let lchars: Vec<char> = left.chars().collect();
    let rchars: Vec<char> = right.chars().collect();
    if lchars.len().abs_diff(rchars.len()) > limit {
        return limit + 1;
    }
    let mut previous: Vec<usize> = (0..=rchars.len()).collect();
    let mut before: Option<Vec<usize>> = None;
    for (i0, &a) in lchars.iter().enumerate() {
        let i = i0 + 1;
        let mut row = Vec::with_capacity(rchars.len() + 1);
        row.push(i);
        for (j0, &b) in rchars.iter().enumerate() {
            let j = j0 + 1;
            let cost = usize::from(a != b);
            let mut value = (row[j - 1] + 1)
                .min(previous[j] + 1)
                .min(previous[j - 1] + cost);
            if i > 1 && j > 1 && a == rchars[j - 2] && lchars[i - 2] == b {
                if let Some(ref bef) = before {
                    value = value.min(bef[j - 2] + 1);
                }
            }
            row.push(value);
        }
        before = Some(previous);
        previous = row;
    }
    *previous.last().unwrap_or(&0)
}

fn expansions(
    term: &str,
    frequency: &HashMap<String, usize>,
    total: usize,
    fuzzy: bool,
) -> HashMap<String, f64> {
    let mut result = HashMap::new();
    result.insert(term.to_string(), 1.0);
    if !fuzzy || term.chars().count() < 3 {
        return result;
    }
    let exact = *frequency.get(term).unwrap_or(&0);
    let common = (total as f64 * 0.005).max(5.0);
    let phonetic = soundex(term);
    let term_len = term.chars().count();
    for (word, &count) in frequency {
        if word == term {
            continue;
        }
        if word.starts_with(term) {
            result.insert(word.clone(), 0.5);
        } else if term_len >= 4 && exact < 3 {
            let limit = if term_len < 7 { 1 } else { 2 };
            let edits = distance(term, word, limit);
            if edits <= limit && ((count as f64) <= common || (exact == 0 && edits == 1)) {
                result.insert(word.clone(), 0.35);
            } else if (count as f64) <= common && phonetic.is_some() && phonetic == soundex(word) {
                result.insert(word.clone(), 0.2);
            }
        }
    }
    result
}

fn vocabulary_expansions(
    term: &str,
    vocabulary: &[(String, Vec<String>)],
    frequency: &HashMap<String, usize>,
    total: usize,
) -> HashMap<Vec<String>, f64> {
    let mut aliases: HashMap<Vec<String>, f64> = HashMap::new();
    for (name, spellings) in vocabulary {
        let preferred = words(name);
        if preferred.len() != 1 || stem(&preferred[0]) != term {
            continue;
        }
        for spelling in spellings {
            let phrase: Vec<String> = words(spelling).into_iter().map(|w| stem(&w)).collect();
            if !phrase.is_empty()
                && !(phrase.len() == 1 && phrase[0] == term)
                && phrase.iter().all(|w| frequency.contains_key(w))
            {
                aliases.insert(phrase, 0.5);
            }
        }
        let word = &preferred[0];
        let wchars: Vec<char> = word.chars().collect();
        if wchars.len() > 5 {
            for split in 3..(wchars.len() - 2) {
                let left: String = wchars[..split].iter().collect();
                let right: String = wchars[split..].iter().collect();
                let phrase = vec![stem(&left), stem(&right)];
                if phrase.iter().all(|part| frequency.contains_key(part)) {
                    aliases.insert(phrase, 0.5);
                }
            }
        }
        if let Some(key) = sound_key(term) {
            if key.len() >= 3 {
                let common = (total as f64 * 0.005).max(5.0);
                let term_len = term.chars().count();
                for (w, &count) in frequency {
                    let w_len = w.chars().count();
                    if w != term
                        && w_len >= 4
                        && (count as f64) <= common
                        && !STOP.contains(w.as_str())
                        && sound_key(w).as_deref() == Some(key.as_str())
                    {
                        let limit = term_len.max(w_len) / 2;
                        if distance(term, w, limit) <= limit {
                            aliases.entry(vec![w.clone()]).or_insert(0.3);
                        }
                    }
                }
            }
        }
    }
    aliases
}

type CandidateWeights = (HashMap<String, (f64, f64)>, HashMap<Vec<String>, (f64, f64)>);

fn candidate_weights(
    term: &str,
    frequency: &HashMap<String, usize>,
    total: usize,
    fuzzy: bool,
    vocabulary: &[(String, Vec<String>)],
) -> CandidateWeights {
    let mut singles = expansions(term, frequency, total, fuzzy);
    let mut phrases = HashMap::new();
    let weight = |quality: f64, count: usize| -> (f64, f64) {
        let rarity = (((total as f64) - (count as f64) + 0.5) / ((count as f64) + 0.5)).ln_1p();
        (quality, rarity)
    };
    if fuzzy {
        for (phrase, quality) in vocabulary_expansions(term, vocabulary, frequency, total) {
            if phrase.len() == 1 {
                let word = phrase.into_iter().next().unwrap();
                let entry = singles.entry(word).or_insert(0.0);
                if quality > *entry {
                    *entry = quality;
                }
            } else {
                let min_count = phrase
                    .iter()
                    .map(|w| *frequency.get(w).unwrap_or(&0))
                    .min()
                    .unwrap_or(0);
                phrases.insert(phrase, weight(quality, min_count));
            }
        }
    }
    let single_weights = singles
        .into_iter()
        .map(|(word, quality)| {
            let count = *frequency.get(&word).unwrap_or(&0);
            (word, weight(quality, count))
        })
        .collect();
    (single_weights, phrases)
}

#[derive(Clone, Copy)]
struct Span {
    start: usize,
    end: usize,
}

fn snippet(text: &str, tokens: &[Span], matched: &HashSet<usize>) -> String {
    let width = tokens.len().min(32);
    let first = *matched.iter().min().unwrap_or(&0);
    let target = first.saturating_sub(4) as isize;
    let mut best_begin = 0usize;
    let mut best_key: Option<(usize, isize)> = None;
    for i in 0..=(tokens.len() - width) {
        let count = (i..i + width).filter(|j| matched.contains(j)).count();
        let dist = -(target - i as isize).abs();
        let key = (count, dist);
        if best_key.map_or(true, |b| key > b) {
            best_key = Some(key);
            best_begin = i;
        }
    }
    let end = best_begin + width;
    let mut position = if best_begin > 0 { tokens[best_begin].start } else { 0 };
    let mut output = String::new();
    if best_begin > 0 {
        output.push('…');
    }
    for (i, token) in tokens.iter().enumerate().take(end).skip(best_begin) {
        if matched.contains(&i) {
            output.push_str(&text[position..token.start]);
            output.push_str("**");
            output.push_str(&text[token.start..token.end]);
            output.push_str("**");
            position = token.end;
        }
    }
    if end < tokens.len() {
        output.push_str(&text[position..tokens[end - 1].end]);
        output.push('…');
    } else {
        output.push_str(&text[position..]);
    }
    output.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[derive(Debug, Clone)]
pub struct SearchHit {
    pub index: usize,
    pub snippet: String,
    pub score: f64,
}

/// Rank passages (`(doc_id, text)`), returning `(top hits, per-doc-id match counts in rank order)`.
pub fn rank_passages(
    passages: &[(String, String)],
    query: &str,
    limit: usize,
    fuzzy: bool,
    stem_phrases: bool,
    vocabulary: &[(String, Vec<String>)],
) -> Result<(Vec<SearchHit>, Vec<(String, usize)>), CliError> {
    if query.chars().count() > 500 || query.matches('"').count() % 2 != 0 {
        return Err(CliError::value(
            "Use up to 500 characters and close any quoted phrases.",
        ));
    }
    let parts: Vec<&str> = query.split('"').collect();
    let mut phrases: Vec<Vec<String>> = parts
        .iter()
        .skip(1)
        .step_by(2)
        .map(|part| words(part))
        .collect();
    if stem_phrases {
        phrases = phrases
            .into_iter()
            .map(|p| p.into_iter().map(|w| stem(&w)).collect())
            .collect();
    }
    let unquoted: Vec<String> = parts
        .iter()
        .step_by(2)
        .flat_map(|part| words(part))
        .collect();
    let significant: Vec<String> = unquoted
        .iter()
        .filter(|w| !STOP.contains(w.as_str()))
        .cloned()
        .collect();
    let source_terms: Vec<String> = if !significant.is_empty() {
        significant
    } else if phrases.is_empty() {
        unquoted
    } else {
        Vec::new()
    };
    let mut terms = Vec::new();
    let mut seen_terms = HashSet::new();
    for w in source_terms {
        let s = stem(&w);
        if seen_terms.insert(s.clone()) {
            terms.push(s);
        }
    }
    if (terms.is_empty() && phrases.is_empty()) || phrases.iter().any(|p| p.is_empty()) {
        return Err(CliError::value("Enter words or a quoted phrase to search for."));
    }

    struct Entry {
        orig_index: usize,
        doc_id: String,
        text: String,
        tokens: Vec<Span>,
        folded: Vec<String>,
        stems: Vec<String>,
        positions: HashMap<String, Vec<usize>>,
    }

    let mut corpus: Vec<Entry> = Vec::new();
    let mut total_length = 0usize;
    let mut documents: HashMap<String, HashSet<String>> = HashMap::new();
    for (orig_index, (doc_id, raw_text)) in passages.iter().enumerate() {
        let text: String = raw_text.nfc().collect();
        let tokens: Vec<Span> = TOKEN_RE
            .find_iter(&text)
            .map(|m| Span { start: m.start(), end: m.end() })
            .collect();
        if tokens.is_empty() {
            continue;
        }
        let folded: Vec<String> = tokens.iter().map(|s| fold(&text[s.start..s.end])).collect();
        let stems: Vec<String> = folded.iter().map(|w| stem(w)).collect();
        let mut positions: HashMap<String, Vec<usize>> = HashMap::new();
        for (idx, w) in stems.iter().enumerate() {
            positions.entry(w.clone()).or_default().push(idx);
        }
        let doc_set = documents.entry(doc_id.clone()).or_default();
        for w in positions.keys() {
            doc_set.insert(w.clone());
        }
        total_length += tokens.len();
        corpus.push(Entry {
            orig_index,
            doc_id: doc_id.clone(),
            text,
            tokens,
            folded,
            stems,
            positions,
        });
    }
    if corpus.is_empty() {
        return Ok((Vec::new(), Vec::new()));
    }

    let mut frequency: HashMap<String, usize> = HashMap::new();
    for doc_words in documents.values() {
        for w in doc_words {
            *frequency.entry(w.clone()).or_insert(0) += 1;
        }
    }
    let total = documents.len();
    let average = total_length as f64 / corpus.len() as f64;
    let weights: Vec<CandidateWeights> = terms
        .iter()
        .map(|t| candidate_weights(t, &frequency, total, fuzzy, vocabulary))
        .collect();

    struct Ranked {
        all_exact: bool,
        coverage: f64,
        score: f64,
        neg_order: isize,
        matches: HashSet<usize>,
    }

    let mut ranked: Vec<Ranked> = Vec::new();
    for (order, entry) in corpus.iter().enumerate() {
        let mut matches: HashSet<usize> = HashSet::new();
        let phrase_tokens = if stem_phrases { &entry.stems } else { &entry.folded };
        let mut phrase_ok = true;
        for phrase in &phrases {
            let mut found = Vec::new();
            if phrase_tokens.len() >= phrase.len() {
                for i in 0..=(phrase_tokens.len() - phrase.len()) {
                    if phrase_tokens[i..i + phrase.len()] == phrase[..] {
                        for offset in 0..phrase.len() {
                            found.push(i + offset);
                        }
                    }
                }
            }
            if found.is_empty() {
                phrase_ok = false;
                break;
            }
            matches.extend(found);
        }
        if !phrase_ok {
            continue;
        }
        let mut exact = 0usize;
        let mut coverage = 0.0f64;
        let mut score = 0.0f64;
        let normalization = 1.2 * (0.25 + 0.75 * (entry.tokens.len() as f64) / average);
        for (candidates, aliases) in &weights {
            // (is_exact, weight, quality, key_phrase, matched_positions)
            let mut best: Option<(bool, f64, f64, Vec<String>, Vec<usize>)> = None;
            for (word, positions) in &entry.positions {
                if let Some(&(quality, rarity)) = candidates.get(word) {
                    let count = positions.len() as f64;
                    let w = quality * rarity * (count * 2.2) / (count + normalization);
                    let cand = (quality == 1.0, w, quality, vec![word.clone()], positions.clone());
                    if best.as_ref().map_or(true, |b| cmp_choice(&cand, b) == Ordering::Greater) {
                        best = Some(cand);
                    }
                }
            }
            for (phrase, &(quality, rarity)) in aliases {
                let size = phrase.len();
                if let Some(starts) = entry.positions.get(&phrase[0]) {
                    let mut hits = Vec::new();
                    for &idx in starts {
                        if idx + size <= entry.stems.len() && entry.stems[idx..idx + size] == phrase[..] {
                            hits.push(idx);
                        }
                    }
                    if !hits.is_empty() {
                        let count = hits.len() as f64;
                        let w = quality * rarity * (count * 2.2) / (count + normalization);
                        let matched: Vec<usize> = hits
                            .into_iter()
                            .flat_map(|idx| (0..size).map(move |off| idx + off))
                            .collect();
                        let cand = (false, w, quality, phrase.clone(), matched);
                        if best.as_ref().map_or(true, |b| cmp_choice(&cand, b) == Ordering::Greater) {
                            best = Some(cand);
                        }
                    }
                }
            }
            if let Some((is_exact, w, quality, _, matched)) = best {
                if is_exact {
                    exact += 1;
                }
                coverage += quality;
                score += w;
                matches.extend(matched);
            }
        }
        if !matches.is_empty() && (coverage > 0.0 || terms.is_empty()) {
            ranked.push(Ranked {
                all_exact: exact == terms.len(),
                coverage,
                score,
                neg_order: -(order as isize),
                matches,
            });
        }
    }

    ranked.sort_by(|a, b| {
        b.all_exact
            .cmp(&a.all_exact)
            .then_with(|| b.coverage.total_cmp(&a.coverage))
            .then_with(|| b.score.total_cmp(&a.score))
            .then_with(|| b.neg_order.cmp(&a.neg_order))
    });

    // Preserve first-seen insertion order like Python Counter on ranked
    let mut counts: Vec<(String, usize)> = Vec::new();
    let mut count_idx: HashMap<String, usize> = HashMap::new();
    for item in &ranked {
        let doc_id = &corpus[(-item.neg_order) as usize].doc_id;
        if let Some(&idx) = count_idx.get(doc_id) {
            counts[idx].1 += 1;
        } else {
            count_idx.insert(doc_id.clone(), counts.len());
            counts.push((doc_id.clone(), 1));
        }
    }

    let mut hits = Vec::new();
    for item in ranked.into_iter().take(limit) {
        let entry = &corpus[(-item.neg_order) as usize];
        let rounded = (item.score * 1_000_000.0).round_ties_even() / 1_000_000.0;
        hits.push(SearchHit {
            index: entry.orig_index,
            snippet: snippet(&entry.text, &entry.tokens, &item.matches),
            score: rounded,
        });
    }
    Ok((hits, counts))
}

fn cmp_choice(
    a: &(bool, f64, f64, Vec<String>, Vec<usize>),
    b: &(bool, f64, f64, Vec<String>, Vec<usize>),
) -> Ordering {
    a.0.cmp(&b.0)
        .then_with(|| a.1.total_cmp(&b.1))
        .then_with(|| a.2.total_cmp(&b.2))
        .then_with(|| a.3.cmp(&b.3))
        .then_with(|| a.4.cmp(&b.4))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fold_and_stem() {
        assert_eq!(fold("Café"), "cafe");
        assert_eq!(fold("don't"), "dont");
        assert_eq!(stem("running"), "run");
        assert_eq!(stem("stories"), "story");
        assert_eq!(stem("class"), "class");
        assert_eq!(stem("focus"), "focus");
        assert_eq!(stem("making"), "mak");
    }

    #[test]
    fn test_soundex_and_distance() {
        assert_eq!(soundex("robert").as_deref(), Some("r163"));
        assert_eq!(soundex("rupert").as_deref(), Some("r163"));
        assert_eq!(distance("kitten", "sitting", 3), 3);
        assert_eq!(distance("ca", "ac", 1), 1); // transposition
    }

    #[test]
    fn test_rank_passages_fuzzy_and_phrases() {
        let passages = vec![
            ("m1".into(), "We finalized the Kubernetes rollout yesterday.".into()),
            ("m2".into(), "The budget review starts at noon.".into()),
        ];
        let (hits, counts) = rank_passages(&passages, "kubernetis", 10, true, false, &[]).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].index, 0);
        assert!(hits[0].snippet.contains("**Kubernetes**"));
        assert_eq!(counts, vec![("m1".into(), 1)]);

        let (exact_only, _) = rank_passages(&passages, "kubernetis", 10, false, false, &[]).unwrap();
        assert!(exact_only.is_empty());

        let (phrase_hits, _) =
            rank_passages(&passages, "\"budget review\"", 10, true, false, &[]).unwrap();
        assert_eq!(phrase_hits.len(), 1);
        assert_eq!(phrase_hits[0].index, 1);
    }

    #[test]
    fn test_vocabulary_expansion() {
        let passages = vec![
            ("m1".into(), "Let's check sound folio local later.".into()),
        ];
        let vocab = vec![("Soundfolio".into(), vec!["sound folio".into()])];
        let (hits, _) = rank_passages(&passages, "soundfolio", 10, true, false, &vocab).unwrap();
        assert_eq!(hits.len(), 1);
        assert!(hits[0].snippet.contains("**sound** **folio**"));
    }
}
