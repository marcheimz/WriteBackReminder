# WriteBackReminder

Simple FastAPI-based web app for logging conversations and planning reminders.

## Setup

Create and activate a virtual environment (any recent Python 3.10+ works):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Configuration is now env-only (no JSON file is read at runtime). Use the helper script to migrate values from `secrets/config.json` into environment variables.

Env setup helper:

```bash
# Print shell exports (local)
python scripts/configure_env.py --from-json secrets/config.json --use-s3 true local

# Or write a .env file
python scripts/configure_env.py --from-json secrets/config.json --use-s3 true local --dotenv .env

# Set Fly secrets (reads app name from fly.toml if --app omitted)
python scripts/configure_env.py --from-json secrets/config.json --use-s3 true fly --app writebackreminder
```

## Running the server

With the environment activated, start the development server with Uvicorn (FastAPI):

```bash
uvicorn main:app --reload
```

Alternatively, you can launch it directly via Python:

```bash
python main.py
```

The app listens on `http://127.0.0.1:8000/` by default.

## Usage

1. Open `http://127.0.0.1:5000/` and click **Sign in with Google**.
2. Once authenticated, the `/conversations` dashboard lets you log conversation summaries, review history, and view the latest AI follow-up recommendation (if one exists) for the selected person.
3. Visit `/recommendations` (or the header link) to see every suggested follow-up sorted by urgency.
4. Use the “Switch user” button to sign out and let a different Google account in.

## Google Sign-In

Google OAuth is optional and only enabled when client credentials are available.

1. Create an OAuth 2.0 Web Application credential in the [Google Cloud Console](https://console.cloud.google.com/).
2. Set the authorized redirect URI to `http://127.0.0.1:8000/auth/google` (match your deployment host/port).
3. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` as environment variables (or use the helper script above to migrate from a JSON file).
4. Restart the app. When the env vars are present, the landing page will offer a **Sign in with Google** button.

## AI follow-up suggestions

The app can call OpenAI to propose follow-up messages and urgency scores for each contact.

1. Set `OPENAI_API_KEY` in your environment (or via Fly secrets).
2. Start the app. Whenever a user logs in, the server refreshes recommendations in the background.
   - Configure the refresh cadence via `followup_refresh_hours` (set to `0` to force regeneration on each visit).
   - Change the model with `followup_model`.
3. Recommendations are stored separately from conversation history under `userdata/recommendations/`.

You can also generate a suggestion from the command line for testing:

```bash
python ai_followup.py you@example.com "Contact Name"
```

## Environment variables

All configuration values can be provided via environment variables (useful for containers and Fly.io). When set, env vars take precedence over `secrets/config.json`.

- `SECRET_KEY` — session signing key. Set a strong random string in production.
- `OPENAI_API_KEY` — enables AI follow-up generation.
- `FOLLOWUP_REFRESH_HOURS` — hours between background refreshes (float, `0` disables interval and refreshes on demand).
- `FOLLOWUP_MODEL` — OpenAI model identifier (defaults to `gpt-4o-2024-08-06`).
- `USER_DATA_DIR` — directory for user data (defaults to `userdata`).
- `RECOMMENDATIONS_DIR` — directory for AI recommendations (defaults to `userdata/recommendations`).
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — if set, Google Sign-In uses these directly.
- `GOOGLE_CREDENTIALS_PATH` — path to a JSON file with `{ "client_id": ..., "client_secret": ... }` (used when the above vars are not set; defaults to `secrets/google_oauth.json`).

Optional S3/Tigris storage (multi-region friendly)

- `USE_S3` — set to `true` to enable S3-backed storage with local read-through caching and write-back sync.
- `BUCKET_NAME` — S3/Tigris bucket name (Fly sets this for Tigris).
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` — access keys (Fly sets for Tigris).
- `AWS_ENDPOINT_URL_S3` — endpoint URL (Fly sets for Tigris).
- `AWS_REGION` — region or `auto` for Tigris.
- `S3_PREFIX` — optional prefix within the bucket (default: `writebackreminder`).

Behavior when `USE_S3=true`:
- On first access to a user, the app will try to download that user's conversation JSON from S3 into the local volume (read‑through cache).
- On every write, it persists locally and uploads the updated conversation history back to S3 (write‑back). AI recommendations stay local-only.
- Local volume still provides fast reads and survives restarts; S3 enables multi‑region serving and centralized storage.

Google OAuth redirect URI

- Local: `http://127.0.0.1:8000/auth/google`
- Fly.io: `https://YOUR-APP.fly.dev/auth/google`

## Deploy to Fly.io

This repo includes a `Dockerfile` and a baseline `fly.toml`. After installing `flyctl`:

1. Create the app (one-time):
   ```bash
   fly launch --no-deploy
   ```
2. Set secrets using the helper script (it reads your JSON config and pushes the values to Fly):
   ```bash
   python scripts/configure_env.py \
     --from-json secrets/config.json \
     --use-s3 true \
     fly --app writebackreminder
   ```
   Adjust the paths/flags to match your environment. Use `--dry-run` first if you just want to inspect the generated `fly secrets set` command.
   To override or append individual secrets manually, run `fly secrets set KEY=value` afterwards.
3. (Recommended) Persist data using a volume:
   ```bash
   fly volumes create wbr_data --size 1 --region <your-region>
   ```
   `fly.toml` is preconfigured to mount the volume at `/data` and to store
   data under `/data/userdata` and `/data/userdata/recommendations`. You can
   override these with env or secrets if needed.
4. Deploy:
   ```bash
   fly deploy
   ```

The container listens on port `8080` internally; Fly serves HTTPS externally. Update your Google OAuth redirect URI to the Fly URL shown after launch.
