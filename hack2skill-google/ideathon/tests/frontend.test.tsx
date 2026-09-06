// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App";

const firebaseMocks = vi.hoisted(() => ({
  auth: { currentUser: null as any },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getAppCheckToken: vi.fn(),
}));

let authStateListener: ((user: any) => void) | undefined;

vi.mock("../src/lib/firebase", () => firebaseMocks);
vi.mock("firebase/auth", () => ({
  onAuthStateChanged: vi.fn((_auth: unknown, listener: (user: any) => void) => {
    authStateListener = listener;
    listener(firebaseMocks.auth.currentUser);
    return vi.fn();
  }),
}));

const quotaOpen = {
  completedInteractionCount: 0,
  completedInteractionLimit: 10,
  monthlyAttemptCount: 0,
  monthlyAttemptLimit: 80,
  cooldownSeconds: 0,
};

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  }));
}

function mockApi({ quota = quotaOpen, history = [], interaction }: {
  quota?: typeof quotaOpen;
  history?: unknown[];
  interaction?: () => Promise<Response>;
} = {}) {
  const fetchMock = vi.fn((input: string) => {
    if (input.includes("/api/journal/quota")) return jsonResponse(quota);
    if (input.includes("/api/journal/history")) return jsonResponse(history);
    if (input.includes("/api/journal/interaction")) {
      return interaction ? interaction() : jsonResponse({ assistantResponse: "A thoughtful response." });
    }
    return jsonResponse({ error: "Unexpected request" }, 500);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function authenticate() {
  const user = { displayName: "Test journal user", email: "user@example.test", getIdToken: vi.fn().mockResolvedValue("id-token") };
  firebaseMocks.auth.currentUser = user;
  await act(async () => authStateListener?.(user));
  return user;
}

describe("Reflection Thread frontend safeguards", () => {
  beforeEach(() => {
    firebaseMocks.auth.currentUser = null;
    firebaseMocks.signInWithGoogle.mockReset();
    firebaseMocks.signOut.mockReset();
    firebaseMocks.getAppCheckToken.mockReset().mockResolvedValue("app-check-token");
    authStateListener = undefined;
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 0;
    });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("shows only a Google sign-in action when unauthenticated", () => {
    mockApi();
    render(<App />);

    expect(screen.getByRole("button", { name: /continue with google/i })).not.toBeNull();
    expect(screen.queryByLabelText(/write a reflection/i)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /continue with google/i }));
    expect(firebaseMocks.signInWithGoogle).toHaveBeenCalledOnce();
  });

  it("renders backend capacity and disables a new submission at the completed-reflection ceiling", async () => {
    mockApi({ quota: { ...quotaOpen, completedInteractionCount: 10 } });
    render(<App />);
    await authenticate();

    expect(await screen.findByText("10 / 10")).not.toBeNull();
    fireEvent.change(screen.getByLabelText(/write a reflection/i), { target: { value: "A complete reflection." } });
    const send = screen.getByRole("button", { name: /reflect with gemini/i });
    expect((send as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/completed-reflection capacity is reached/i)).not.toBeNull();
  });

  it("keeps a failed draft and exposes a keyboard-operable Retry Save action", async () => {
    mockApi({ interaction: () => jsonResponse({ error: "The reflection could not be saved." }, 503) });
    render(<App />);
    await authenticate();

    const composer = await screen.findByLabelText(/write a reflection/i);
    fireEvent.change(composer, { target: { value: "Keep this draft after a failed save." } });
    fireEvent.click(screen.getByRole("button", { name: /reflect with gemini/i }));

    expect((await screen.findByRole("alert")).textContent).toMatch(/could not be saved/i);
    expect((composer as HTMLTextAreaElement).value).toBe("Keep this draft after a failed save.");
    expect((screen.getByRole("button", { name: /retry save/i }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("renders saved user and model text as inert text rather than HTML", async () => {
    const attackText = "<img src=x onerror=alert('unsafe')>";
    mockApi({ history: [{
      id: "entry-1",
      userPrompt: attackText,
      assistantResponse: attackText,
      automaticSessionSummary: "Inert text check",
      sessionId: "session-1",
      turnOrder: 1,
      timestamps: { epochMs: 1 },
      status: "completed",
      idempotencyRequestId: "request-1",
    }] });
    const { container } = render(<App />);
    await authenticate();

    expect(await screen.findAllByText(attackText)).toHaveLength(2);
    expect(container.querySelector("img")).toBeNull();
  });
});
