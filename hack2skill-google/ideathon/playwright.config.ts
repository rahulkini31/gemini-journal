import { defineConfig, devices } from '@playwright/test';
import { defineBddConfig } from 'playwright-bdd';

const E2E_PORT = 5179;

const testDir = defineBddConfig({
  features: 'tests/bdd/features/*.feature',
  steps: 'tests/bdd/features/steps/*.ts',
});

export default defineConfig({
  testDir,
  fullyParallel: true,
  reporter: 'list',
  use: {
    baseURL: `http://localhost:${E2E_PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: devices['Desktop Chrome'] }],
  webServer: {
    // The unified server (server.ts) serves the app the same way in dev as
    // in production, so this is the real app, not a stand-in. TEST_BYPASS_AUTH
    // lets the backend accept the fake bypass token tests/bdd/fakes/
    // e2eAuthState.ts issues; VITE_E2E swaps in the browser-side auth fake
    // (see vite.config.ts). No real Firebase/Featherless/Gemini credentials
    // anywhere in this — journal endpoints degrade to their existing,
    // already-tested "temporarily unavailable" state without a real
    // Firestore connection, which the scenarios here don't depend on.
    command: 'npm run dev',
    url: `http://localhost:${E2E_PORT}`,
    reuseExistingServer: !process.env.CI,
    env: {
      VITE_E2E: 'true',
      TEST_BYPASS_AUTH: 'true',
      NODE_ENV: 'development',
      PORT: String(E2E_PORT),
    },
    timeout: 30_000,
  },
});
