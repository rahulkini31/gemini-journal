/**
 * Shared, in-browser fake auth state for Playwright E2E — the runtime
 * counterpart to how tests/frontend.test.tsx (Vitest) already mocks
 * ../src/lib/firebase and firebase/auth at the module level for component
 * tests. Playwright drives a real bundled app in a real browser, so it
 * can't use vi.mock(); vite.config.ts's VITE_E2E-gated alias branch swaps
 * in ./firebase.ts and ./firebase-auth.ts (which both import this module)
 * in place of the real ones instead — see that file's comment for why this
 * never reaches the production bundle.
 *
 * The bypass id token this issues only works because the E2E dev server is
 * also launched with TEST_BYPASS_AUTH=true (playwright.config.ts), which
 * server.ts already supports for exactly this purpose (isTestBypassEnabled,
 * checked against the literal string "test-id-token") — this reuses that
 * existing, already-audited mechanism rather than inventing a new one.
 */

export interface FakeUser {
  uid: string;
  displayName: string | null;
  email: string | null;
}

export const BYPASS_ID_TOKEN = "test-id-token";

type Listener = (user: FakeUser | null) => void;

let currentUser: FakeUser | null = null;
const listeners = new Set<Listener>();

export function getCurrentUser(): FakeUser | null {
  return currentUser;
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  listener(currentUser);
  return () => listeners.delete(listener);
}

export function setUser(user: FakeUser | null): void {
  currentUser = user;
  listeners.forEach((listener) => listener(currentUser));
}

declare global {
  interface Window {
    __e2e__: {
      signIn(user: FakeUser): void;
      signOut(): void;
    };
  }
}

if (typeof window !== "undefined") {
  window.__e2e__ = {
    signIn: (user) => setUser(user),
    signOut: () => setUser(null),
  };
}
