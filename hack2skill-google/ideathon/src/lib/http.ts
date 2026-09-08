/**
 * Shared `{"error": string}` response-shape extraction — both this app's
 * own backend (server.ts's safeError) and the separate ADK agent
 * prototype's backend (adk-agent/app/main.py's _safe_error) use the same
 * `{error: string}` contract, so App.tsx and src/lib/graphApi.ts (its two
 * callers) shared this exact check instead of each re-implementing it
 * (found by /simplify).
 */
export function safeErrorMessage(value: unknown, fallback: string): string {
  if (value && typeof value === "object" && "error" in value && typeof value.error === "string") {
    return value.error;
  }
  return fallback;
}
