from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
BRACKET_ID_PATTERN = re.compile(r"\[([A-Za-z0-9_-]{6,})\]")
TITLE_HINT_PATTERN = re.compile(
    r"(?:episodes|episode|ova|movie|finale)\s*[\d.,~ ]*(?:-|:)?\s*(?P<title>.+)",
    re.IGNORECASE,
)
QUOTED_TITLE_PATTERN = re.compile(r'"(?P<title>[^"]+)"')
TRAILING_BRACKET_BLOCKS_PATTERN = re.compile(r"(?:\s*\[[^\]]+\]\s*)+$")
TRAILING_CONTENT_ID_PATTERN = re.compile(
    r"(?:\s*[-_]+\s*)(?P<id>(?=[A-Za-z0-9_-]{8,}$)(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_-]+)$"
)
RANGE_PATTERN = re.compile(r"(?P<start>\d+(?:\.\d+)?)\s*(?:-|~|to|through)\s*(?P<end>\d+(?:\.\d+)?)", re.IGNORECASE)
NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
EPISODE_HINT_PATTERN = re.compile(r"\b(?:episode|episodes|ep|eps)\b", re.IGNORECASE)
COLLECTION_HINT_PATTERN = re.compile(r"\b(?:collection|compilation|episodes?|ep|eps)\b", re.IGNORECASE)
NUMERIC_TITLE_PATTERN = re.compile(r"(?:and\s+)?\d+(?:\.\d+)?(?:\s+(?:and\s+)?\d+(?:\.\d+)?)*")
SERIES_PATTERN = re.compile(
    r"^(?P<kind>Episode|OVA|Movie)\s+"
    r"(?P<number>\d+(?:\.\d+)?(?:~\d+(?:\.\d+)?)?)"
    r"(?:\s*-\s*(?P<title>.+))?$",
    re.IGNORECASE,
)
MINIMUM_HYBRID_COVER_SCORE = 0.6
AMBIGUITY_EPSILON = 1e-9


@dataclass(frozen=True)
class CoverCandidateMatch:
    path: Path
    full_norm: str
    title_norm: str
    shared_ids: tuple[str, ...]
    shared_episode_refs: tuple[str, ...]
    exact_full_match: bool
    exact_title_match: bool
    exact_episode_match: bool
    title_similarity: float
    full_similarity: float
    reference_overlap: float
    repo_score_norm: float
    hybrid_score: float
    strategy: str
    metrics: dict[str, float]


def tokenize(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def parse_series_name(stem: str) -> tuple[str | None, str | None, str | None]:
    match = SERIES_PATTERN.match(stem)
    if not match:
        return None, None, None
    return (
        match.group("kind").lower(),
        match.group("number"),
        (match.group("title") or "").strip() or None,
    )


def legacy_series_cover_score(candidate: Path, source_video: Path, metadata_title: str | None) -> int:
    candidate_stem = candidate.stem
    source_stem = source_video.stem
    kind, number, title = parse_series_name(source_stem)
    candidate_tokens = set(tokenize(candidate_stem))
    score = 0

    if number:
        first_number = number.split("~", 1)[0]
        if candidate_stem == first_number:
            score += 80
        elif first_number in candidate_tokens:
            score += 70
        elif number in candidate_stem:
            score += 60

    if kind and kind in candidate_tokens:
        score += 15

    comparison_strings = [source_stem]
    if title:
        comparison_strings.append(title)
    if metadata_title:
        comparison_strings.append(metadata_title)

    normalized_candidate = " ".join(tokenize(candidate_stem))
    best_overlap_score = 0
    for value in comparison_strings:
        normalized_value = " ".join(tokenize(value))
        if normalized_candidate and normalized_candidate == normalized_value:
            score += 50
            continue

        source_tokens = set(tokenize(value))
        if not source_tokens:
            continue
        overlap = candidate_tokens & source_tokens
        if overlap:
            best_overlap_score = max(best_overlap_score, int((len(overlap) / len(source_tokens)) * 35))

    score += best_overlap_score
    return score


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.replace("⧸", "/")
    normalized = re.sub(r"[“”＂]", '"', normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def extract_content_ids(value: str) -> set[str]:
    ids = {match.group(1) for match in BRACKET_ID_PATTERN.finditer(unicodedata.normalize("NFKC", value))}
    stripped = TRAILING_BRACKET_BLOCKS_PATTERN.sub("", unicodedata.normalize("NFKC", value)).strip()
    trailing_match = TRAILING_CONTENT_ID_PATTERN.search(stripped)
    if trailing_match:
        ids.add(trailing_match.group("id"))
    return ids


def strip_content_suffixes(value: str) -> str:
    stripped = unicodedata.normalize("NFKC", value)
    stripped = TRAILING_BRACKET_BLOCKS_PATTERN.sub("", stripped).strip()
    stripped = TRAILING_CONTENT_ID_PATTERN.sub("", stripped).strip(" -_")
    return stripped


def extract_title(stem: str) -> str:
    cleaned = strip_content_suffixes(stem).replace("⧸", "/")

    quoted = QUOTED_TITLE_PATTERN.search(cleaned)
    if quoted:
        return normalize_text(quoted.group("title"))

    match = re.search(r"\bEpisode\s+\d+(?:\.\d+)?\s*-\s*(?P<title>.+)$", cleaned, re.IGNORECASE)
    if match:
        title = normalize_text(match.group("title"))
        return "" if NUMERIC_TITLE_PATTERN.fullmatch(title) else title

    hinted = TITLE_HINT_PATTERN.search(cleaned)
    if hinted:
        title = normalize_text(hinted.group("title"))
        title = re.sub(r"\bfate apocrabridged\b", " ", title)
        title = re.sub(r"\bfate apocrypha abridged\b", " ", title)
        title = re.sub(r"\s+", " ", title).strip()
        return "" if NUMERIC_TITLE_PATTERN.fullmatch(title) else title

    title = normalize_text(cleaned)
    return "" if NUMERIC_TITLE_PATTERN.fullmatch(title) else title


def extract_primary_episode_number(stem: str) -> str | None:
    _, number, _ = parse_series_name(stem)
    return number


def _expand_range(start: str, end: str) -> set[str]:
    if "." in start or "." in end:
        return {start, end}

    start_int = int(start)
    end_int = int(end)
    if end_int < start_int or (end_int - start_int) > 20:
        return {start, end}
    return {str(value) for value in range(start_int, end_int + 1)}


def extract_episode_references(stem: str) -> set[str]:
    normalized = strip_content_suffixes(stem).replace("⧸", "/")
    lowered = normalized.casefold()
    references: set[str] = set()

    primary_episode = extract_primary_episode_number(stem)
    if primary_episode:
        references.add(primary_episode)

    for match in RANGE_PATTERN.finditer(lowered):
        references.update(_expand_range(match.group("start"), match.group("end")))

    if EPISODE_HINT_PATTERN.search(lowered) or COLLECTION_HINT_PATTERN.search(lowered):
        references.update(NUMBER_PATTERN.findall(lowered))
    elif re.fullmatch(r"\d+(?:\.\d+)?", lowered.strip()):
        references.add(lowered.strip())

    return references


def is_collection_like(stem: str) -> bool:
    lowered = strip_content_suffixes(stem).casefold()
    refs = extract_episode_references(stem)
    return bool(COLLECTION_HINT_PATTERN.search(lowered) and len(refs) >= 2)


def levenshtein_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    if len(left) < len(right):
        left, right = right, left

    previous_row = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current_row = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            insertions = current_row[right_index - 1] + 1
            deletions = previous_row[right_index] + 1
            substitutions = previous_row[right_index - 1] + (left_char != right_char)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def normalized_levenshtein(left: str, right: str) -> float:
    if not left and not right:
        return 1.0
    return 1.0 - (levenshtein_distance(left, right) / max(len(left), len(right), 1))


def bigrams(value: str) -> list[str]:
    padded = f" {value} "
    return [padded[index : index + 2] for index in range(len(padded) - 1)]


def sorensen_dice(left: str, right: str) -> float:
    left_bigrams = bigrams(left)
    right_bigrams = bigrams(right)
    if not left_bigrams and not right_bigrams:
        return 1.0

    left_counts: dict[str, int] = {}
    right_counts: dict[str, int] = {}
    for token in left_bigrams:
        left_counts[token] = left_counts.get(token, 0) + 1
    for token in right_bigrams:
        right_counts[token] = right_counts.get(token, 0) + 1

    overlap = 0
    for token, count in left_counts.items():
        overlap += min(count, right_counts.get(token, 0))

    return (2.0 * overlap) / (len(left_bigrams) + len(right_bigrams))


def sequence_ratio(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def jaro_winkler(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0

    match_distance = max(0, max(len(left), len(right)) // 2 - 1)
    left_matches = [False] * len(left)
    right_matches = [False] * len(right)

    matches = 0
    for left_index, left_char in enumerate(left):
        start = max(0, left_index - match_distance)
        end = min(left_index + match_distance + 1, len(right))
        for right_index in range(start, end):
            if right_matches[right_index] or left_char != right[right_index]:
                continue
            left_matches[left_index] = True
            right_matches[right_index] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    transpositions = 0
    right_index = 0
    for left_index, matched in enumerate(left_matches):
        if not matched:
            continue
        while not right_matches[right_index]:
            right_index += 1
        if left[left_index] != right[right_index]:
            transpositions += 1
        right_index += 1

    transpositions /= 2
    matches_float = float(matches)
    jaro = (
        (matches_float / len(left))
        + (matches_float / len(right))
        + ((matches_float - transpositions) / matches_float)
    ) / 3.0

    prefix = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char or prefix == 4:
            break
        prefix += 1

    return jaro + (prefix * 0.1 * (1.0 - jaro))


def token_jaccard(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def token_overlap(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


def smart_title_score(source_stem: str, candidate_stem: str) -> float:
    source_title = extract_title(source_stem)
    candidate_title = extract_title(candidate_stem)
    if not source_title or not candidate_title:
        return 0.0

    if source_title == candidate_title:
        return 1.0

    lev_dice = (normalized_levenshtein(source_title, candidate_title) + sorensen_dice(source_title, candidate_title)) / 2.0
    jaro = jaro_winkler(source_title, candidate_title)
    overlap = token_overlap(source_title, candidate_title)
    jaccard = token_jaccard(source_title, candidate_title)
    return min(1.0, (0.45 * lev_dice) + (0.25 * jaro) + (0.20 * overlap) + (0.10 * jaccard))


def _sortable_reference(value: str) -> tuple[int, float]:
    if "." in value:
        return (1, float(value))
    return (0, float(value))


def _strategy_for_scores(
    *,
    id_score: float,
    title_path: float,
    numeric_path: float,
    reference_path: float,
    exact_title_match: bool,
    exact_episode_match: bool,
    shared_ids: tuple[str, ...],
    shared_episode_refs: tuple[str, ...],
) -> str:
    if shared_ids and id_score > 0.0:
        strategy_scores = {"shared_id": id_score}
    else:
        strategy_scores = {}
    if exact_episode_match and numeric_path > 0.0:
        strategy_scores["episode_number"] = numeric_path
    if len(shared_episode_refs) >= 2 and reference_path > 0.0:
        strategy_scores["episode_reference_overlap"] = reference_path
    if exact_title_match and title_path > 0.0:
        strategy_scores["exact_title"] = title_path
    if (not exact_title_match and title_path > 0.0) or not strategy_scores:
        strategy_scores["title_similarity"] = title_path
    return max(strategy_scores, key=strategy_scores.get)


def _candidate_rank(match: CoverCandidateMatch) -> tuple[float, ...]:
    return (
        match.hybrid_score,
        1.0 if match.shared_ids else 0.0,
        1.0 if match.exact_title_match else 0.0,
        match.title_similarity,
        1.0 if match.exact_episode_match else 0.0,
        match.reference_overlap,
        match.repo_score_norm,
        1.0 if match.exact_full_match else 0.0,
    )


def evaluate_candidates(
    source_video: Path,
    covers_dir: Path,
    metadata_title: str | None = None,
) -> list[CoverCandidateMatch]:
    if not covers_dir.is_dir():
        return []

    source_full = normalize_text(strip_content_suffixes(source_video.stem))
    source_title = extract_title(source_video.stem)
    source_ids = extract_content_ids(source_video.stem)
    source_episode = extract_primary_episode_number(source_video.stem)
    source_refs = extract_episode_references(source_video.stem)
    source_is_collection = is_collection_like(source_video.stem)

    results: list[CoverCandidateMatch] = []
    for candidate in sorted(covers_dir.iterdir()):
        if not candidate.is_file() or candidate.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        candidate_full = normalize_text(strip_content_suffixes(candidate.stem))
        candidate_title = extract_title(candidate.stem)
        candidate_ids = tuple(sorted(source_ids & extract_content_ids(candidate.stem)))
        candidate_refs = extract_episode_references(candidate.stem)
        shared_refs = tuple(sorted(source_refs & candidate_refs, key=_sortable_reference))
        repo_score_norm = min(legacy_series_cover_score(candidate, source_video, metadata_title) / 100.0, 1.0)

        exact_full_match = bool(source_full and source_full == candidate_full)
        exact_title_match = bool(source_title and source_title == candidate_title)
        exact_episode_match = bool(source_episode and source_episode in candidate_refs)
        reference_overlap = (len(shared_refs) / len(source_refs)) if source_refs else 0.0

        title_similarity = smart_title_score(source_video.stem, candidate.stem)
        full_similarity = (
            (normalized_levenshtein(source_full, candidate_full) + sorensen_dice(source_full, candidate_full)) / 2.0
            if source_full and candidate_full
            else 0.0
        )

        id_score = 1.25 if candidate_ids else 0.0
        title_path = title_similarity + (0.05 * full_similarity) + (0.05 * repo_score_norm)
        if exact_full_match:
            title_path = max(title_path, 0.95)
        if exact_title_match:
            title_path = max(title_path, 1.05)

        numeric_path = 0.0
        if exact_episode_match:
            numeric_path += 0.55
        numeric_path += 0.20 * reference_overlap
        numeric_path += 0.10 * repo_score_norm

        reference_path = 0.0
        if shared_refs:
            reference_path += 0.25 * reference_overlap
            if len(shared_refs) >= 2:
                reference_path += 0.35
            if len(shared_refs) >= 2 and (source_is_collection or is_collection_like(candidate.stem)):
                reference_path += 0.20
            reference_path += 0.10 * title_similarity
            reference_path += 0.05 * repo_score_norm

        hybrid_score = max(id_score, title_path, numeric_path, reference_path)
        strategy = _strategy_for_scores(
            id_score=id_score,
            title_path=title_path,
            numeric_path=numeric_path,
            reference_path=reference_path,
            exact_title_match=exact_title_match,
            exact_episode_match=exact_episode_match,
            shared_ids=candidate_ids,
            shared_episode_refs=shared_refs,
        )

        metrics = {
            "levenshtein_full": normalized_levenshtein(source_full, candidate_full) if source_full or candidate_full else 0.0,
            "dice_full": sorensen_dice(source_full, candidate_full) if source_full or candidate_full else 0.0,
            "lev_dice_full_avg": full_similarity,
            "lev_dice_title_avg": (
                (normalized_levenshtein(source_title, candidate_title) + sorensen_dice(source_title, candidate_title)) / 2.0
                if source_title or candidate_title
                else 0.0
            ),
            "sequence_ratio_full": sequence_ratio(source_full, candidate_full) if source_full or candidate_full else 0.0,
            "jaro_winkler_title": jaro_winkler(source_title, candidate_title) if source_title or candidate_title else 0.0,
            "token_jaccard_title": token_jaccard(source_title, candidate_title) if source_title or candidate_title else 0.0,
            "token_overlap_title": token_overlap(source_title, candidate_title) if source_title or candidate_title else 0.0,
            "smart_title_score": title_similarity,
            "repo_score_norm": repo_score_norm,
            "hybrid_score": hybrid_score,
            "reference_overlap": reference_overlap,
            "id_match": 1.0 if candidate_ids else 0.0,
        }

        results.append(
            CoverCandidateMatch(
                path=candidate,
                full_norm=candidate_full,
                title_norm=candidate_title,
                shared_ids=candidate_ids,
                shared_episode_refs=shared_refs,
                exact_full_match=exact_full_match,
                exact_title_match=exact_title_match,
                exact_episode_match=exact_episode_match,
                title_similarity=title_similarity,
                full_similarity=full_similarity,
                reference_overlap=reference_overlap,
                repo_score_norm=repo_score_norm,
                hybrid_score=hybrid_score,
                strategy=strategy,
                metrics=metrics,
            )
        )

    return sorted(results, key=_candidate_rank, reverse=True)


def find_best_cover(
    source_video: Path,
    covers_dir: Path,
    *,
    metadata_title: str | None = None,
    minimum_score: float = MINIMUM_HYBRID_COVER_SCORE,
) -> CoverCandidateMatch | None:
    results = evaluate_candidates(source_video, covers_dir, metadata_title)
    if not results:
        return None
    best = results[0]
    if best.hybrid_score < minimum_score:
        return None
    if len(results) > 1:
        runner_up = results[1]
        if abs(runner_up.hybrid_score - best.hybrid_score) <= AMBIGUITY_EPSILON:
            return None
    return best


__all__ = [
    "CoverCandidateMatch",
    "evaluate_candidates",
    "extract_content_ids",
    "extract_episode_references",
    "extract_primary_episode_number",
    "extract_title",
    "find_best_cover",
    "jaro_winkler",
    "legacy_series_cover_score",
    "levenshtein_distance",
    "MINIMUM_HYBRID_COVER_SCORE",
    "normalize_text",
    "normalized_levenshtein",
    "parse_series_name",
    "sequence_ratio",
    "smart_title_score",
    "sorensen_dice",
    "tokenize",
    "token_jaccard",
    "token_overlap",
]
