export interface ChatMessage {
  role: "user" | "model";
  text: string;
}

export interface JournalInteraction {
  id: string;
  userPrompt: string;
  assistantResponse: string;
  automaticSessionSummary: string;
  sessionId: string;
  turnOrder: number;
  modelUsed?: string;
  timestamps: {
    epochMs?: number;
  };
  status: "pending" | "failed" | "completed" | "success";
  idempotencyRequestId: string;
}

/** Values come from the backend; the browser never derives service-wide usage. */
export interface QuotaStatus {
  completedInteractionCount: number;
  completedInteractionLimit: number;
  monthlyAttemptCount: number;
  monthlyAttemptLimit: number;
  cooldownSeconds: number;
}

/**
 * Graph data from the experimental ADK agent prototype
 * (adk-agent/app/tools/graph_tools.py, adk-agent/app/tools/sentiment_graph_tools.py),
 * fetched directly from that separate service — see src/lib/graphApi.ts.
 */
export interface RelationshipGraphNode {
  id: string;
  summary: string;
}

export interface RelationshipGraphEdge {
  source: string;
  target: string;
  reason: string;
}

export interface RelationshipGraph {
  nodes: RelationshipGraphNode[];
  edges: RelationshipGraphEdge[];
}

export interface EmotionalPatternGraphNode {
  id: string;
  kind: "trigger" | "emotion";
  label: string;
}

export interface EmotionalPatternGraphEdge {
  trigger: string;
  emotion: string;
  mention_count: number;
  average_intensity: number;
}

export interface EmotionalPattern extends EmotionalPatternGraphEdge {
  example_phrases: string[];
}

export interface EmotionalPatternGraph {
  patterns: EmotionalPattern[];
  graph: {
    nodes: EmotionalPatternGraphNode[];
    edges: EmotionalPatternGraphEdge[];
  };
}
