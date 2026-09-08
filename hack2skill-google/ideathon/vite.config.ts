import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
// vitest/config re-exports vite's defineConfig with the `test` field added
// to the type — a drop-in replacement for a single shared config file, used
// here only to exclude Playwright-BDD's generated output (.features-gen/)
// and its own source (tests/bdd/) from Vitest's test discovery; it isn't a
// new build tool in the mix.
import {defineConfig} from 'vitest/config';

// Playwright BDD (tests/bdd/) needs the app in a signed-in state without
// real Google OAuth popup sign-in, which isn't automatable headlessly.
// tests/frontend.test.tsx (Vitest) already solves the same problem for
// component tests via vi.mock() at the module level; Playwright drives a
// real bundled app in a real browser, so it can't use that — this mirrors
// the identical two-module fake shape at the bundler level instead, gated
// strictly behind VITE_E2E=true (set only by playwright.config.ts's
// webServer.env). `npm run build`/`vite build` never sets this var, so the
// production bundle never resolves these aliases and carries zero
// footprint from them — not a runtime branch shipped to users, a
// build-time module swap that only exists for the dedicated E2E dev server.
const e2eAliases = process.env.VITE_E2E === 'true' ? {
  './lib/firebase': path.resolve(__dirname, 'tests/bdd/fakes/firebase.ts'),
  'firebase/auth': path.resolve(__dirname, 'tests/bdd/fakes/firebase-auth.ts'),
} : {};

export default defineConfig(() => ({
  plugins: [react(), tailwindcss()],
  resolve: {alias: {'@': path.resolve(__dirname, '.'), ...e2eAliases}},
  build: {
    outDir: 'dist/client',
  },
  server: {
    hmr: process.env.DISABLE_HMR !== 'true',
    watch: process.env.DISABLE_HMR === 'true' ? null : {},
  },
  test: {
    exclude: ['**/node_modules/**', '**/dist/**', '.features-gen/**', 'tests/bdd/**'],
  },
}));
