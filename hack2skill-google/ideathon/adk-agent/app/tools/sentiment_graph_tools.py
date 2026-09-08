"""The emotional-pattern graph tool: "which thing made the user feel what."

Distinct from app/tools/graph_tools.py's `find_related_reflections` (which
links entries to entries by similarity/keywords): this tool aggregates the
sentiment/trigger data already extracted for each completed interaction
(app/sentiment/extraction.py, stored via
app/tools/journal_tools.py::record_sentiment_analysis) into a
`Trigger --EVOKES--> Emotion` graph, so a repeated pattern like "work-related
entries tend toward frustrated" becomes a countable, explainable fact
instead of an impression.

Design invariants, matching the rest of this prototype:
- Built in-memory, per request, scoped to the calling user's own
  interactions only — never cross-user, never persisted as a new store.
- Label-based, not embedding-based — there is no vector to leak here.
- A pattern is only reported once it has at least `min_mentions`
  occurrences, so a single journal entry never reads as a settled trend.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Iterable, Optional

import networkx as nx

from app.config import MIN_MENTIONS_FOR_INSIGHT
from app.tools.journal_tools import require_uid

MAX_EXAMPLE_PHRASES_PER_EDGE = 3
MAX_PATTERNS_RETURNED = 10


def _normalize(label: str) -> str:
    return " ".join(label.strip().lower().split())


def _load_sentiment_records(db: Any, uid: str) -> Iterable[tuple[list[dict], list[dict]]]:
    docs = (
        db.collection(f"users/{uid}/interactions")
        .where("sentimentStatus", "==", "completed")
        .stream()
    )
    for doc in docs:
        data = doc.to_dict() or {}
        analysis = data.get("sentimentAnalysis") or {}
        emotions = analysis.get("emotions") or []
        triggers = analysis.get("triggers") or []
        if emotions and triggers:
            yield emotions, triggers


def _build_pattern_graph(records: Iterable[tuple[list[dict], list[dict]]]) -> tuple[nx.MultiDiGraph, dict]:
    """Returns (graph, edge_stats). edge_stats[(trigger, emotion)] holds the
    aggregated count/intensity/example phrases behind each edge — the graph
    itself carries the same information as (kind, label) node keys plus
    weighted edges, for a caller that wants the raw network shape."""
    graph = nx.MultiDiGraph()
    edge_stats: dict[tuple[str, str], dict] = defaultdict(lambda: {"count": 0, "intensity_sum": 0.0, "phrases": []})

    for emotions, triggers in records:
        for trigger in triggers:
            trigger_label = trigger.get("label")
            if not trigger_label:
                continue
            trigger_phrase = trigger.get("phrase") or ""
            trigger_intensity = float(trigger.get("intensity") or 0.0)
            graph.add_node(("trigger", trigger_label))

            for emotion in emotions:
                emotion_label = emotion.get("label")
                if not emotion_label:
                    continue
                emotion_intensity = float(emotion.get("intensity") or 0.0)
                graph.add_node(("emotion", emotion_label))

                key = (trigger_label, emotion_label)
                stats = edge_stats[key]
                stats["count"] += 1
                # How strongly this trigger and emotion co-occurred in this
                # one entry — averaged across entries below for the edge's
                # overall intensity.
                stats["intensity_sum"] += (trigger_intensity + emotion_intensity) / 2
                if trigger_phrase and len(stats["phrases"]) < MAX_EXAMPLE_PHRASES_PER_EDGE:
                    stats["phrases"].append(trigger_phrase)
                graph.add_edge(("trigger", trigger_label), ("emotion", emotion_label))

    return graph, edge_stats


def _compute_patterns(
    *,
    db: Any,
    uid: str,
    trigger_label: Optional[str],
    min_mentions: int,
    wrap_phrases: bool,
) -> dict:
    """Shared by the agent tool and the direct HTTP endpoint below.
    `wrap_phrases` controls the `<journal-data>` untrusted-data wrapping:
    on for the tool (its output can re-enter a model prompt), off for the
    HTTP endpoint (a direct response for a UI, which should show plain
    text, not literal wrapper tags).
    """
    min_mentions = max(1, min_mentions)
    records = list(_load_sentiment_records(db, uid))
    if not records:
        return {"patterns": [], "graph": {"nodes": [], "edges": []}, "note": "No analyzed reflections yet."}

    graph, edge_stats = _build_pattern_graph(records)

    normalized_filter = _normalize(trigger_label) if trigger_label else None
    patterns = []
    graph_edges = []
    for (trigger, emotion), stats in edge_stats.items():
        if stats["count"] < min_mentions:
            continue
        average_intensity = round(stats["intensity_sum"] / stats["count"], 2)
        graph_edges.append({
            "trigger": trigger,
            "emotion": emotion,
            "mention_count": stats["count"],
            "average_intensity": average_intensity,
        })
        if normalized_filter and _normalize(trigger) != normalized_filter:
            continue
        phrases = stats["phrases"]
        patterns.append({
            "trigger": trigger,
            "emotion": emotion,
            "mention_count": stats["count"],
            "average_intensity": average_intensity,
            "example_phrases": [f"<journal-data>{p}</journal-data>" for p in phrases] if wrap_phrases else list(phrases),
        })

    # Strongest, best-supported patterns first: most mentions, then highest
    # average intensity.
    patterns.sort(key=lambda pattern: (pattern["mention_count"], pattern["average_intensity"]), reverse=True)
    graph_nodes = [{"id": f"{kind}:{label}", "kind": kind, "label": label} for kind, label in graph.nodes()]

    return {
        "patterns": patterns[:MAX_PATTERNS_RETURNED],
        "graph": {"nodes": graph_nodes, "edges": graph_edges},
    }


def get_full_emotional_pattern_graph(*, db: Any, uid: str, min_mentions: int = MIN_MENTIONS_FOR_INSIGHT) -> dict:
    """Direct, non-agent accessor for the whole pattern graph — see
    `_compute_patterns`'s docstring for why this returns plain text where
    the tool below returns `<journal-data>`-wrapped text."""
    return _compute_patterns(db=db, uid=uid, trigger_label=None, min_mentions=min_mentions, wrap_phrases=False)


def build_sentiment_graph_tool(*, db: Any) -> Callable:
    async def analyze_emotional_patterns(
        tool_context: Any,
        trigger_label: Optional[str] = None,
        min_mentions: int = MIN_MENTIONS_FOR_INSIGHT,
    ) -> dict:
        """Find patterns in what tends to cause the user's feelings across
        all of their own past reflections — "which thing made me feel what."
        Only call this when the user explicitly asks about patterns, moods,
        or what tends to affect how they feel; never call it unasked.

        Args:
            trigger_label: Optional — narrow to one specific trigger (e.g.
                "work") to see just its emotional breakdown.
            min_mentions: Minimum number of times a trigger-emotion pattern
                must occur to be reported, so a single occurrence never
                reads as a settled pattern.
        """
        uid = require_uid(tool_context)
        result = _compute_patterns(
            db=db, uid=uid, trigger_label=trigger_label, min_mentions=min_mentions, wrap_phrases=True,
        )
        return {"status": "success", **result}

    return analyze_emotional_patterns
