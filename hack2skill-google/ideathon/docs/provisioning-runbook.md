# Provisioning Runbook

This runbook provisions the minimum Google Cloud and Firebase surface for the
hackathon demo. It is deliberately review-first: run each command only after
checking the prerequisite and expected outcome. Re-running the commands is
safe; the intended resources are either enabled, created once, or updated to
the declared configuration.

**Scope**

- Google Cloud project: `genai-academy-temp`
- Regional services: `asia-south1` (Mumbai)
- Gemini: Gemini API free tier only. Do **not** enable a paid Gemini upgrade,
  add a paid API key, or use grounding, media generation, or other paid
  features.
- Runtime: one Cloud Run service, scaling from zero with a maximum of one
  instance.

> Important: Google Cloud budgets are alerts, not hard spending caps. The
> Cloud Run and application request limits are still required to control cost.

## 1. Preflight and project selection

Authenticate using an account that can administer the project, then set the
active project and region for this terminal session:

```bash
gcloud auth login
gcloud config set project genai-academy-temp
gcloud config set run/region asia-south1
gcloud projects describe genai-academy-temp --format='value(projectNumber,projectId)'
gcloud billing projects describe genai-academy-temp
```

Expected result: the project exists, its project ID is exactly
`genai-academy-temp`, and billing is enabled. Do not continue if billing is
not enabled, as Cloud Run publication and Artifact Registry usage can fail.

Set review variables for the remaining commands:

```bash
export PROJECT_ID='genai-academy-temp'
export REGION='asia-south1'
export SERVICE_NAME='ai-reflection'
export RUNTIME_SA_NAME='ai-reflection-runtime'
export RUNTIME_SA_EMAIL="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
export SECRET_NAME='gemini-api-key'
```

## 2. Enable required APIs

Enable only the APIs used by the app and deployment path:

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  firestore.googleapis.com \
  firebase.googleapis.com \
  firebaseappcheck.googleapis.com \
  identitytoolkit.googleapis.com \
  serviceusage.googleapis.com
```

If the implementation uses the Google Gen AI SDK with a Gemini API key,
enable the Gemini Developer API only if it is not already available through
the approved Google AI Studio project configuration:

```bash
gcloud services enable generativelanguage.googleapis.com
```

This command does not upgrade Gemini billing. Keep the key and all requests on
the free tier. Before deploying, verify active services:

```bash
gcloud services list --enabled --format='value(config.name)' | \
  rg 'run|cloudbuild|artifactregistry|secretmanager|firestore|firebase|identitytoolkit|generativelanguage'
```

## 3. Initialize Firebase (user-owned console step)

Firebase project association and Google sign-in consent require the project
owner to act in the Firebase console:

1. Open the [Firebase console](https://console.firebase.google.com/).
2. Select or add the existing Google Cloud project `genai-academy-temp`.
3. Accept Firebase terms if prompted.
4. In **Authentication → Sign-in method**, enable **Google**.
5. Set an authorised support email and save.
6. In **Project settings → General**, register the web application and record
   its browser Firebase configuration for the application deployment settings.

Do not treat the browser Firebase configuration as a secret; Firebase ID token
verification, not config concealment, provides access control. Never put the
Gemini API key in Firebase config, client code, repository files, or browser
environment variables.

Verify the associated project from the CLI after completing the console step:

```bash
firebase projects:list
firebase use genai-academy-temp
```

If the `firebase` CLI is unavailable, verify project association in Firebase
Console instead. Do not install tooling solely for this runbook without
reviewing the local development setup.

## 4. Create the default Firestore database

Create the default native Firestore database once in `asia-south1`:

```bash
gcloud firestore databases create \
  --database='(default)' \
  --location=asia-south1 \
  --type=firestore-native
```

If it reports that the database already exists, inspect rather than recreate:

```bash
gcloud firestore databases describe --database='(default)' \
  --format='yaml(name,locationId,type,deleteProtectionState)'
```

Expected result: database location `asia-south1` and type `FIRESTORE_NATIVE`.
Database location cannot be changed later; stop for review if it differs.

## 5. Deploy the secure Firestore rules

The repository’s `firestore.rules` must restrict every user document path to
the authenticated owner. It must never contain `allow read, write: if true`.
Review before deployment:

```bash
rg -n "allow read, write: if true|request\.auth\.uid|match /users" firestore.rules
```

Deploy only the reviewed rules file:

```bash
firebase deploy --only firestore:rules --project "$PROJECT_ID"
```

Required policy shape for user data:

```javascript
match /users/{userId}/{document=**} {
  allow read, write: if request.auth != null && request.auth.uid == userId;
}
```

The server must independently enforce verified-token UID boundaries because
Admin SDK writes bypass Firestore Security Rules.

## 6. Create the dedicated Cloud Run runtime identity

Create the service account if it does not yet exist:

```bash
gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
  --display-name='AI Reflection Cloud Run runtime'
```

Grant only the roles needed at runtime:

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role='roles/datastore.user'

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role='roles/firebaseappcheck.tokenVerifier'
```

The App Check role permits the custom Cloud Run backend to verify App Check
tokens. Do not grant Editor, Owner, Service Account Token Creator, or
project-wide Secret Accessor. `roles/datastore.user` is needed only if the
server persists data using the Firestore Admin SDK; remove it if the final
architecture does not.

Verify the account and its bindings:

```bash
gcloud iam service-accounts describe "$RUNTIME_SA_EMAIL"
gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.members:serviceAccount:${RUNTIME_SA_EMAIL}" \
  --format='table(bindings.role)'
```

## 7. Store and inject the Gemini API key

Create the secret once. Supply the approved free-tier Gemini API key through
the terminal prompt; do not paste it into shell history, source code, or this
runbook:

```bash
read -s 'GEMINI_KEY?Paste approved free-tier Gemini API key: '
printf '%s' "$GEMINI_KEY" | gcloud secrets create "$SECRET_NAME" \
  --replication-policy='automatic' \
  --data-file=-
unset GEMINI_KEY
```

If the secret already exists, add a new version only when rotating the key:

```bash
read -s 'GEMINI_KEY?Paste replacement free-tier Gemini API key: '
printf '%s' "$GEMINI_KEY" | gcloud secrets versions add "$SECRET_NAME" --data-file=-
unset GEMINI_KEY
```

Now grant the dedicated runtime identity access:

```bash
gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
  --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role='roles/secretmanager.secretAccessor'
```

Check metadata only—never print secret contents:

```bash
gcloud secrets describe "$SECRET_NAME"
gcloud secrets versions list "$SECRET_NAME"
```

At deploy time, Cloud Run receives the latest secret version as `GEMINI_API_KEY`.
Only server code may read it and call Gemini.

## 8. Reuse the existing Artifact Registry repository

List repositories in the selected region first:

```bash
gcloud artifacts repositories list --location="$REGION" \
  --format='table(name,format,location,description)'
```

Select the existing Docker-format repository from that output and set its exact
repository ID:

```bash
export ARTIFACT_REPOSITORY='REPLACE_WITH_EXISTING_DOCKER_REPOSITORY_ID'
gcloud artifacts repositories describe "$ARTIFACT_REPOSITORY" \
  --location="$REGION" \
  --format='yaml(name,format,location)'
```

Expected result: `format: DOCKER` and `location: asia-south1`. Reuse it; do not
create a duplicate registry repository. Retain at most three demo images under
the repository’s cleanup policy to avoid accumulating storage charges.

## 9. Deploy Cloud Run with the fixed cost envelope

Deploy the reviewed container image. Replace `IMAGE_URI` only with an image in
the verified existing Artifact Registry repository:

```bash
export IMAGE_URI='asia-south1-docker.pkg.dev/genai-academy-temp/REPLACE_REPOSITORY/ai-reflection:demo'

gcloud run deploy "$SERVICE_NAME" \
  --image="$IMAGE_URI" \
  --region="$REGION" \
  --service-account="$RUNTIME_SA_EMAIL" \
  --set-secrets="GEMINI_API_KEY=${SECRET_NAME}:latest" \
  --cpu='1' \
  --memory='512Mi' \
  --concurrency='4' \
  --timeout='15s' \
  --min-instances='0' \
  --max-instances='1' \
  --update-labels='dev-tutorial=cloud-run-ai-challenge' \
  --no-allow-unauthenticated
```

`--no-allow-unauthenticated` is the safe default. If hackathon judging requires
a public URL, first confirm that the application’s Firebase token middleware
protects every API route; then make only the service invoker binding public:

```bash
gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --region="$REGION" \
  --member='allUsers' \
  --role='roles/run.invoker'
```

Do not make the service public until the authenticated application flow has
been tested. This does not expose Gemini credentials, which remain server-side
in Secret Manager.

## 10. Budget visibility and low-cost operating settings

In **Cloud Billing → Budgets & alerts**, create a monthly budget of **₹500**
(or its displayed INR equivalent) for `genai-academy-temp`, with email alerts
at ₹100, ₹250, ₹400, and ₹500. Include all services, and add the billing-owner
email as a notification recipient.

The expected upfront deployment charge is **₹0**. For one user with a small
demo workload, Cloud Run, Firestore, Firebase Authentication, Secret Manager,
and logging should stay within their free quotas; Artifact Registry’s retained
image storage can create a small monthly charge. Gemini must remain on the
free tier, using synthetic demo content only.

In addition to Cloud Run’s single-instance limit, configure the application to
limit total Gemini generations and short responses. A practical demo limit is
ten interactions per month, including retries and automatic summaries. Do not
enable paid Gemini features as a workaround for quota exhaustion.

## 11. Post-deploy verification

Verify the deployed service configuration:

```bash
gcloud run services describe "$SERVICE_NAME" --region="$REGION" \
  --format='yaml(metadata.labels,spec.template.spec.serviceAccountName,spec.template.spec.containerConcurrency,spec.template.spec.timeoutSeconds,spec.template.metadata.annotations,spec.template.spec.containers)'
```

Confirm all of the following in the output or Cloud Run console:

- Label `dev-tutorial=cloud-run-ai-challenge` is present.
- Runtime identity is `ai-reflection-runtime@genai-academy-temp.iam.gserviceaccount.com`.
- CPU is 1, memory is 512 MiB, concurrency is 4, timeout is 15 seconds.
- Minimum instances is 0 and maximum instances is 1.
- `GEMINI_API_KEY` is a Secret Manager reference, not a literal value.

Then perform an application-level test with two Firebase users:

1. Sign in as User A and create a reflection.
2. Confirm its chat turn and automatic summary persist under User A only.
3. Sign in as User B and verify User A’s data cannot be listed, read, edited,
   or deleted.
4. Submit a malformed or expired Firebase token and verify the API rejects it.
5. Trigger a failed write in a safe test environment and ensure the client
   retains the unsaved input and offers a retry.
6. Verify no Cloud Run logs include journal text, tokens, API keys, or secrets.

## 12. Rollback and access shutdown

To roll back a bad release, first list revisions and route traffic to the last
known-good revision (replace the placeholder after review):

```bash
gcloud run revisions list --service="$SERVICE_NAME" --region="$REGION"
gcloud run services update-traffic "$SERVICE_NAME" \
  --region="$REGION" \
  --to-revisions='REPLACE_WITH_KNOWN_GOOD_REVISION=100'
```

To immediately remove public invocation access after a demo:

```bash
gcloud run services remove-iam-policy-binding "$SERVICE_NAME" \
  --region="$REGION" \
  --member='allUsers' \
  --role='roles/run.invoker'
```

To stop all runtime request serving while preserving the service configuration,
remove public access as above and remove any remaining explicit invoker
bindings that are no longer needed. If a complete shutdown is required, delete
the specific Cloud Run service only after recording its URL and revision data:

```bash
gcloud run services delete "$SERVICE_NAME" --region="$REGION" --quiet
```

Deleting the service is irreversible for its revisions. It does not delete
Firestore data, the Gemini secret, Firebase configuration, or images. Rotate
the Gemini key and disable or destroy unneeded secret versions if compromise is
suspected; retain only the active version needed for the demo.

For project handoff, revoke temporary human IAM access separately from the
runtime service account. Do not delete the runtime identity until the Cloud Run
service is fully retired.
