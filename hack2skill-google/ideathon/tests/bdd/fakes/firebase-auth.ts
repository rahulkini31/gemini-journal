/**
 * E2E-only fake for the "firebase/auth" package specifier, aliased in by
 * vite.config.ts only when VITE_E2E=true. Exports just what src/App.tsx
 * actually imports from the real package: `onAuthStateChanged` and the
 * `User` type. See tests/bdd/fakes/e2eAuthState.ts for the shared state
 * both this file and firebase.ts read/write.
 */
import { FakeUser, subscribe } from "./e2eAuthState";

export type User = FakeUser;

export function onAuthStateChanged(_auth: unknown, listener: (user: User | null) => void): () => void {
  return subscribe(listener);
}
