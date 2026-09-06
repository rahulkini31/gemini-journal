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
