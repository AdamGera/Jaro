"""Rank transcript and note passages and keep their original text for context."""
import math
import re
import unicodedata
from collections import Counter, defaultdict

TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
STOP = frozenset("a about an and are as at be but by did do for from had has have he i in is it its of on or our she so that the their them there they this to was we were what when where which who with you".split())
SUFFIXES = (("ingly", 3, ""), ("edly", 3, ""), ("ings", 3, ""), ("ing", 3, ""), ("ied", 2, "y"),
            ("ies", 2, "y"), ("ed", 3, ""), ("es", 3, ""), ("ly", 3, ""), ("s", 3, ""))
SOUNDS = {char: code for code, group in enumerate(("bfpv", "cgjkqsxz", "dt", "l", "mn", "r"), 1) for char in group}


def fold(word):
    decomposed = unicodedata.normalize("NFKD", word.casefold())
    return "".join(c for c in decomposed if not unicodedata.category(c).startswith("M") and c not in "'’")


def words(text):
    return [fold(match.group()) for match in TOKEN.finditer(unicodedata.normalize("NFC", text))]


def stem(word):
    if len(word) <= 3 or not word.isascii() or not word.isalpha():
        return word
    for suffix, minimum, replacement in SUFFIXES:
        if len(word) - len(suffix) >= minimum and word.endswith(suffix):
            if suffix != "s" or not word.endswith(("ss", "us")):
                word = word[:-len(suffix)] + replacement
                break
    if len(word) >= 4 and word[-1] == word[-2] and word[-1] not in "aeioulsz":
        word = word[:-1]
    return word[:-1] if len(word) >= 4 and word.endswith("e") else word


def sound_key(word):
    if not word.isascii() or not word.isalpha():
        return None
    last = SOUNDS.get(word[0])
    digits = [word[0]]
    for char in word[1:]:
        if char in "hw":
            continue
        code = SOUNDS.get(char)
        if code is not None and code != last:
            digits.append(str(code))
        last = code
    return "".join(digits)


def soundex(word):
    key = sound_key(word) if len(word) >= 4 else None
    return key[:4].ljust(4, "0") if key else None


def distance(left, right, limit):
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous, before = list(range(len(right) + 1)), None
    for i, a in enumerate(left, 1):
        row = [i]
        for j, b in enumerate(right, 1):
            value = min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b))
            if i > 1 and j > 1 and a == right[j - 2] and left[i - 2] == b:
                value = min(value, before[j - 2] + 1)
            row.append(value)
        before, previous = previous, row
    return previous[-1]


def expansions(term, frequency, total, fuzzy):
    result = {term: 1.0}
    if not fuzzy or len(term) < 3:
        return result
    exact = frequency[term]
    common = max(5, total * 0.005)
    phonetic = soundex(term)
    for word, count in frequency.items():
        if word == term:
            continue
        if word.startswith(term):
            result[word] = 0.5
        elif len(term) >= 4 and exact < 3:
            limit = 1 if len(term) < 7 else 2
            edits = distance(term, word, limit)
            if edits <= limit and (count <= common or (not exact and edits == 1)):
                result[word] = 0.35
            elif count <= common and phonetic and phonetic == soundex(word):
                result[word] = 0.2
    return result


def vocabulary_expansions(term, vocabulary, frequency, total):
    aliases = {}
    for name, spellings in vocabulary:
        preferred = words(name)
        if len(preferred) != 1 or stem(preferred[0]) != term:
            continue
        for spelling in spellings:
            phrase = tuple(map(stem, words(spelling)))
            if phrase and phrase != (term,) and all(word in frequency for word in phrase):
                aliases[phrase] = 0.5
        word = preferred[0]
        for split in range(3, len(word) - 2):
            phrase = (stem(word[:split]), stem(word[split:]))
            if all(part in frequency for part in phrase):
                aliases[phrase] = 0.5
        key = sound_key(term)
        if key and len(key) >= 3:
            common = max(5, total * 0.005)
            for word, count in frequency.items():
                if (word != term and len(word) >= 4 and count <= common and word not in STOP
                        and sound_key(word) == key):
                    limit = max(len(term), len(word)) // 2
                    if distance(term, word, limit) <= limit:
                        aliases.setdefault((word,), 0.3)
    return aliases


def candidate_weights(term, frequency, total, fuzzy, vocabulary):
    singles = expansions(term, frequency, total, fuzzy)
    phrases = {}

    def weight(quality, count):
        return quality, math.log1p((total - count + 0.5) / (count + 0.5))

    if fuzzy:
        for phrase, quality in vocabulary_expansions(term, vocabulary, frequency, total).items():
            if len(phrase) == 1:
                word = phrase[0]
                singles[word] = max(quality, singles.get(word, 0))
            else:
                phrases[phrase] = weight(quality, min(frequency[word] for word in phrase))
    return {word: weight(quality, frequency[word]) for word, quality in singles.items()}, phrases


def snippet(text, tokens, matched):
    width = min(len(tokens), 32)
    first = min(matched)
    beginning = max(range(len(tokens) - width + 1), key=lambda i: (
        sum(j in matched for j in range(i, i + width)), -abs(max(0, first - 4) - i)))
    end = beginning + width
    position = tokens[beginning].start() if beginning else 0
    output = ["…"] if beginning else []
    for i in range(beginning, end):
        if i in matched:
            token = tokens[i]
            output += [text[position:token.start()], "**", token.group(), "**"]
            position = token.end()
    output.append(text[position:tokens[end - 1].end()] if end < len(tokens) else text[position:])
    if end < len(tokens):
        output.append("…")
    return " ".join("".join(output).split())


def rank_passages(passages, query, limit, fuzzy, *, stem_phrases=False, vocabulary=()):
    if len(query) > 500 or query.count('"') % 2:
        raise ValueError("Use up to 500 characters and close any quoted phrases.")
    parts = query.split('"')
    phrases = [words(part) for part in parts[1::2]]
    if stem_phrases:
        phrases = [list(map(stem, phrase)) for phrase in phrases]
    unquoted = [word for part in parts[::2] for word in words(part)]
    significant = [word for word in unquoted if word not in STOP]
    terms = list(dict.fromkeys(map(stem, significant or ([] if phrases else unquoted))))
    if not (terms or phrases) or any(not phrase for phrase in phrases):
        raise ValueError("Enter words or a quoted phrase to search for.")

    corpus, length = [], 0
    documents = defaultdict(set)
    for passage in passages:
        text = unicodedata.normalize("NFC", passage["text"])
        tokens = list(TOKEN.finditer(text))
        folded = [fold(token.group()) for token in tokens]
        stems = list(map(stem, folded))
        positions = defaultdict(list)
        for index, word in enumerate(stems):
            positions[word].append(index)
        for word in positions:
            documents[passage["id"]].add(word)
        if tokens:
            length += len(tokens)
            corpus.append((passage, text, tokens, folded, stems, positions))
    if not corpus:
        return [], Counter()
    frequency = Counter(word for document in documents.values() for word in document)
    total, average = len(documents), length / len(corpus)
    weights = [candidate_weights(term, frequency, total, fuzzy, vocabulary) for term in terms]

    ranked = []
    for order, (passage, text, tokens, folded, stems, positions) in enumerate(corpus):
        matches = set()
        phrase_tokens = stems if stem_phrases else folded
        for phrase in phrases:
            found = {i + offset for i in range(len(phrase_tokens) - len(phrase) + 1)
                     if phrase_tokens[i:i + len(phrase)] == phrase for offset in range(len(phrase))}
            if not found:
                break
            matches.update(found)
        else:
            exact, coverage, score = 0, 0.0, 0.0
            normalization = 1.2 * (0.25 + 0.75 * len(tokens) / average)
            for candidates, aliases in weights:
                choices = []
                for word in positions.keys() & candidates.keys():
                    quality, rarity = candidates[word]
                    count = len(positions[word])
                    choices.append((quality == 1, quality * rarity * (count * 2.2) / (count + normalization),
                                    quality, (word,), positions[word]))
                for phrase, (quality, rarity) in aliases.items():
                    size = len(phrase)
                    hits = [index for index in positions.get(phrase[0], ()) if tuple(stems[index:index + size]) == phrase]
                    if hits:
                        count = len(hits)
                        choices.append((False, quality * rarity * (count * 2.2) / (count + normalization),
                                        quality, phrase, [index + offset for index in hits for offset in range(size)]))
                if choices:
                    exact_match, weight, quality, _, matched = max(choices)
                    exact += exact_match
                    coverage += quality
                    score += weight
                    matches.update(matched)
            if matches and (coverage > 0 or not terms):
                ranked.append((exact == len(terms), coverage, score, -order, matches))
    ranked.sort(key=lambda hit: hit[:4], reverse=True)
    counts = Counter(corpus[-order][0]["id"] for _, _, _, order, _ in ranked)
    hits = []
    for _, _, score, order, matches in ranked[:limit]:
        passage, text, tokens, *_ = corpus[-order]
        hits.append({**passage, "snippet": snippet(text, tokens, matches), "score": round(score, 6)})
    return hits, counts


def meeting_passages(passages, query, limit, fuzzy, *, stem_phrases=False, vocabulary=()):
    hits, _ = rank_passages(passages, query, limit, fuzzy, stem_phrases=stem_phrases, vocabulary=vocabulary)
    return hits
