/**
 * E2E-only fake for src/lib/firebase.ts, aliased in by vite.config.ts only
 * when VITE_E2E=true — see that file's comment and
 * tests/bdd/fakes/e2eAuthState.ts's module docstring for the full picture.
 * Exports exactly what src/App.tsx imports from the real module.
 */
import { BYPASS_ID_TOKEN, getCurrentUser, setUser } from "./e2eAuthState";

export const auth = {
  get currentUser() {
    const user = getCurrentUser();
    if (!user) return null;
    return { ...user, getIdToken: async () => BYPASS_ID_TOKEN };
  },
};

export const googleProvider = {};

export async function signInWithGoogle(): Promise<never> {
  throw new Error(
    "signInWithGoogle() is not available in the E2E fake — drive sign-in via window.__e2e__.signIn() from a step definition instead.",
  );
}

export async function signOut(): Promise<void> {
  setUser(null);
}

export async function getAppCheckToken(): Promise<string> {
  return "test-app-check-token";
}
