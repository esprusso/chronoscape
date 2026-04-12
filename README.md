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

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `N` | Add new event |
| `E` | Export timeline |
| `← →` | Scroll timeline |
| `+ -` | Zoom in / out |
| `Esc` | Close panels / deselect |

## Data

SQLite database is stored in `./data/timeline.db`. Mount this directory to persist data across container restarts.

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
