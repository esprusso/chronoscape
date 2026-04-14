# Chronoscape

A visual life reflection app for logging and exploring life events on an interactive horizontal timeline. Rate emotional weight, organize events into Eras, and use a local LLM as a reflective biographer to turn fragmented memories into cohesive entries.

## Quick Start

```bash
docker compose up -d
```

Open [http://localhost:8000](http://localhost:8000)

### Without Docker

```bash
pip install -r requirements.txt
python main.py
```

## LLM Setup

Chronoscape connects to a local LLM via an OpenAI-compatible API (LM Studio, Ollama, etc.) for its reflection engine.

| Variable | Default | Description |
|---|---|---|
| `LLM_BASE_URL` | `http://host.docker.internal:1234/v1` | OpenAI-compatible API endpoint |
| `LLM_MODEL` | `qwen3.5-122b-a10b` | Model name |
| `LLM_API_KEY` | `lm-studio` | API key (LM Studio ignores this) |

You can also change these from the Settings panel (gear icon) at runtime.

## Google Auth Setup

Google sign-in is disabled unless both of these environment variables are present:

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_CLIENT_ID` | Yes | OAuth client ID from Google Cloud |
| `GOOGLE_CLIENT_SECRET` | Yes | OAuth client secret from Google Cloud |

For production, also set these:

| Variable | Required | Description |
|---|---|---|
| `APP_SECRET_KEY` | Yes | Signing secret for session and OAuth flow cookies |
| `SETTINGS_ENCRYPTION_KEY` | Recommended | Encryption key for stored per-user LLM API keys |
| `COOKIE_SECURE` | Recommended | Set to `true` for HTTPS deployments. On Vercel this now defaults to `true`. |

Google Cloud OAuth must include this authorized redirect URI:

```text
https://YOUR-PRODUCTION-DOMAIN/auth/callback
```

For local development, add:

```text
http://localhost:8000/auth/callback
```

On Vercel, add the variables in Project Settings -> Environment Variables, then redeploy.

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `N` | Add new event |
| `E` | Export timeline |
| `← →` | Scroll timeline |
| `+ -` | Zoom in / out |
| `Esc` | Close panels / deselect |

## Data

Chronoscape now prefers an external Postgres database when one is configured via:

- `DATABASE_URL`
- `POSTGRES_URL`
- `POSTGRES_URL_NON_POOLING`

If none of those are present, it falls back to local SQLite at `/tmp/data/timeline.db`.

For local Docker or persistent self-hosting, set `DATA_DIR` to a mounted directory such as `./data` so the SQLite database survives restarts.

For Vercel production, use Postgres. SQLite under `/tmp` is only suitable for local development and throwaway preview testing.

## Troubleshooting

### LM Studio not reachable

- Ensure LM Studio is running and the local server is started (Settings > Local Server > Start Server)
- Verify `LLM_BASE_URL` points to the correct address and port
- In Docker, `host.docker.internal` resolves to the host machine. On Linux, the `extra_hosts` directive in `docker-compose.yml` handles this. If it still fails, try using the host's LAN IP directly

### Model not loaded

- Open LM Studio and load the model specified in `LLM_MODEL`
- Use Settings > Test Connection to see available models

### Malformed JSON responses

- Some models struggle with structured JSON output. Try a larger model or one known for instruction-following
- The backend automatically strips markdown fences and `<think>` blocks
- Check server logs for raw LLM output: `docker compose logs -f`

### Export issues

- If the exported PNG is blank or cropped, try a different zoom level
- Large timelines may take a moment to render — wait for the download prompt
- The export temporarily removes scrolling constraints to capture the full timeline

### Vercel persistence

- `/tmp` is ephemeral in Vercel serverless functions and is cleared between cold starts
- Use `/tmp` only for per-request scratch files or short-lived cache data
- For Chronoscape auth and multi-user data, configure `DATABASE_URL` or attach Vercel Postgres so the app uses Postgres automatically
- Move user uploads to object storage such as Vercel Blob or S3
- Move structured or long-lived app data to an external database such as Postgres, Supabase, Neon, or MongoDB
