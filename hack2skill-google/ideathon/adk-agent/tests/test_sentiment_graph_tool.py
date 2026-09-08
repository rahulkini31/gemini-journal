"""Aggregation correctness, min_mentions filtering, and owner isolation for
app/tools/sentiment_graph_tools.py::analyze_emotional_patterns.
"""

from __future__ import annotations

import pytest

from app.tools.sentiment_graph_tools import build_sentiment_graph_tool
from tests.fakes import FakeFirestore, FakeToolContext

USER_A = "user-a"
USER_B = "user-b"


def _seed(db: FakeFirestore, uid: str, doc_id: str, emotions: list[dict], triggers: list[dict]):
    db.document(f"users/{uid}/interactions/{doc_id}").set({
        "sentimentStatus": "completed",
        "sentimentAnalysis": {"emotions": emotions, "triggers": triggers},
    })


@pytest.fixture
def db_with_a_repeated_work_pattern():
    db = FakeFirestore()
    # Three "work" entries: two frustrated, one motivated — a real pattern
    # with a clear dominant emotion, not a coin flip.
    _seed(db, USER_A, "e1",
          [{"label": "frustrated", "intensity": 0.8}],
          [{"label": "work", "phrase": "a tight deadline", "intensity": 0.9}])
    _seed(db, USER_A, "e2",
          [{"label": "frustrated", "intensity": 0.6}],
          [{"label": "work", "phrase": "a difficult meeting", "intensity": 0.7}])
    _seed(db, USER_A, "e3",
          [{"label": "motivated", "intensity": 0.7}],
          [{"label": "work", "phrase": "shipped a project", "intensity": 0.8}])
    # A single "family" mention — must not read as a pattern (min_mentions).
    _seed(db, USER_A, "e4",
          [{"label": "joy", "intensity": 0.9}],
          [{"label": "family", "phrase": "dinner with parents", "intensity": 0.6}])
    # Another user's data, which must never appear in USER_A's results.
    _seed(db, USER_B, "other", [{"label": "sad", "intensity": 0.5}], [{"label": "work", "phrase": "n/a", "intensity": 0.5}])
    return db


@pytest.mark.asyncio
async def test_repeated_pattern_is_reported_with_correct_counts_and_average_intensity(db_with_a_repeated_work_pattern):
    tool = build_sentiment_graph_tool(db=db_with_a_repeated_work_pattern)
    result = await tool(FakeToolContext(uid=USER_A))

    work_frustrated = next(p for p in result["patterns"] if p["trigger"] == "work" and p["emotion"] == "frustrated")
    assert work_frustrated["mention_count"] == 2
    # Each entry's edge intensity blends that entry's trigger AND emotion
    # intensity (e1: (0.9+0.8)/2=0.85, e2: (0.7+0.6)/2=0.65), then averages
    # across the 2 mentions.
    assert work_frustrated["average_intensity"] == round(((0.9 + 0.8) / 2 + (0.7 + 0.6) / 2) / 2, 2)
    assert len(work_frustrated["example_phrases"]) == 2


@pytest.mark.asyncio
async def test_single_mention_pattern_is_filtered_out_by_default_min_mentions(db_with_a_repeated_work_pattern):
    tool = build_sentiment_graph_tool(db=db_with_a_repeated_work_pattern)
    result = await tool(FakeToolContext(uid=USER_A))

    trigger_emotion_pairs = {(p["trigger"], p["emotion"]) for p in result["patterns"]}
    assert ("family", "joy") not in trigger_emotion_pairs  # only one mention
    assert ("work", "motivated") not in trigger_emotion_pairs  # also only one mention


@pytest.mark.asyncio
async def test_lowering_min_mentions_surfaces_single_occurrence_patterns(db_with_a_repeated_work_pattern):
    tool = build_sentiment_graph_tool(db=db_with_a_repeated_work_pattern)
    result = await tool(FakeToolContext(uid=USER_A), min_mentions=1)

    trigger_emotion_pairs = {(p["trigger"], p["emotion"]) for p in result["patterns"]}
    assert ("family", "joy") in trigger_emotion_pairs


@pytest.mark.asyncio
async def test_trigger_label_filter_narrows_to_one_triggers_breakdown(db_with_a_repeated_work_pattern):
    tool = build_sentiment_graph_tool(db=db_with_a_repeated_work_pattern)
    result = await tool(FakeToolContext(uid=USER_A), trigger_label="work", min_mentions=1)

    triggers_in_result = {p["trigger"] for p in result["patterns"]}
    assert triggers_in_result == {"work"}


@pytest.mark.asyncio
async def test_never_returns_another_users_patterns(db_with_a_repeated_work_pattern):
    tool = build_sentiment_graph_tool(db=db_with_a_repeated_work_pattern)
    result = await tool(FakeToolContext(uid=USER_A), min_mentions=1)

    for pattern in result["patterns"]:
        for phrase in pattern["example_phrases"]:
            assert "n/a" not in phrase  # user B's placeholder phrase, would only appear on cross-user leakage


@pytest.mark.asyncio
async def test_graph_shape_includes_trigger_and_emotion_nodes(db_with_a_repeated_work_pattern):
    tool = build_sentiment_graph_tool(db=db_with_a_repeated_work_pattern)
    result = await tool(FakeToolContext(uid=USER_A), min_mentions=1)

    node_ids = {node["id"] for node in result["graph"]["nodes"]}
    assert "trigger:work" in node_ids
    assert "emotion:frustrated" in node_ids


@pytest.mark.asyncio
async def test_no_analyzed_reflections_yet_returns_an_empty_result_not_an_error():
    db = FakeFirestore()
    tool = build_sentiment_graph_tool(db=db)
    result = await tool(FakeToolContext(uid=USER_A))
    assert result["status"] == "success"
    assert result["patterns"] == []
