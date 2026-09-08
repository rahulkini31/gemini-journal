/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Base URL for the experimental ADK agent prototype's graph endpoints
   * (adk-agent/app/main.py) — see src/lib/graphApi.ts. That service is
   * separate from this app's own backend (server.ts).
   */
  readonly VITE_ADK_AGENT_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
