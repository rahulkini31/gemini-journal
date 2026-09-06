# Baseline deployment evidence

Verified 2026-08-28 in Google Cloud project `genai-academy-temp`.

## Live service

| Item | Verified value |
| --- | --- |
| Cloud Run service | `ai-reflection` |
| URL | `https://ai-reflection-tjmxdke56q-el.a.run.app` |
| Region | `asia-south1` |
| Ready revision | `ai-reflection-00003-w4x` |
| Traffic | 100% to the ready revision |
| Campaign label | `dev-tutorial=cloud-run-ai-challenge` |
| Runtime identity | `ai-reflection-runtime@genai-academy-temp.iam.gserviceaccount.com` |
| CPU / memory | 1 vCPU / 512 MiB |
| Concurrency / timeout | 4 / 15 seconds |
| Service scaling | minimum 0, maximum 1 |

The service-level maximum is recorded as
`run.googleapis.com/maxScale: 1`. Cloud Run also displays its revision-level
default of 20, but the service-level limit is the effective cap across all
traffic-serving revisions.

Two superseded, untagged revisions and their two exact container digests were
deleted after the final revision became healthy. They contained no application
data; the removed rollback revisions/images are not recoverable. The final
`latest` image and revision are the only application artifacts retained.

## Live smoke results

| Check | Result |
| --- | --- |
| Public `/` | HTTP 200, HTML |
| Public `/api/config` | HTTP 200, Firebase public config only |
| Unauthenticated `/api/journal/history` | HTTP 401 |
| Unauthenticated `/api/journal/quota` | HTTP 401 |
| Unauthenticated interaction POST | HTTP 401 before model/persistence |
| Browser bundle compared to Secret Manager value | Runtime Gemini secret absent |
| Backend bundle/source map paths | SPA HTML only; backend artifacts are not statically served |
| Security headers | no-referrer, nosniff, frame deny, camera/microphone/geolocation disabled |
| Visible landing page | Accessible Google-only sign-in view rendered successfully |

The final browser-authenticated Gemini turn still requires the account owner to
complete the Google account chooser in the visible app tab. No model quota was
spent by these unauthenticated smoke tests.

## Firebase, Firestore, and secret controls

- The only Firestore database is `(default)`, Standard Native mode,
  `asia-south1`, with `freeTier: true`; PITR is disabled.
- Default Firestore rules release
  `projects/genai-academy-temp/releases/cloud.firestore` points to ruleset
  `362ce49a-ad64-4fea-927d-8be21ed12ec7`.
- Direct client rules allow only the verified owner at
  `users/{userId}/interactions/{interactionId}` and deny all other paths.
- The dedicated runtime identity has `roles/datastore.user` constrained to the
  default database, `roles/firebaseappcheck.tokenVerifier`, and
  `roles/firebaseauth.viewer`. It has no project Owner or Editor role.
- Secret Accessor is granted on the one named `gemini-api-key` secret, not at
  project scope. Cloud Run injects version 1 without exposing its value.
- The reCAPTCHA Enterprise score key is restricted to the exact Cloud Run
  domain. Firebase App Check registration uses a one-hour token TTL.

## Automated and local release checks

- TypeScript check: pass.
- Vitest: 10/10 pass across backend and frontend suites.
- Production Vite/esbuild build: pass.
- Production `npm start`: local `/` returned 200 and protected history returned
  401 without credentials.
- Static scan: no private key, OAuth client secret, refresh token, Gemini
  credential assignment, open Firestore allow rule, or unsafe HTML-rendering
  sink found outside dependencies/build output.

## Cost controls

- The project-filtered Cloud Billing budget `Secure Journal ₹500 ceiling` is
  active at INR 500 with 50%, 80%, 90%, and 100% current-spend alerts.
- Cloud Billing budgets are alerts, not a hard stop. Application quotas and the
  service-level one-instance maximum are the operational bounds.
- Only the final container image is retained. Cloud Run minimum instances is
  zero. Expected upfront deployment cash cost is ₹0 within the listed free
  allowances; see `docs/bom.md` for caveats.

## Enhancement gate status

The Memory Threads Custom Instructions addendum was saved in Google AI Studio.
Google AI Studio then failed the table-only request with an internal error in
multiple attempts using Gemini 3.5 Flash, including a fresh chat, and one fresh
attempt using Gemini 3.7 Flash. One retry also violated the gate by returning a project-status/build
summary instead of the required table. That response was rejected. No Memory
Threads implementation was performed, so the challenge's original-feature
requirement remains blocked at its mandatory AI Studio threat-model gate.
