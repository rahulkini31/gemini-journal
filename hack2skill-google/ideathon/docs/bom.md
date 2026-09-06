# Bill of materials and cost guardrails

This estimate is for the Personal Gemini Journal challenge in Google Cloud project `genai-academy-temp`. It is a planning artifact only: no resources are created or changed by this document.

Pricing was checked on 2026-08-25. Prices are USD list prices unless stated otherwise.

## Personal-hackathon profile (primary)

This one-person showcase is capped at **₹500/month** (about **US$5.88** at ₹85/US$), before tax. It replaces the earlier US$25 scale-up profile as the recommended configuration.

Set a ₹500 Cloud Billing budget (or the closest amount in the account currency) and email alerts at:

| Threshold | USD | Planning INR |
| --- | ---: | ---: |
| 20% | $1.18 | ₹100 |
| 50% | $2.94 | ₹250 |
| 80% | $4.71 | ₹400 |
| 100% | $5.88 | ₹500 |

Cloud Billing alerts are notifications, not a spending cut-off. Do not automate billing disablement without explicit approval because it takes the app offline. See [Cloud Billing budgets and alerts](https://cloud.google.com/billing/docs/how-to/budgets).

Use a **Free Tier Gemini API project/key** in AI Studio and do not click *Set up billing*, prepay, or configure auto-reload for Gemini. The optional paid tier requires a minimum **US$10 (~₹850) prepayment**, already above this ceiling. Existing Cloud billing can remain enabled for Cloud Run's free quotas; it does not require the Gemini paid tier. See [Gemini API billing](https://ai.google.dev/gemini-api/docs/billing).

### Costs for one deploy and one showcase user

**Unavoidable cash to deploy once: ₹0 / US$0.** This assumes one default-pool build below its included 2,500 monthly Cloud Build minutes; request-based Cloud Run with `min-instances=0`; the default Firestore database within free quota; and Gemini Standard Free Tier requests. Artifact Registry storage bills hourly, but it does not require an upfront payment.

**Fixed monthly project cost after deployment:** the existing repository is about 0.86 GiB. Assume exactly one compact new 0.25 GiB image and no other Artifact Registry use on the billing account:

```text
0.86 GiB existing + 0.25 GiB new = 1.11 GiB stored
(1.11 - 0.50 GiB free) × ~$0.10/GiB-month = $0.061/month
$0.061 × ₹85 = ₹5.19/month
```

Thus the fixed estimate is **₹5.20/month**. If the shared Artifact Registry free allowance is already consumed by another project, budget the full 1.11 GiB: **$0.111 / ₹9.44 per month**. There is no Cloud Run fixed charge with `min-instances=0`.

**Variable cost for exactly one user: ₹0.** Assume one user makes 10 interactions in a showcase month, each capped at 1,000 chat input tokens, 300 chat output tokens, 300 summary input tokens, 100 summary output tokens, and one 300-token text embedding. That is 13,000 model input tokens, 4,000 model output tokens, and 3,000 embedding tokens. Gemini 3.6 Flash Standard and Gemini Embedding 2 text are free on Free Tier; this small request/read/write volume is inside all listed Cloud Run, Firestore, Secret Manager, Authentication, and Logging free quotas. The conservative total is therefore **₹5.20/month** (or **₹9.44** if Artifact Registry's shared allowance is exhausted), plus applicable tax or egress.

### Free-tier model availability, privacy, and runtime limits

On 2026-08-25, Gemini 3.6 Flash Standard, 3.1 Flash-Lite Standard, Gemini 3.5 Flash Standard (`gemini-flash-latest` currently resolves there), Gemini 3.7 Flash Standard, and Gemini Embedding 2 text all list a Free Tier price of free. The risk is availability: Google limits Free Tier access to certain models, and actual RPM/TPM/RPD vary by project and are not guaranteed. Verify the selected key's limits in AI Studio and test all required model IDs before deployment. If a model is unavailable or quota-limited, show a retry message; **do not upgrade to paid Gemini to complete the demo**.

Free Tier content can be used to improve Google products. This profile is therefore restricted to synthetic, non-sensitive demo journal entries; it is not appropriate for real private journals without a separately approved privacy/cost decision.

- `min-instances=0`, `max-instances=1`, 1 vCPU/512 MiB, concurrency 4, and a 15-second timeout.
- Require verified Firebase Authentication and Firebase App Check before every model call.
- 10 completed interaction units/month globally and one/minute. Use only the codelab-mandated fallback ladder for 503/429/404/500; every outbound model attempt consumes rate-limit capacity and there is no additional generic retry loop.
- Enforce the token caps above and a 4 KiB request-body cap. Keep App Check's one-hour token TTL, and disable grounding, media/files, caching, code execution, VPC connector, GPU, PITR/backups/TTL, log exports, and Cloud Trace.
- Retain only the new deploy image after the showcase; do not create repeated revisions.
- After recording the live walkthrough, remove public Cloud Run invocation unless live judging requires the URL. Requests rejected by Cloud Run IAM do not reach the container and are not billed as application requests.

These limits keep intended use free, but are not a financial circuit breaker: an attacker keeping even one Cloud Run instance busy can exceed ₹500. Early authentication, small bodies, short timeout, and fail-fast rate limits are required.

## Bill of materials

| Component | Current unit price / included usage | Cost guardrail |
| --- | --- | --- |
| Gemini API primary: `gemini-3.6-flash` | Standard Free Tier input/output is free. Paid price is $0.75/M input and $3.75/M output through 2026-12-31, then $1.50/M and $7.50/M. | Free Tier Standard only; cap input/output; never call from browser. |
| Gemini fallback: `gemini-3.1-flash-lite` | Standard Free Tier input/output is free. Paid price is $0.25/M text input and $1.50/M output. | Use only for defined recoverable errors and only if Free Tier quota permits. |
| Gemini fallback: `gemini-flash-latest` | Alias currently maps to Gemini 3.5 Flash. Standard Free Tier input/output is free; Paid Standard is $1.50/M input and $9.00/M output. | Do not enable paid Gemini. |
| Gemini fallback: `gemini-3.7-flash` | Standard Free Tier input/output is free. Paid introductory price through 2026-12-31 is $0.75/M input and $3.75/M output. | Use only in the specified fallback order and only if Free Tier quota permits. |
| Memory Threads: `gemini-embedding-2` | Text embeddings are free on Free Tier; Paid Standard is $0.20/M text tokens. | Text only, one embedding per persisted interaction, 768 dimensions, no backfill job. |
| Search/Maps grounding | First 5,000 shared Gemini 3.x requests/month free; then $14/1,000 requests. | Disable; it is not needed by the journal. |
| Cloud Run | Request-based Tier 1 basis: $0.000024/active vCPU-second, $0.0000025/active GiB-second, $0.40/M requests. Free/month: 180,000 vCPU-seconds, 360,000 GiB-seconds, 2M requests. Mumbai (`asia-south1`) is Tier 1. | 1 vCPU, 512 MiB, `min-instances=0`, service-level `max-instances=1`, 15-second timeout. |
| Artifact Registry | First 0.5 GiB-month per billing account free; then $0.000136986/GiB-hour (about $0.10/GiB-month). This project already has about 0.86 GiB across existing repositories, implying roughly $0.04/month above the free allowance if no other billing-account usage applies. | Reuse the existing Mumbai repository for the app. Retain only three new tagged images and delete new untagged images older than seven days; do not delete pre-existing images without approval. |
| Cloud Build | 2,500 e2-standard-2 default-pool build-minutes/month per billing account free; then $0.006/minute. | Default pool only; no private pool or extra SSD. |
| Firebase Authentication | Google sign-in/other Identity Platform providers: 50,000 MAUs free, then Identity Platform pricing. | Google sign-in only; no SMS/phone auth. |
| Cloud Firestore Standard | One default database: 1 GiB storage, 50K reads/day, 20K writes/day, 20K deletes/day, 10 GiB/month egress free. Beyond us-central1: $0.03/100K reads, $0.09/100K writes, $0.01/100K deletes, $0.10/GiB-month. | Use the default database only. Do not enable PITR, backups, TTL, or a named database. |
| Firestore vector search | kNN charges one read per up-to-100 vector index entries scanned, plus reads for returned documents. Vector/index bytes count as Firestore storage. | Per-user namespace, result limit five, and no collection-wide query. |
| Secret Manager | Six active versions and 10K accesses/month per billing account free; then $0.06/version/location/month and $0.03/10K accesses. | One `GEMINI_API_KEY` version; inject server-side only. |
| Cloud Logging / Monitoring | Logging: first 50 GiB/project/month free, then $0.50/GiB. Monitoring byte-ingested custom metrics: first 150 MiB/billing account free, then $0.258/MiB. | Never log journal text, tokens, or secrets; retain default 30-day logs and avoid custom metrics. |
| Firebase App Check / reCAPTCHA | App Check is no-cost subject to attester quota. With billing, reCAPTCHA Premium: first 10K organisation-wide assessments/month free; 10,001-100K is $8 flat; then $1/1K. | Keep the one-hour token TTL; do not shorten it. |

## Previous paid multi-user scale-up reference (not the primary profile)

Assume 20 testers, each completing 30 interactions: **600 model turns/month**. For every turn, budget 2,000 chat input tokens, 600 chat output tokens, 700 summary input tokens, 150 summary output tokens, and one 300-token text embedding. No grounding is enabled.

```text
Chat + summary input:  600 × (2,000 + 700) = 1.62M tokens
Chat + summary output: 600 × (600 + 150)   = 0.45M tokens
Embeddings:            600 × 300           = 0.18M tokens

3.6 Flash: (1.62 × $0.75) + (0.45 × $3.75) = $2.9025
Embeddings: 0.18 × $0.20                    = $0.0360
10% retry/fallback allowance                  = $0.2939
Expected Gemini API total                      = $3.23/month
```

Other low-traffic assumptions remain in their free tiers: 5,000 Cloud Run requests at 1.5 seconds on 1 vCPU/512 MiB; 1,800 Firestore writes, 4,200 reads, and under 1 GiB storage; 20 five-minute builds; one 250 MB Artifact Registry image; one active secret and under 10K accesses; under 1 GiB logs; 20 MAUs; and an estimated 9,600 App Check assessments (20 users × 8 hours/day × 30 days × two refreshes/hour).

Allowing approximately $0.04/month for the Artifact Registry storage already present in the project, the expected total is therefore **about $3.27/month**, before regional egress, tax, and currency conversion. This paid estimate is retained only as a future reference and is not enabled for the ₹500 personal-hackathon profile.

## Previous paid-scale guardrail (not the primary profile)

Use a server-side *model-turn unit* permit before any model request. A unit reserves one chat, one automatic summary, and one text embedding. A retry consumes another unit before it is issued; no retry may bypass the permit.

Configure these initial limits:

- 60 model-turn units/day across the service
- 5 units/day and 2 units/minute per verified Firebase UID
- 2,000 input tokens maximum for a chat and 600 output tokens maximum
- 700 input tokens maximum for a summary and 150 output tokens maximum
- Reject oversized request bodies before model calls; fail closed with an accessible daily-capacity message

The expensive-fallback cap assumes every unit reaches Gemini 3.5 Flash Standard:

```text
Per unit = (2,700 input × $1.50/M) + (750 output × $9.00/M)
           + (300 embedding tokens × $0.20/M)
         = $0.01086

60 units/day × 30 days × $0.01086 = $19.55/month
```

This preserves roughly $5.45 within the former $25 budget for small non-AI charges. It is not enabled for the ₹500 personal-hackathon profile.

## Cloud Run saturation warning

`max-instances=1` limits parallel capacity but is not a dollar ceiling. If that one 1 vCPU/512 MiB request-based instance were continuously busy for 30 days, using the $0.000024 active vCPU-second and $0.0000025 active GiB-second rate table and subtracting the free tier, its approximate compute exposure is:

```text
(2,592,000 - 180,000) vCPU-s × $0.000024 = $57.89
(1,296,000 - 360,000) GiB-s × $0.0000025 = $2.34
                                                   -------
                                                   $60.23
```

Early Firebase-token verification, body-size limits, the model-turn permit, 20-second timeout, and `min-instances=0` make that scenario unlikely. It remains important: a budget alert is not an enforced stop. Any future budget action that disables billing must be separately approved and accepts service outage.

## Caveats and verification points

- The selected application region is Mumbai (`asia-south1`), a Tier 1 Cloud Run region supported by Firestore. The plan reuses the existing Mumbai Artifact Registry repository and co-locates the new Cloud Run service and default Firestore database there.
- Cloud Run internet egress and Firestore egress vary by client destination. The low-traffic estimate assumes they remain inside free or negligible usage; it is not an egress guarantee.
- Gemini promotional prices end on 2026-12-31. Recheck this document before January 2027.
- Exact Gemini RPM/TPM/RPD limits vary by project and paid tier; use the Google AI Studio Rate Limit dashboard, not a copied quota value.
- INR is only a planning conversion; Google bills according to the billing account currency. GST/taxes are excluded.

## Official sources

- [Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini 3.7/3.6 introductory and standard pricing](https://ai.google.dev/gemini-api/docs/latest-model)
- [Gemini model aliases and releases](https://ai.google.dev/gemini-api/docs/changelog)
- [Gemini API billing](https://ai.google.dev/gemini-api/docs/billing)
- [Gemini API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- [Cloud Run pricing](https://cloud.google.com/run/pricing)
- [Cloud Run maximum instances](https://cloud.google.com/run/docs/configuring/max-instances)
- [Artifact Registry pricing](https://cloud.google.com/artifact-registry/pricing)
- [Cloud Build pricing](https://cloud.google.com/build/pricing)
- [Firebase pricing](https://firebase.google.com/pricing)
- [Firestore pricing](https://cloud.google.com/firestore/pricing)
- [Firestore vector search](https://cloud.google.com/firestore/docs/vector-search)
- [Secret Manager pricing](https://cloud.google.com/secret-manager/pricing)
- [Google Cloud Observability pricing](https://cloud.google.com/products/observability/pricing)
- [Firebase App Check web reCAPTCHA Enterprise](https://firebase.google.com/docs/app-check/web/recaptcha-enterprise-provider)
- [reCAPTCHA billing](https://cloud.google.com/recaptcha/docs/billing-information)
- [Cloud Billing budgets and alerts](https://cloud.google.com/billing/docs/how-to/budgets)
