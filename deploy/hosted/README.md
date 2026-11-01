# AskHooman hosted bridge on Cloud Run

The multi-tenant bridge (SPEC.md, section "Hosted bridge and agent connection (v4)"): the same `server/` package, switched on with `ASKS_MODE=hosted`. Asks and tasks live in each user's Google Drive appDataFolder; everything else (users, agents, OAuth clients, tokens, keys) lives in Firestore. There is no local state, so any number of instances can run.

Placeholders used below: `<PROJECT_ID>` (your Google Cloud project), `<REGION>` (e.g. `europe-west1`; it needs Firestore and Cloud Run domain mappings), `<PUBLIC_HOST>` (the hostname the service is reached at, e.g. `askhooman.example.com`). MCP is then at `https://<PUBLIC_HOST>/mcp`.

Files here:

```
Dockerfile        python:3.11-slim + uvicorn on $PORT; the build context is the repository root
requirements.txt  server/requirements.txt + google-cloud-firestore (the single-tenant bridge doesn't need it)
cloudbuild.yaml   builds the image into Artifact Registry of the project the build runs in
.gcloudignore     only server/ (without tests) and deploy/hosted/ are uploaded to Cloud Build
```

`gcloud run deploy --source` only supports a Dockerfile at the root of the sources, so the image is built with `cloudbuild.yaml` and deployed with `--image`.

## Your own Google Cloud project

A self-hosted server needs its own project and its own OAuth clients. Drive's appDataFolder is scoped to one Google Cloud project: files the bridge writes there are visible only to clients of the same project. So the official AskHooman apps (which belong to the official project) cannot see data written by your server; you need your own client app (or your own build of one) registered in your project.

Create in the Google Cloud console:

- OAuth consent screen with the scopes `openid`, `email` and `https://www.googleapis.com/auth/drive.appdata`. While it is in testing (or Internal for a Workspace organization), only listed or organization accounts can sign in; `drive.appdata` needs Google's verification for a public External app.
- OAuth client of type **Web application** with the redirect URI `https://<PUBLIC_HOST>/oauth/google/callback` (and the same path on every host in `ASKS_PUBLIC_URL_ALIASES`). Its JSON (`{"web": {"client_id", "client_secret", ...}}` as the console downloads it, or a flat `{"client_id", "client_secret"}`) goes into Secret Manager, step 4 below.
- The OAuth client(s) of your app (e.g. an iOS client): their client ids are the accepted audiences of the app's Google ID tokens for the App API (`ASKS_GOOGLE_APP_CLIENT_IDS`).

## First deploy

From the repository root:

```bash
PROJECT=<PROJECT_ID>
REGION=<REGION>
gcloud config set project $PROJECT

# 1. APIs
gcloud services enable run.googleapis.com firestore.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com drive.googleapis.com

# 2. Firestore in Native mode, and TTL for documents that expire (the server writes the expire_time field)
gcloud firestore databases create --location=$REGION --type=firestore-native
for c in requests codes tokens families clients; do
  gcloud firestore fields ttls update expire_time --collection-group=$c --enable-ttl --async
done

# 3. The service's runtime service account
gcloud iam service-accounts create qtracker-run --display-name="Qtracker hosted bridge"
SA=qtracker-run@$PROJECT.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA --role=roles/datastore.user

# 4. Secrets: the Web client JSON and the encryption key (32 random bytes, base64; never printed)
gcloud secrets create qtracker-google-web-client --replication-policy=automatic \
  --data-file=/path/to/client_secret_<WEB_CLIENT_ID>.json
python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode(),end="")' | \
  gcloud secrets create qtracker-encryption-key --replication-policy=automatic --data-file=-
for s in qtracker-google-web-client qtracker-encryption-key; do
  gcloud secrets add-iam-policy-binding $s --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
done

# 5. Artifact Registry and the image
gcloud artifacts repositories create qtracker --repository-format=docker --location=$REGION
IMAGE=$REGION-docker.pkg.dev/$PROJECT/qtracker/bridge:$(date +%Y%m%d-%H%M)
gcloud builds submit --config deploy/hosted/cloudbuild.yaml --ignore-file deploy/hosted/.gcloudignore \
  --substitutions _IMAGE=$IMAGE .

# 6. The service
gcloud run deploy qtracker --image=$IMAGE --region=$REGION --service-account=$SA \
  --allow-unauthenticated --no-cpu-throttling --min-instances=0 --max-instances=3 --memory=512Mi \
  --set-env-vars=ASKS_PUBLIC_URL=https://<PUBLIC_HOST>,ASKS_GOOGLE_APP_CLIENT_IDS=<APP_CLIENT_ID>,ASKS_FIRESTORE_PROJECT=$PROJECT \
  --set-secrets=ASKS_GOOGLE_WEB_CLIENT=qtracker-google-web-client:latest,ASKS_ENCRYPTION_KEY=qtracker-encryption-key:latest

# 7. Domain (the domain must be verified for your Google account: gcloud domains verify <your domain>)
gcloud beta run domain-mappings create --service=qtracker --domain=<PUBLIC_HOST> --region=$REGION
gcloud beta run domain-mappings describe --domain=<PUBLIC_HOST> --region=$REGION
```

At your DNS provider: `CNAME <PUBLIC_HOST> → ghs.googlehosted.com`. If the zone is on Cloudflare, the record must be **DNS only** (not proxied), or Google cannot issue the certificate. The certificate appears within 15–60 minutes; `domain-mappings describe` shows its status.

Why these flags:

- `--allow-unauthenticated`: the service is public and does all authorization itself (OAuth, agent keys, ID tokens). If an organization policy forbids `allUsers`, the deploy says so and the project needs an exception.
- `--no-cpu-throttling`: webhooks to agents and their retries (5 s, 30 s, 2 min) run in the background after the response. With throttling, the background thread stalls until the next request. Cost: the instance is billed while it is alive (up to ~15 minutes after the last request).
- `--max-instances=3`: rate limits are counted per instance, so their overall ceiling grows with the number of instances.
- Agent webhooks and client metadata documents are accepted only on https port 443.

If `gcloud builds submit` fails on permissions: the build service account (in new projects `<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`) needs `roles/artifactregistry.writer` and `roles/logging.logWriter`.

## Check

```bash
curl -s https://<PUBLIC_HOST>/.well-known/oauth-authorization-server   # issuer, endpoints, S256, CIMD
curl -si -X POST https://<PUBLIC_HOST>/mcp | grep -i www-authenticate   # resource_metadata=...
claude mcp add --transport http askhooman https://<PUBLIC_HOST>/mcp     # then /mcp → askhooman → Authenticate
```

(`/healthz` works locally, but Cloud Run's frontend reserves paths ending in `z` and answers 404 there.)

The browser opens Google sign-in, then the page "Claude Code wants to ask you questions…" with the agent name. After Allow, the agent sees `asks_mine`, `ask_create`, `tasks_mine` and the other tools, and its asks appear in the appDataFolder of that Google account.

## Continuous deploy from GitHub

`.github/workflows/deploy.yml` builds and deploys on every push to `main` with keyless auth (GitHub OIDC → Workload Identity Federation → a deploy service account). It runs only when the repository variables `GCP_PROJECT`, `GCP_REGION`, `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SA` and `CLOUD_RUN_SERVICE` are set; a fork without them skips the job. The deploy service account needs Cloud Run Admin, Service Account User on the runtime service account, Cloud Build Editor and Artifact Registry Writer.

## Update by hand

```bash
IMAGE=$REGION-docker.pkg.dev/$PROJECT/qtracker/bridge:$(date +%Y%m%d-%H%M)
gcloud builds submit --config deploy/hosted/cloudbuild.yaml --ignore-file deploy/hosted/.gcloudignore \
  --substitutions _IMAGE=$IMAGE .
gcloud run deploy qtracker --image=$IMAGE --region=$REGION   # env vars and secrets are kept
```

Logs: `gcloud run services logs read qtracker --region=$REGION --limit=100`. Tokens and secrets never appear in the logs. Cloud Run's request logs do show Google's callback URL with a one-time `code` and `state`; without the Web client secret, the PKCE verifier and the browser cookie they are useless.

## Environment variables

| variable | value |
|---|---|
| `ASKS_MODE` | `hosted` (already set in the Dockerfile) |
| `ASKS_PUBLIC_URL` | `https://<PUBLIC_HOST>` (default `https://askhooman.com`): the OAuth issuer; token audience = `<it>/mcp`; Google redirect = `<it>/oauth/google/callback` |
| `ASKS_PUBLIC_URL_ALIASES` | optional, comma-separated: more hosts of the same service, e.g. the old domain after a rename. Each keeps its own issuer and token audience, so agents connected there keep working; map the domain to the service and add `<alias>/oauth/google/callback` to the Web client's redirect URIs |
| `ASKS_GOOGLE_WEB_CLIENT` | secret `qtracker-google-web-client`: JSON with `client_id`/`client_secret` (the value or a path to the file) |
| `ASKS_GOOGLE_APP_CLIENT_IDS` | client id(s) of your app's OAuth client(s), comma-separated |
| `ASKS_ENCRYPTION_KEY` | secret `qtracker-encryption-key`: base64 of 32 bytes; to rotate, `k2:<new>,k1:<old>` (the first encrypts, all decrypt) |
| `ASKS_STORE` | `firestore` (default); `memory` only for local development |
| `ASKS_FIRESTORE_PROJECT` | `<PROJECT_ID>` (default: the project of the environment) |
| `ASKS_FIRESTORE_DATABASE` | `(default)` |
| `ASKS_TRUST_FORWARDED` | `1` (default): the client IP for rate limits is the last `X-Forwarded-For` entry, which Cloud Run's frontend sets |

Local run without Google Cloud (in-memory store; Google sign-in works only if the Web client's redirect URI points at localhost):

```bash
ASKS_MODE=hosted ASKS_STORE=memory ASKS_PUBLIC_URL=http://127.0.0.1:8799 \
ASKS_GOOGLE_WEB_CLIENT='{"client_id":"x","client_secret":"y"}' ASKS_GOOGLE_APP_CLIENT_IDS=ios \
ASKS_ENCRYPTION_KEY=$(python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())') \
uv run --with-requirements server/requirements.txt python -m uvicorn server.app:app --port 8799
```
