import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, LoaderCircle, Network, Sparkles, X } from "lucide-react";
import { fetchEmotionalPatternGraph, fetchRelationshipGraph } from "../lib/graphApi";
import { EmotionalPatternGraph, RelationshipGraph } from "../types";
import GraphView, { GraphViewEdge, GraphViewNode } from "./GraphView";

interface PatternsPanelProps {
  idToken: string;
  onClose: () => void;
}

type Tab = "relationships" | "emotions";

const EMOTION_COLOR = "#f59e0b";
const TRIGGER_COLOR = "#6366f1";
const ENTRY_COLOR = "#0ea5e9";

export default function PatternsPanel({ idToken, onClose }: PatternsPanelProps) {
  const [tab, setTab] = useState<Tab>("relationships");
  const [relationshipGraph, setRelationshipGraph] = useState<RelationshipGraph | null>(null);
  const [emotionalGraph, setEmotionalGraph] = useState<EmotionalPatternGraph | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([fetchRelationshipGraph(idToken), fetchEmotionalPatternGraph(idToken)])
      .then(([relationships, emotions]) => {
        if (cancelled) return;
        setRelationshipGraph(relationships);
        setEmotionalGraph(emotions);
      })
      .catch((caught) => {
        if (cancelled) return;
        setError(caught instanceof Error ? caught.message : "The pattern graphs could not be loaded.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [idToken]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  // Memoized on the fetched graph objects (not recomputed every render):
  // GraphView's own useMemo only skips its 300-tick force simulation when
  // its nodes/edges props keep the same array reference, which a fresh
  // .map() on every PatternsPanel render would defeat even when the
  // underlying data hasn't changed (found by /simplify).
  const { relationshipNodes, relationshipEdges } = useMemo(() => ({
    relationshipNodes: (relationshipGraph?.nodes || []).map((node): GraphViewNode => ({
      id: node.id,
      label: node.summary,
    })),
    relationshipEdges: (relationshipGraph?.edges || []).map((edge): GraphViewEdge => ({
      source: edge.source,
      target: edge.target,
      label: edge.reason,
    })),
  }), [relationshipGraph]);

  const { emotionalNodes, emotionalEdges } = useMemo(() => ({
    emotionalNodes: (emotionalGraph?.graph.nodes || []).map((node): GraphViewNode => ({
      id: node.id,
      label: node.label,
      kind: node.kind,
    })),
    emotionalEdges: (emotionalGraph?.graph.edges || []).map((edge): GraphViewEdge => ({
      source: `trigger:${edge.trigger}`,
      target: `emotion:${edge.emotion}`,
      label: `${edge.mention_count} time${edge.mention_count === 1 ? "" : "s"}, avg intensity ${edge.average_intensity}`,
      weight: edge.mention_count,
    })),
  }), [emotionalGraph]);

  return (
    <div className="patterns-overlay" role="presentation" onClick={onClose}>
      <section
        className="patterns-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="patterns-heading"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="patterns-header">
          <h2 id="patterns-heading">Your reflection patterns</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close patterns view">
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <div className="patterns-tabs" role="tablist" aria-label="Pattern graph type">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "relationships"}
            className={tab === "relationships" ? "patterns-tab active" : "patterns-tab"}
            onClick={() => setTab("relationships")}
          >
            <Network size={16} aria-hidden="true" /> Related reflections
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "emotions"}
            className={tab === "emotions" ? "patterns-tab active" : "patterns-tab"}
            onClick={() => setTab("emotions")}
          >
            <Sparkles size={16} aria-hidden="true" /> Emotional patterns
          </button>
        </div>

        {loading ? (
          <p className="patterns-status" role="status">
            <LoaderCircle className="mini-spinner" aria-hidden="true" /> Loading your patterns…
          </p>
        ) : error ? (
          <p className="patterns-status error" role="alert">
            <AlertTriangle size={16} aria-hidden="true" /> {error}
          </p>
        ) : tab === "relationships" ? (
          <div role="tabpanel" aria-label="Related reflections graph">
            <GraphView
              nodes={relationshipNodes}
              edges={relationshipEdges}
              colorForKind={() => ENTRY_COLOR}
              emptyMessage="No related reflections yet. Save a few more entries to see connections between them."
            />
          </div>
        ) : (
          <div role="tabpanel" aria-label="Emotional patterns graph">
            <GraphView
              nodes={emotionalNodes}
              edges={emotionalEdges}
              colorForKind={(kind) => (kind === "emotion" ? EMOTION_COLOR : TRIGGER_COLOR)}
              emptyMessage="No emotional patterns yet. Once a few reflections share a theme and a feeling, they'll show up here."
            />
            {emotionalGraph && emotionalGraph.patterns.length > 0 && (
              <ul className="patterns-list" aria-label="Top patterns">
                {emotionalGraph.patterns.slice(0, 5).map((pattern, index) => (
                  <li key={`${pattern.trigger}-${pattern.emotion}-${index}`}>
                    <strong>{pattern.trigger}</strong> tends to bring up <strong>{pattern.emotion}</strong>
                    {" "}({pattern.mention_count} time{pattern.mention_count === 1 ? "" : "s"})
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
