"""The relationship-graph recall tool ("Obsidian/graphify"-style).

This is the capability beyond the original Memory Threads design: instead of
only a flat top-K similarity search, this builds a small graph over the
user's own reflections and traverses it, so an entry can surface as related
even when it is not directly similar to the query — the way linking through
an intermediate note surfaces related material in a tool like Obsidian's
graph view.

Design invariants, matching the rest of this prototype:
- Built **in-memory, per request**, scoped to the calling user's own
  interactions only — never cross-user, never persisted as a new store.
- Edges come from (a) embedding cosine similarity above a threshold, reusing
  the same embeddings already computed for Memory Threads recall (no extra
  model cost), and (b) cheap shared-keyword overlap (no model call at all).
- No new paid graph database — `networkx` is a free, in-process library.
- Never returns raw embedding vectors.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import networkx as nx

from app.config import MAX_GRAPH_NODES
from app.embedded_interactions import load_embedded_interactions
from app.similarity import cosine_similarity
from app.tools.journal_tools import JournalToolError, require_uid

SIMILARITY_EDGE_THRESHOLD = 0.75
MIN_SHARED_KEYWORDS = 2
DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_RESULTS = 5

_WORD_RE = re.compile(r"[A-Za-z']{4,}")
_STOPWORDS = frozenset({
    "this", "that", "with", "have", "were", "been", "will", "about",
    "there", "their", "what", "when", "where", "which", "would", "could",
    "should", "still", "today", "reflection", "journal",
})


def _keywords(text: str) -> set[str]:
    return {word.lower() for word in _WORD_RE.findall(text) if word.lower() not in _STOPWORDS}


@dataclass(frozen=True)
class GraphNode:
    interaction_id: str
    summary: str
    keywords: set[str]
    embedding: list[float]


def _build_graph(nodes: list[GraphNode]) -> nx.Graph:
    graph = nx.Graph()
    for node in nodes:
        graph.add_node(node.interaction_id, summary=node.summary)
    for a, b in itertools.combinations(nodes, 2):
        reasons = []
        if a.embedding and b.embedding:
            similarity = cosine_similarity(a.embedding, b.embedding)
            if similarity >= SIMILARITY_EDGE_THRESHOLD:
                reasons.append(f"similar reflections (score {similarity:.2f})")
        shared = a.keywords & b.keywords
        if len(shared) >= MIN_SHARED_KEYWORDS:
            reasons.append(f"shared themes: {', '.join(sorted(shared)[:5])}")
        if reasons:
            graph.add_edge(a.interaction_id, b.interaction_id, reason="; ".join(reasons))
    return graph


def get_full_relationship_graph(*, db: Any, uid: str) -> dict:
    """Returns the whole relationship graph — every one of the user's own
    embedded reflections as a node, every edge above the same similarity/
    keyword thresholds `find_related_reflections` uses — with no query and
    no BFS traversal. `find_related_reflections` is right for a chat tool
    ("what's related to this one thing"); a visualization wants the whole
    picture, not a conversational subset. Reuses `_load_user_nodes`/
    `_build_graph` unchanged.

    Unlike the tool's `related` results, summaries here are plain text, not
    `<journal-data>`-wrapped — that wrapping exists to keep the text inert
    when it re-enters a model prompt, which a direct HTTP response for a UI
    never does.
    """
    nodes = list(_load_user_nodes(db, uid))
    graph = _build_graph(nodes)
    graph_nodes = [{"id": node.interaction_id, "summary": node.summary} for node in nodes]
    graph_edges = [
        {"source": source, "target": target, "reason": data["reason"]}
        for source, target, data in graph.edges(data=True)
    ]
    return {"nodes": graph_nodes, "edges": graph_edges}


def build_graph_tool(*, db: Any, embedding_client: Any) -> Callable:
    async def find_related_reflections(
        query: str,
        tool_context: Any,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> dict:
        """Find reflections related to `query` by walking a relationship
        graph of the user's own past entries, not just plain similarity —
        this can surface entries connected through a shared theme even when
        they are not directly similar to the query itself. Only call this
        when the user is explicitly looking for patterns, themes, or
        connections across their reflections.

        Args:
            query: What to explore, in the user's own words.
            max_depth: How many relationship hops to follow (1-2). Depth 1
                is direct matches only; depth 2 also includes reflections
                connected through an intermediate one.
            max_results: Maximum number of related reflections to return.
        """
        uid = require_uid(tool_context)
        query = query.strip()
        if not query:
            raise JournalToolError("INVALID_INPUT", "A relationship query cannot be empty.")
        max_depth = max(1, min(2, max_depth))
        max_results = max(1, min(DEFAULT_MAX_RESULTS, max_results))

        nodes = list(_load_user_nodes(db, uid))
        if not nodes:
            return {"status": "success", "related": [], "note": "No embedded reflections yet."}

        graph = _build_graph(nodes)
        query_embedding = list(embedding_client.embed(query))
        query_keywords = _keywords(query)

        # Seed: the node(s) most similar to the query itself, then traverse
        # outward from there — this is what makes depth-2 results possible
        # even when nothing in the corpus is directly similar to `query`.
        scored_seeds = sorted(
            nodes,
            key=lambda node: cosine_similarity(query_embedding, node.embedding) if node.embedding else 0.0,
            reverse=True,
        )
        seed_ids = [node.interaction_id for node in scored_seeds[:3] if node.interaction_id in graph]

        related_ids: set[str] = set()
        for seed_id in seed_ids:
            lengths = nx.single_source_shortest_path_length(graph, seed_id, cutoff=max_depth)
            related_ids.update(node_id for node_id in lengths if node_id != seed_id)

        by_id = {node.interaction_id: node for node in nodes}
        results = []
        for node_id in list(related_ids)[:max_results]:
            node = by_id.get(node_id)
            if not node:
                continue
            # Reason: the shortest connecting edge's reason, if directly
            # connected to a seed, else a generic transitive-path note.
            reason = "connected through a shared theme across your reflections"
            for seed_id in seed_ids:
                if graph.has_edge(seed_id, node_id):
                    reason = graph[seed_id][node_id]["reason"]
                    break
            results.append({
                "interaction_id": node.interaction_id,
                "summary": f"<journal-data>{node.summary}</journal-data>",
                "reason": reason,
            })

        return {"status": "success", "related": results}

    return find_related_reflections


def _load_user_nodes(db: Any, uid: str) -> Iterable[GraphNode]:
    for item in load_embedded_interactions(db, uid, limit=MAX_GRAPH_NODES):
        yield GraphNode(
            interaction_id=item.interaction_id,
            summary=item.summary,
            keywords=_keywords(f"{item.summary} {item.user_prompt}"),
            embedding=item.embedding,
        )
