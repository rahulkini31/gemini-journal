# Execution log

## Phase 0 — Compliance lock

**State:** Initialized on 2026-08-25 (Asia/Kolkata)

### Scope

- Established the compliance record in `docs/compliance-matrix.md` from the local
  challenge brief and official Google codelab.
- No application code, package installation, cloud resource, credential, or
  deployment has been created or changed by this documentation step.

### Required pre-code gate

> **Google AI Studio must be configured with the complete Production Directives
> before any application architecture or code is generated or written.** For each
> feature, AI Studio must first produce a Threat Summary Table covering the five
> threat zones and countermeasures.

Required Phase 1 evidence:

1. AI Studio New App / Build Mode created.
2. Custom Instructions contain the codelab Production Directives (threat modeling,
   OWASP coding, Firebase/Firestore isolation, Secret Manager, security review,
   functional walkthroughs, fallback/persistence standards, and README directives).
3. AI Studio returns its Threat Summary Table for the baseline prompt before it
   outputs architecture or code.
4. A sanitized screenshot/transcript is retained in the project evidence set.

**Gate owner:** Independent security auditor.  
**Gate status:** Passed on 2026-08-26. The approved five-zone table is retained in
`docs/ai-studio-threat-model.md`; baseline application-code generation is authorized.  
**Next allowed action:** Baseline implementation was authorized after the account
owner accepted the Firebase setup terms.

## Phase 2 — Secure baseline implementation and deployment

**State:** Implemented, tested, and deployed on 2026-08-28.

- Built the unified React and Node.js application with Google-only Firebase
  Authentication, Firebase App Check, Cloud Firestore, Gemini Developer API,
  Secret Manager, Cloud Build, Artifact Registry, and Cloud Run.
- Provisioned a dedicated least-privilege runtime identity and one server-only
  Gemini secret version.
- Deployed owner-bound default Firestore rules and confirmed the only database
  is the free-tier `(default)` database in `asia-south1`.
- Added durable, distinct limits for 10 completed interactions and 80 model
  attempts per UTC month, plus one admitted interaction per verified UID per
  minute.
- Passed TypeScript, 10 automated tests, production build/start, static secret
  and unsafe-rendering scans, live unauthenticated boundary tests, and Cloud Run
  configuration/IAM inspection.
- Published Cloud Run revision `ai-reflection-00003-w4x` at
  `https://ai-reflection-tjmxdke56q-el.a.run.app`, with the required campaign
  label, zero minimum instances, one service-level maximum instance, and 100%
  traffic.
- Removed two exact superseded revisions and their untagged images after the
  final revision became healthy. No user or Firestore data was removed.
- Created the project-filtered INR 500 billing budget with 50%, 80%, 90%, and
  100% alerts. This is an alert, not a hard spending stop.

Detailed evidence is in `docs/deployment-evidence.md`.

## Phase 3 — Memory Threads enhancement gate

**State:** Blocked at Google AI Studio threat-model generation on 2026-08-28.

The enhancement security addendum was saved to the existing AI Studio Custom
Instructions. AI Studio failed the table-only request repeatedly with an
internal error under both Gemini 3.5 Flash and Gemini 3.7 Flash. A response that
ran a build and returned project status instead of the required threat table was
explicitly rejected. No enhancement code, database index, embedding, or cloud
resource was created. Continue only after AI Studio returns a complete table and
an independent human review accepts it.

## Future phase checklist

| Phase | Entry condition | Exit condition | State |
| --- | --- | --- | --- |
| 1 — AI Studio constitution | Phase 0 complete | Pre-code gate evidence is recorded | Complete — independent audit passed 2026-08-26 |
| 2 — Baseline build | Phase 1 complete; Firebase terms accepted | Core Auth, Gemini, Firestore, and Secret Manager requirements implemented | Complete — automated and cloud baseline checks passed |
| 3 — Original enhancement | Baseline design accepted in AI Studio | Memory Threads threat table and feature tests complete | Blocked — AI Studio internal error; gate remains closed |
| 4 — Verification & publish | Security/stability gates pass | Cloud Run live URL tested and mandatory label verified | Baseline live; authenticated owner walkthrough and enhancement pending |
| 5 — Share & submit | Source/docs ready | Repository, social/blog, and form deliverables complete | Blocked by Phase 4 |
