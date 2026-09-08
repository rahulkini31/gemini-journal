/**
 * Client for the experimental ADK agent prototype's graph endpoints
 * (adk-agent/app/main.py: GET /api/graph/relationships,
 * GET /api/graph/emotional-patterns). This is a genuinely separate service
 * from this app's own backend (server.ts) — a deliberate, narrow exception
 * documented in adk-agent/README.md — so it needs its own base URL and
 * calls are cross-origin, not the relative `/api/...` paths the rest of
 * this app uses.
 */
import { EmotionalPatternGraph, RelationshipGraph } from "../types";

const ADK_AGENT_URL = (import.meta.env.VITE_ADK_AGENT_URL as string | undefined)?.replace(/\/+$/, "") || "http://localhost:8090";

async function getGraph<T>(path: string, idToken: string): Promise<T> {
  const response = await fetch(`${ADK_AGENT_URL}${path}`, {
    headers: { Authorization: `Bearer ${idToken}` },
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const message = data && typeof data === "object" && "error" in data && typeof data.error === "string"
      ? data.error
      : "The pattern graph could not be loaded.";
    throw new Error(message);
  }
  return data as T;
}

export function fetchRelationshipGraph(idToken: string): Promise<RelationshipGraph> {
  return getGraph<RelationshipGraph>("/api/graph/relationships", idToken);
}

export function fetchEmotionalPatternGraph(idToken: string): Promise<EmotionalPatternGraph> {
  return getGraph<EmotionalPatternGraph>("/api/graph/emotional-patterns", idToken);
}
