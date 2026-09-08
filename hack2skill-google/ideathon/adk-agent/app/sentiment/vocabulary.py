"""Trigger-label canonicalization.

Trigger labels are open-ended (the model chooses the wording), but must not
fork into near-duplicates ("work stress" vs "stress at work" vs "job
pressure" all meaning the same thing) — otherwise the emotional-pattern
graph never accumulates enough mentions of any one trigger to show a real
pattern. There are two layers, matching the plan:

1. The model itself is shown the user's existing vocabulary and instructed
   to reuse a label that fits (see app/sentiment/extraction.py's prompt).
2. `canonicalize_trigger_label` is a code-side safety net that catches
   inconsistency the model didn't resolve on its own — a stdlib
   `difflib`-based near-duplicate snap, not a second model/embedding call.

There is deliberately no separate vocabulary document: the vocabulary is
derived fresh, each time, from the same `users/{uid}/interactions` documents
everything else already reads (the same pattern app/tools/graph_tools.py and
app/memory/firestore_memory_service.py use) — one less place for the label
set to drift out of sync with what's actually stored.
"""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from typing import Any

from app.config import MAX_TRIGGER_VOCABULARY_SIZE, TRIGGER_LABEL_SIMILARITY_THRESHOLD

# Small connector words stripped before token-overlap comparison, so "stress
# at work" and "work stress" compare as the same two content words rather
# than as three tokens with one non-matching filler word.
_MINOR_WORDS = frozenset({"a", "an", "the", "at", "in", "on", "of", "with", "and", "my"})


def normalize_label(label: str) -> str:
    return " ".join(label.strip().lower().split())


def _content_tokens(label: str) -> set[str]:
    return {word for word in normalize_label(label).split() if word not in _MINOR_WORDS}


def _label_similarity(a: str, b: str) -> float:
    """Word-reordering and minor-phrasing tolerant: "work stress" and
    "stress at work" share the same content words in a different order and
    score 1.0 here, even though a plain character-sequence ratio on the raw
    strings scores them only ~0.48. Falls back to a character ratio when
    either label has no content tokens left (e.g. single short words like
    "sleep" vs "sleeping"), so that case isn't missed either.

    This intentionally does NOT catch true synonyms with no shared words
    (e.g. "job pressure" vs "work stress") — that is the model's own job,
    given the existing vocabulary in its prompt (see
    app/sentiment/extraction.py::build_extraction_instruction); this is only
    the code-side safety net for surface-level inconsistency.
    """
    tokens_a, tokens_b = _content_tokens(a), _content_tokens(b)
    char_ratio = SequenceMatcher(None, normalize_label(a), normalize_label(b)).ratio()
    if not tokens_a or not tokens_b:
        return char_ratio
    jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    return max(jaccard, char_ratio)


def load_trigger_vocabulary(db: Any, uid: str) -> list[str]:
    """Returns up to MAX_TRIGGER_VOCABULARY_SIZE existing trigger labels for
    this user, most-frequently-mentioned first — the more established a
    label is, the more worth reusing it is."""
    counts: Counter[str] = Counter()
    docs = (
        db.collection(f"users/{uid}/interactions")
        .where("sentimentStatus", "==", "completed")
        .stream()
    )
    for doc in docs:
        data = doc.to_dict() or {}
        triggers = (data.get("sentimentAnalysis") or {}).get("triggers") or []
        for trigger in triggers:
            label = trigger.get("label") if isinstance(trigger, dict) else None
            if isinstance(label, str) and label.strip():
                counts[normalize_label(label)] += 1
    return [label for label, _count in counts.most_common(MAX_TRIGGER_VOCABULARY_SIZE)]


def canonicalize_trigger_label(proposed: str, vocabulary: list[str]) -> str:
    """Snaps `proposed` to the closest existing vocabulary entry when they
    are near-duplicates; otherwise returns `proposed` unchanged (a
    genuinely new label). Comparison is case/whitespace-insensitive but the
    returned string preserves the existing vocabulary's original casing so
    the graph doesn't accumulate case-variant duplicates either."""
    normalized_proposed = normalize_label(proposed)
    if not normalized_proposed:
        return normalized_proposed
    best_label = None
    best_score = 0.0
    for existing in vocabulary:
        score = _label_similarity(normalized_proposed, existing)
        if score > best_score:
            best_score, best_label = score, existing
    if best_label is not None and best_score >= TRIGGER_LABEL_SIMILARITY_THRESHOLD:
        return best_label
    return normalized_proposed
