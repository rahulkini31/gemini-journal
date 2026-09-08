import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { onAuthStateChanged, User } from "firebase/auth";
import { auth, getAppCheckToken, signInWithGoogle, signOut } from "./lib/firebase";
import { ChatMessage, JournalInteraction, QuotaStatus } from "./types";
import PatternsPanel from "./components/PatternsPanel";
import {
  AlertTriangle,
  BookOpen,
  CheckCircle2,
  Clock3,
  History,
  LoaderCircle,
  LogOut,
  MessageSquare,
  Network,
  PenLine,
  Plus,
  Send,
  ShieldCheck,
  Sparkles,
} from "lucide-react";

const BODY_LIMIT_BYTES = 4 * 1024;
const APPROX_INPUT_TOKEN_LIMIT = 1_000;

type HistoryLoadOptions = { restoreLatest?: boolean };

function newRequestId(): string {
  return crypto.randomUUID();
}

function safeErrorMessage(value: unknown, fallback: string): string {
  if (value && typeof value === "object" && "error" in value && typeof value.error === "string") {
    return value.error;
  }
  return fallback;
}

function formatTime(epochMs?: number): string {
  if (!epochMs) return "Saved reflection";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(epochMs);
}

function buildConversation(entries: JournalInteraction[]): ChatMessage[] {
  return entries
    .slice()
    .sort((a, b) => (a.turnOrder - b.turnOrder) || ((a.timestamps?.epochMs || 0) - (b.timestamps?.epochMs || 0)))
    .flatMap((entry) => [
      { role: "user" as const, text: entry.userPrompt },
      { role: "model" as const, text: entry.assistantResponse },
    ]);
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loadingAuth, setLoadingAuth] = useState(true);
  const [history, setHistory] = useState<JournalInteraction[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [quota, setQuota] = useState<QuotaStatus | null>(null);
  const [quotaNotice, setQuotaNotice] = useState("Checking service capacity…");
  const [activeSessionId, setActiveSessionId] = useState("");
  const [activeConversation, setActiveConversation] = useState<ChatMessage[]>([]);
  const [currentPrompt, setCurrentPrompt] = useState("");
  const [idempotencyRequestId, setIdempotencyRequestId] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [retryAvailable, setRetryAvailable] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [patternsToken, setPatternsToken] = useState<string | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const signInRef = useRef<HTMLButtonElement>(null);
  const messageEndRef = useRef<HTMLDivElement>(null);
  const requestInFlightRef = useRef(false);

  const requestBody = useMemo(() => ({
    prompt: currentPrompt,
    sessionId: activeSessionId,
    idempotencyRequestId,
  }), [activeSessionId, currentPrompt, idempotencyRequestId]);
  const requestBytes = useMemo(() => new Blob([JSON.stringify(requestBody)]).size, [requestBody]);
  const approximateTokens = useMemo(() => Math.ceil(Array.from(currentPrompt).length / 4), [currentPrompt]);
  const isPayloadTooLarge = requestBytes > BODY_LIMIT_BYTES;
  const isPromptTooLarge = approximateTokens > APPROX_INPUT_TOKEN_LIMIT;
  const cooldownSeconds = Math.max(0, quota?.cooldownSeconds || 0);
  const capacityReached = quota ? quota.completedInteractionCount >= quota.completedInteractionLimit : false;
  const modelAttemptCapacityReached = quota ? quota.monthlyAttemptCount >= quota.monthlyAttemptLimit : false;

  const sessionPreviews = useMemo(() => {
    const seen = new Set<string>();
    return history
      .slice()
      .sort((a, b) => (b.timestamps?.epochMs || 0) - (a.timestamps?.epochMs || 0))
      .filter((entry) => {
        if (seen.has(entry.sessionId)) return false;
        seen.add(entry.sessionId);
        return true;
      });
  }, [history]);

  const startNewSession = () => {
    if (currentPrompt.trim()) {
      setErrorMessage("Your draft is still here. Save it, retry it, or clear it before starting a new session.");
      composerRef.current?.focus();
      return;
    }
    setActiveSessionId(newRequestId());
    setIdempotencyRequestId(newRequestId());
    setActiveConversation([]);
    setRetryAvailable(false);
    setErrorMessage(null);
    setSuccessMessage(null);
  };

  const fetchQuota = async (idToken?: string) => {
    if (!auth.currentUser && !idToken) return;
    try {
      const token = idToken || await auth.currentUser!.getIdToken();
      const response = await fetch("/api/journal/quota", { headers: { Authorization: `Bearer ${token}` } });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(safeErrorMessage(data, "Service capacity could not be checked."));
      if (
        !data ||
        typeof data.completedInteractionCount !== "number" ||
        typeof data.completedInteractionLimit !== "number" ||
        typeof data.monthlyAttemptCount !== "number" ||
        typeof data.monthlyAttemptLimit !== "number" ||
        typeof data.cooldownSeconds !== "number"
      ) {
        throw new Error("Service capacity returned an invalid status.");
      }
      setQuota(data);
      setQuotaNotice("Service capacity is up to date.");
    } catch {
      setQuota(null);
      setQuotaNotice("Capacity status is temporarily unavailable. The server remains authoritative.");
    }
  };

  const restoreSession = (sessionId: string, entries = history) => {
    const sessionEntries = entries.filter((entry) => entry.sessionId === sessionId);
    setActiveSessionId(sessionId);
    setActiveConversation(buildConversation(sessionEntries));
    setIdempotencyRequestId(newRequestId());
    setCurrentPrompt("");
    setRetryAvailable(false);
    setErrorMessage(null);
    requestAnimationFrame(() => composerRef.current?.focus());
  };

  const fetchHistory = async ({ restoreLatest = false }: HistoryLoadOptions = {}) => {
    if (!auth.currentUser) return;
    setLoadingHistory(true);
    try {
      const idToken = await auth.currentUser.getIdToken();
      const response = await fetch("/api/journal/history", { headers: { Authorization: `Bearer ${idToken}` } });
      const data = await response.json().catch(() => null);
      if (!response.ok || !Array.isArray(data)) {
        throw new Error(safeErrorMessage(data, "Could not load your saved reflections."));
      }
      setHistory(data);
      if (restoreLatest && data.length) {
        const latest = data.slice().sort((a: JournalInteraction, b: JournalInteraction) =>
          (b.timestamps?.epochMs || 0) - (a.timestamps?.epochMs || 0),
        )[0];
        restoreSession(latest.sessionId, data);
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Could not load your saved reflections.");
    } finally {
      setLoadingHistory(false);
    }
  };

  useEffect(() => {
    const unsubscribe = onAuthStateChanged(auth, (currentUser) => {
      setUser(currentUser);
      setLoadingAuth(false);
      requestInFlightRef.current = false;
      if (currentUser) {
        setActiveSessionId(newRequestId());
        setIdempotencyRequestId(newRequestId());
        setActiveConversation([]);
        setCurrentPrompt("");
        void fetchHistory({ restoreLatest: true });
        void fetchQuota();
      } else {
        setHistory([]);
        setQuota(null);
        setQuotaNotice("Sign in to check service capacity.");
        setActiveConversation([]);
        setCurrentPrompt("");
        setActiveSessionId("");
        setIdempotencyRequestId("");
        setRetryAvailable(false);
        setErrorMessage(null);
        setSuccessMessage(null);
        requestAnimationFrame(() => signInRef.current?.focus());
      }
    });
    return () => unsubscribe();
  }, []);

  useEffect(() => {
    if (activeConversation.length) messageEndRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [activeConversation]);

  const handleSubmit = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!auth.currentUser || !currentPrompt.trim() || isSubmitting || requestInFlightRef.current) return;
    if (isPayloadTooLarge) {
      setErrorMessage("This draft is larger than the 4 KiB request boundary. Shorten it before sending.");
      return;
    }
    if (isPromptTooLarge) {
      setErrorMessage("This draft is over the approximate 1,000-input-token guidance. Shorten it before sending.");
      return;
    }

    requestInFlightRef.current = true;
    setIsSubmitting(true);
    setErrorMessage(null);
    setSuccessMessage(null);
    try {
      const [idToken, appCheckToken] = await Promise.all([auth.currentUser.getIdToken(), getAppCheckToken()]);
      const response = await fetch("/api/journal/interaction", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${idToken}`,
          "X-Firebase-AppCheck": appCheckToken,
        },
        body: JSON.stringify(requestBody),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(safeErrorMessage(data, "Your reflection could not be saved."));

      const prompt = currentPrompt;
      const responseText = typeof data?.assistantResponse === "string" ? data.assistantResponse : "";
      if (!responseText) throw new Error("The reflection response was incomplete. Your draft is still available to retry.");
      setActiveConversation((previous) => [
        ...previous,
        { role: "user", text: prompt },
        { role: "model", text: responseText },
      ]);
      setCurrentPrompt("");
      setIdempotencyRequestId(newRequestId());
      setRetryAvailable(false);
      setSuccessMessage("Reflection saved with its automatic summary.");
      await Promise.all([fetchHistory(), fetchQuota(idToken)]);
      requestAnimationFrame(() => composerRef.current?.focus());
    } catch (error) {
      setRetryAvailable(true);
      setErrorMessage(error instanceof Error ? error.message : "The reflection could not be saved. Your draft is still available.");
      void fetchQuota();
    } finally {
      requestInFlightRef.current = false;
      setIsSubmitting(false);
    }
  };

  const handleComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  const handleSignIn = async () => {
    setErrorMessage(null);
    try {
      await signInWithGoogle();
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Google sign-in did not complete. Please try again.");
    }
  };

  const handleSignOut = async () => {
    setErrorMessage(null);
    try {
      await signOut();
    } catch {
      setErrorMessage("Sign-out did not complete. Please try again.");
    }
  };

  const handleOpenPatterns = async () => {
    if (!auth.currentUser) return;
    try {
      const token = await auth.currentUser.getIdToken();
      setPatternsToken(token);
    } catch {
      setErrorMessage("Could not open your patterns view. Please try again.");
    }
  };

  const submitDisabled = isSubmitting || !currentPrompt.trim() || isPayloadTooLarge || isPromptTooLarge || capacityReached || modelAttemptCapacityReached || cooldownSeconds > 0;
  const submitHint = capacityReached
    ? "The server reports that the demo’s completed-reflection capacity is reached."
    : modelAttemptCapacityReached
      ? "The server reports that the demo’s model-attempt capacity is exhausted. Retry Save remains available if the server can reconcile an earlier request."
    : cooldownSeconds > 0
      ? `Please wait ${cooldownSeconds} second${cooldownSeconds === 1 ? "" : "s"} before another reflection.`
      : isPayloadTooLarge
        ? "Shorten the request to stay within 4 KiB."
        : isPromptTooLarge
          ? "Shorten the draft to stay within the approximate input-token guidance."
          : "The server validates exact limits before generation.";

  if (loadingAuth) {
    return <main className="app-shell loading-shell" aria-busy="true">
      <LoaderCircle className="spinner" aria-hidden="true" />
      <p>Preparing your journal…</p>
    </main>;
  }

  return <div className="app-shell">
    <a className="skip-link" href="#journal-composer">Skip to journal composer</a>
    <header className="topbar">
      <div className="content-width topbar-content">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true"><BookOpen size={22} /></span>
          <div>
            <h1>Reflection Thread</h1>
            <p>Personal Gemini Journal</p>
          </div>
        </div>
        {user ? <div className="account-actions">
          <span className="account-name">{user.displayName || user.email || "Signed-in journal"}</span>
          <button type="button" className="icon-button" onClick={handleOpenPatterns} aria-label="View reflection patterns">
            <Network size={18} aria-hidden="true" />
          </button>
          <button type="button" className="icon-button" onClick={handleSignOut} aria-label="Sign out">
            <LogOut size={18} aria-hidden="true" />
          </button>
        </div> : <span className="sign-in-prompt">Google sign-in is required to save reflections.</span>}
      </div>
    </header>
    {patternsToken && <PatternsPanel idToken={patternsToken} onClose={() => setPatternsToken(null)} />}

    {!user ? <main className="content-width sign-in-view">
      <section className="sign-in-card" aria-labelledby="sign-in-heading">
        <span className="hero-icon" aria-hidden="true"><ShieldCheck size={32} /></span>
        <p className="eyebrow">A focused space for your ideas</p>
        <h2 id="sign-in-heading">Pick up your reflection thread.</h2>
        <p>Sign in with Google to write, continue a conversation, and revisit the reflections saved to your account.</p>
        {errorMessage && <div className="notice error" role="alert"><AlertTriangle size={18} aria-hidden="true" />{errorMessage}</div>}
        <button ref={signInRef} type="button" className="primary-button sign-in-button" onClick={handleSignIn}>
          <span aria-hidden="true" className="google-letter">G</span> Continue with Google
        </button>
        <p className="fine-print">The app sends journal requests to its backend; it does not call Gemini from your browser.</p>
      </section>
    </main> : <main className="content-width journal-layout">
      <aside className="journal-sidebar" aria-label="Journal navigation">
        <section className="panel capacity-panel" aria-labelledby="capacity-heading">
          <div className="panel-heading"><div><p className="eyebrow">Demo guardrail</p><h2 id="capacity-heading">Service capacity</h2></div><Clock3 size={18} aria-hidden="true" /></div>
          {quota ? <>
            <div className="quota-line"><span>Completed reflections</span><strong>{quota.completedInteractionCount} / {quota.completedInteractionLimit}</strong></div>
            <div className="quota-track" aria-hidden="true"><span style={{ width: `${Math.min(100, (quota.completedInteractionCount / Math.max(1, quota.completedInteractionLimit)) * 100)}%` }} /></div>
            <div className="quota-line secondary-meter"><span>Model attempts issued</span><strong>{quota.monthlyAttemptCount} / {quota.monthlyAttemptLimit}</strong></div>
            <p className="meter-note">Includes permitted fallback and summary attempts; this meter is shown for cost transparency.</p>
            <p className={cooldownSeconds > 0 ? "status-copy warning-copy" : "status-copy"}>{cooldownSeconds > 0 ? `Personal cooldown: ${cooldownSeconds}s remaining.` : "Ready when your draft is within the limits."}</p>
          </> : <p className="status-copy">{quotaNotice}</p>}
          <p className="panel-note">This is service-wide server status, not a count inferred from this browser.</p>
        </section>

        <section className="panel history-panel" aria-labelledby="history-heading">
          <div className="panel-heading"><div><p className="eyebrow">Your saved work</p><h2 id="history-heading">Journal sessions</h2></div><History size={18} aria-hidden="true" /></div>
          {loadingHistory ? <p className="empty-state"><LoaderCircle className="mini-spinner" aria-hidden="true" /> Loading saved reflections…</p>
            : sessionPreviews.length === 0 ? <div className="empty-state"><PenLine size={22} aria-hidden="true" /><p>Your first saved reflection will appear here.</p></div>
              : <ul className="session-list">
                {sessionPreviews.map((entry) => <li key={entry.sessionId}>
                  <button type="button" className={entry.sessionId === activeSessionId ? "session-button active" : "session-button"} onClick={() => restoreSession(entry.sessionId)}>
                    <span>{entry.automaticSessionSummary || "Untitled reflection"}</span>
                    <small>{formatTime(entry.timestamps?.epochMs)}</small>
                  </button>
                </li>)}
              </ul>}
        </section>
      </aside>

      <section className="journal-main" aria-label="Journal conversation">
        <div className="conversation-card">
          <header className="conversation-header">
            <div><p className="eyebrow">{activeConversation.length ? "Continuing session" : "New session"}</p><h2>{activeConversation.length ? "Your reflection thread" : "Start with what is on your mind"}</h2></div>
            <button type="button" className="secondary-button" onClick={startNewSession}><Plus size={17} aria-hidden="true" /> New session</button>
          </header>

          {errorMessage && <div className="notice error journal-notice" role="alert"><AlertTriangle size={18} aria-hidden="true" /><div><p>{errorMessage}</p>{retryAvailable && <button type="button" className="retry-button" onClick={() => void handleSubmit()}>Retry Save</button>}</div></div>}
          {successMessage && <div className="notice success journal-notice" role="status" aria-live="polite"><CheckCircle2 size={18} aria-hidden="true" /><div>{successMessage}</div><button type="button" className="dismiss-button" onClick={() => setSuccessMessage(null)} aria-label="Dismiss success message">Dismiss</button></div>}

          <div className="conversation" aria-live="polite" aria-label="Conversation messages">
            {activeConversation.length === 0 ? <div className="conversation-empty"><span className="hero-icon small" aria-hidden="true"><Sparkles size={22} /></span><h3>Make room for a thought.</h3><p>Journal freely, untangle an idea, or ask for a focused reflection.</p></div>
              : activeConversation.map((message, index) => <article className={`message ${message.role}`} key={`${message.role}-${index}`}>
                <p className="message-label">{message.role === "user" ? "You" : "Gemini reflection"}</p>
                <p className="message-text">{message.text}</p>
              </article>)}
            <div ref={messageEndRef} />
          </div>

          <form className="composer" onSubmit={handleSubmit}>
            <label htmlFor="journal-composer">Write a reflection</label>
            <textarea
              id="journal-composer"
              ref={composerRef}
              value={currentPrompt}
              onChange={(event) => { setCurrentPrompt(event.target.value); setErrorMessage(null); }}
              onKeyDown={handleComposerKeyDown}
              placeholder="What would you like to reflect on?"
              rows={5}
              disabled={isSubmitting}
              aria-describedby="composer-guidance composer-status"
              aria-keyshortcuts="Control+Enter Meta+Enter"
            />
            <div className="composer-footer">
              <div id="composer-guidance" className="composer-metrics">
                <span className={isPayloadTooLarge ? "metric-limit" : ""}>{(requestBytes / 1024).toFixed(2)} KiB / 4 KiB request</span>
                <span className={isPromptTooLarge ? "metric-limit" : ""}>~{approximateTokens} / ~{APPROX_INPUT_TOKEN_LIMIT} input tokens</span>
              </div>
              <button type="submit" className="primary-button" disabled={submitDisabled} aria-describedby="composer-status">
                {isSubmitting ? <><LoaderCircle className="mini-spinner" aria-hidden="true" /> Saving reflection…</> : <><Send size={17} aria-hidden="true" /> Reflect with Gemini</>}
              </button>
            </div>
            <p id="composer-status" className="status-copy" aria-live="polite">{submitHint} Use Ctrl/Cmd + Enter to send.</p>
          </form>
        </div>
      </section>
    </main>}

    <footer className="footer">Demo deployment profile: asia-south1 · limited-capacity single-user showcase</footer>
  </div>;
}
