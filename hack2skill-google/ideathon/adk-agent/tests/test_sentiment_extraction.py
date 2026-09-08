"""Defensive parsing, canonicalization, and the title-mandatory /
sentiment-optional failure split for app/sentiment/extraction.py and
app/sentiment/vocabulary.py.
"""

from __future__ import annotations

import json

from app.sentiment.extraction import EmotionEntry, parse_extraction_response
from app.sentiment.vocabulary import canonicalize_trigger_label, load_trigger_vocabulary
from tests.fakes import FakeFirestore

UID = "user-a"


def _response(title="A quiet morning", emotions=None, triggers=None) -> str:
    return json.dumps({
        "title": title,
        "emotions": emotions if emotions is not None else [{"label": "calm", "intensity": 0.8}],
        "triggers": triggers if triggers is not None else [{"label": "nature", "phrase": "a walk outside", "intensity": 0.7}],
    })


def test_well_formed_response_parses_completely():
    result = parse_extraction_response(_response(), vocabulary=[])
    assert result.title == "A quiet morning"
    assert result.emotions == [EmotionEntry(label="calm", intensity=0.8)]
    assert result.triggers[0].label == "nature"
    assert result.triggers[0].phrase == "a walk outside"
    assert result.sentiment_status == "completed"


def test_missing_title_is_a_hard_failure_signal():
    result = parse_extraction_response(json.dumps({"emotions": [], "triggers": []}), vocabulary=[])
    assert result.title is None


def test_completely_unparseable_text_still_returns_a_safe_result():
    result = parse_extraction_response("not json at all, just prose", vocabulary=[])
    assert result.title is None
    assert result.emotions == []
    assert result.triggers == []
    assert result.sentiment_status == "failed"


def test_valid_title_with_malformed_sentiment_degrades_without_losing_the_title():
    raw = json.dumps({"title": "Still a valid title", "emotions": "not a list", "triggers": None})
    result = parse_extraction_response(raw, vocabulary=[])
    assert result.title == "Still a valid title"
    assert result.emotions == []
    assert result.triggers == []
    assert result.sentiment_status == "failed"


def test_off_taxonomy_emotion_labels_are_dropped_not_coerced():
    raw = _response(emotions=[{"label": "ecstatic", "intensity": 1.0}, {"label": "calm", "intensity": 0.5}])
    result = parse_extraction_response(raw, vocabulary=[])
    labels = [emotion.label for emotion in result.emotions]
    assert "ecstatic" not in labels
    assert "calm" in labels


def test_intensity_is_clamped_to_zero_one_range():
    raw = _response(emotions=[{"label": "calm", "intensity": 5.0}, {"label": "sad", "intensity": -2.0}])
    result = parse_extraction_response(raw, vocabulary=[])
    intensities = {emotion.label: emotion.intensity for emotion in result.emotions}
    assert intensities["calm"] == 1.0
    assert intensities["sad"] == 0.0


def test_emotions_and_triggers_are_capped_at_the_configured_maximum():
    from app.config import MAX_EMOTIONS_PER_ENTRY, MAX_TRIGGERS_PER_ENTRY

    many_emotions = [{"label": "calm", "intensity": 0.5} for _ in range(MAX_EMOTIONS_PER_ENTRY + 5)]
    many_triggers = [{"label": f"trigger-{i}", "phrase": "x", "intensity": 0.5} for i in range(MAX_TRIGGERS_PER_ENTRY + 5)]
    raw = _response(emotions=many_emotions, triggers=many_triggers)
    result = parse_extraction_response(raw, vocabulary=[])
    assert len(result.emotions) == MAX_EMOTIONS_PER_ENTRY
    assert len(result.triggers) == MAX_TRIGGERS_PER_ENTRY


def test_word_reordered_near_duplicate_snaps_to_the_existing_label():
    # The exact case the graph must not fork on: same two content words,
    # different order/filler word.
    canonical = canonicalize_trigger_label("stress at work", ["work stress", "sleep"])
    assert canonical == "work stress"


def test_plural_or_tense_variant_snaps_to_the_existing_label():
    canonical = canonicalize_trigger_label("sleeping", ["sleep", "work stress"])
    assert canonical == "sleep"


def test_a_genuinely_new_trigger_label_is_kept_as_is():
    canonical = canonicalize_trigger_label("gardening", ["work stress", "sleep"])
    assert canonical == "gardening"


def test_true_synonyms_with_no_shared_words_are_not_merged_by_the_code_safety_net():
    # "job pressure" and "work stress" share no words, so the token-overlap
    # check alone can't merge them — that's the model's job, given the
    # vocabulary in its prompt (build_extraction_instruction), not this
    # function's. Documented here so the boundary is explicit, not assumed.
    canonical = canonicalize_trigger_label("job pressure", ["work stress"])
    assert canonical == "job pressure"


def test_extraction_response_runs_trigger_labels_through_canonicalization():
    raw = _response(triggers=[{"label": "stress at work", "phrase": "a tight deadline", "intensity": 0.9}])
    result = parse_extraction_response(raw, vocabulary=["work stress"])
    assert result.triggers[0].label == "work stress"


def test_load_trigger_vocabulary_ranks_by_frequency_and_is_owner_scoped():
    db = FakeFirestore()
    for i in range(3):
        db.document(f"users/{UID}/interactions/a{i}").set({
            "sentimentStatus": "completed",
            "sentimentAnalysis": {"triggers": [{"label": "work"}]},
        })
    db.document(f"users/{UID}/interactions/b0").set({
        "sentimentStatus": "completed",
        "sentimentAnalysis": {"triggers": [{"label": "sleep"}]},
    })
    db.document("users/user-b/interactions/c0").set({
        "sentimentStatus": "completed",
        "sentimentAnalysis": {"triggers": [{"label": "someone-elses-trigger"}]},
    })

    vocabulary = load_trigger_vocabulary(db, UID)
    assert vocabulary[0] == "work"  # most frequent first
    assert "sleep" in vocabulary
    assert "someone-elses-trigger" not in vocabulary
