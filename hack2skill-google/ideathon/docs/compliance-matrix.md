# Cloud Run AI Challenge compliance matrix

This matrix is the build and submission control record for the **Secure Personal
Gemini Journal** challenge. The challenge brief (`challenge.md`) and the official
[Google codelab](https://codelabs.developers.google.com/codelabs/cloud-run/cloud-run-ai-challenge?hl=en)
are the authoritative sources. Items marked **gate** must be satisfied before the
dependent work begins.

## Challenge requirements and evidence

| ID | Mandatory rule / directive | Planned implementation evidence | Verification / test | Submission evidence | Status |
| --- | --- | --- | --- | --- | --- |
| C-01 | **Gate:** configure Google AI Studio Custom Instructions before it writes any application code. | Captured Custom Instructions and AI Studio project metadata; `docs/ai-studio-workflow.md` records prompt/response sequence. | Inspect timestamps/order and confirm AI Studio produced a Threat Summary Table before architecture/code prompt. | AI Studio screenshots and sanitized directive transcript. | Pending — blocked by user UI action |
| C-02 | Custom Instructions must require threat modeling across input, planning/reasoning, tool execution, memory/state, and inter-system communication. | Full Production Directives in AI Studio; per-feature threat tables retained in docs. | Review each feature request has a threat table with risks and mitigations first. | Screenshots/transcript. | Pending |
| C-03 | Follow OWASP web and LLM secure-coding practices: schema validation, injection defense, authorization at every boundary, and safe output rendering. | Server schemas, request-size limits, defensive prompt/context handling, encoded rendered output, security-review findings. | Unit/integration tests for malformed input, injection-like content, unauthorized calls, and unsafe output. | Security section in README and test report. | Pending |
| C-04 | Firebase Authentication; use Google Sign-In and do not implement custom email/password handling. | Firebase Auth Google provider; client obtains ID token; no password UI or storage. | Sign-in/sign-out and unauthenticated-route tests. | Live walkthrough/screenshots. | Pending |
| C-05 | Backend verifies Firebase JWTs with Firebase Admin SDK; UID comes only from verified token. | Auth middleware verifies bearer token and exposes verified UID; backend ignores body UID. | Missing, expired, invalid, forged-token tests; attempted body-UID substitution test. | Source and security test results. | Pending |
| C-06 | Multi-turn Gemini interaction for journaling/brainstorming. | Server-side conversation endpoint with session history and model context. | Three or more turns maintain expected context; UI interaction tests. | Live app/video. | Pending |
| C-07 | Use Gemini 3.6 Flash as the primary AI processing model. | Shared server Gemini helper defines `gemini-3.6-flash` first. | Automated helper test asserts primary selection. | Source/README. | Pending |
| C-08 | All Gemini content generation uses a reusable fallback helper in this order: `gemini-3.6-flash`, `gemini-3.1-flash-lite`, `gemini-flash-latest`, `gemini-3.7-flash`. | `generateContentWithFallback` shared by chat and summary generation. | Simulate 503, 429, 404, and 500; assert sequential fallback and a clear terminal error. | Source, tests, README. | Pending |
| C-09 | Firestore stores each user's logs, prompts, responses, and automatic summaries; zero cross-user leakage. | Owner-scoped `users/{uid}/interactions/{interactionId}` data model; server queries scoped by verified UID. | User A cannot list/read/write/delete User B documents; logout/login persistence test. | `firestore.rules`, source, test report. | Pending |
| C-10 | Firestore rules must be owner-bound and must never use `allow read, write: if true;`. | Versioned `firestore.rules` applies `request.auth.uid == userId`; no broad fallback match. | Firebase rules emulator tests plus static deny-open-rule check. | Repository configuration. | Pending |
| C-11 | Persist generated session summaries automatically. | Summary generator and interaction/session persistence record. | Submission creates a summary; reload displays history without duplicate records. | Live walkthrough and source. | Pending |
| C-12 | API keys/operational credentials must be in Google Cloud Secret Manager or secure server environment injection, never hardcoded or client-side. | `GEMINI_API_KEY` Secret Manager secret injected only to Cloud Run runtime; dedicated runtime identity has Secret Accessor. | Secret scan for repository/build output; client network test confirms Gemini is called only through backend; IAM/deployment inspection. | README setup steps and Cloud Console evidence (no secret values). | Pending |
| C-13 | Do not commit service-account JSON, tokens, or credentials. | `.gitignore`, runtime identity, no local credential artifacts. | Secret and tracked-file scan. | Repository. | Pending |
| C-14 | Body parser must be mounted before routes; requests must be null-safe and return clean 400 errors instead of crashing. | Unified server entry point initializes JSON parsing then routes; schema guards all input sources. | Empty body, invalid JSON, missing fields, bad content-type tests. | Source/tests. | Pending |
| C-15 | Strip `undefined` before Firestore writes. | Database payload sanitizer used for every create/update path. | Unit test rejects/strips nested undefined values and writes succeed. | Source/tests. | Pending |
| C-16 | Inputs and generated output must be persistently handled; failures must not silently lose input or clear it. | Idempotent interaction writes; UI keeps draft after failed save and offers accessible **Retry Save**. | Forced generation/write failure, retry, and duplicate-request tests. | Walkthrough/test report. | Pending |
| C-17 | Every visible user process/interaction has a corresponding test case and each interactive control works. | `docs/functional-walkthrough.md` and automated UI/API test suite. | Execute manual checklist and automated suite. | Test report. | Pending |
| C-18 | Project `dev`, `build`, and `start` scripts must launch/build the unified full-stack application, not a frontend-only bundler after backend is added. | Package scripts and production server entry point. | Run all three commands and health/API smoke tests. | README and CI/test output. | Pending |
| C-19 | AI Studio security reviewer must identify hardcoded secrets, unsafe defaults, data flows, and access checks with severity-ranked remediation. | Recorded AI Studio security-review prompt/output and resolved findings. | No unresolved Critical/High finding before publish. | Review artifact in docs (sanitized). | Pending |
| C-20 | README must cover prerequisites/APIs, Firebase, Firestore rules, Secret Manager and IAM, Cloud Run deployment, required campaign label, and testing. | Generated and reviewed `README.md`. | Fresh-environment documentation review; commands checked for accuracy and no embedded secret values. | Repository README. | Pending |
| C-21 | Deploy a functional app to Cloud Run through AI Studio Publish, test the live app, and republish updates as needed. | AI Studio publish record, Cloud Run service/revision, production smoke-test log. | Open public endpoint; sign in; submit and reload an interaction. | Live URL or walkthrough media. | Pending |
| C-22 | Cloud Run service must have label `dev-tutorial=cloud-run-ai-challenge`. | Cloud Run service label. | `gcloud run services describe` or Console inspection verifies exact key/value. | Screenshot/CLI evidence and README. | Pending |
| C-23 | Add at least one original enhancement, designed and built using Google AI Studio, beyond the base specification. | **Memory Threads**: user-scoped semantic recall over opted-in past summaries; feature-specific AI Studio directive and threat table. | Feature test: only verified user's selected memory can join context; delete removes derived vector; no raw embedding shown. | AI Studio prompt/threat table, app walkthrough, README. | Pending |
| C-24 | Before adding any new service/integration, expand AI Studio Custom Instructions to secure it. | Memory Threads directive added before feature implementation; future integrations get equivalent record. | Review AI Studio change appears before associated design/code prompt. | AI Studio screenshot/transcript. | Pending |
| C-25 | Share frontend/backend code, configuration, Firestore rules, and deployment README in a public or shared GitHub/GitLab repository. | Repository contains application source, `firestore.rules`, README, and evidence docs. | Fresh clone/build review and link-access check. | Repository link. | Pending |
| C-26 | Submit form with email, Cloud Run project/service name, social/blog link, and repository link. | Final submission checklist. | Human verifies all fields and URLs before submission. | Submission confirmation. | Pending — requires user authorization |
| C-27 | Publish a social/blog showcase with `#AccelerateAIwithCloudRun`, highlighting unique features and Google AI Studio use. | Draft post and media/links. | Human reviews exact hashtag, links, claims, and no sensitive data. | Public post/blog link. | Pending — requires user authorization |

## Non-negotiable delivery gates

1. **AI Studio gate:** C-01 through C-03 must have evidence before any application-code generation or coding begins.
2. **Firebase gate:** Firebase setup terms must be accepted by the account owner before Auth/Firestore implementation can be completed.
3. **Security gate:** C-04 through C-20 must pass before production publish.
4. **Deployment gate:** C-21 and C-22 must be verified before final delivery.
5. **Submission gate:** C-23 through C-27 must be complete before form submission. Social publishing and final-form submission remain user-authorized actions.

## Evaluation alignment

| Pillar | Evidence to prioritize |
| --- | --- |
| Authenticity | Memory Threads design rationale, AI Studio feature prompts, and a visibly distinct user experience. |
| Usability | Google SSO, accessible errors/retry, clear history and memory consent controls, complete interaction tests. |
| Stability | Model fallback tests, idempotency, persistence-failure recovery, unified scripts, production smoke test. |
| Security | Threat tables, JWT verification, owner-bound Firestore rules, Secret Manager binding, secret scan, review remediation. |
