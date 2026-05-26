# Help Scout Custom App — Triage Button

**Date:** 2026-05-26
**Status:** Approved

## Goal

Add a Help Scout Custom App with a single "Triage Tickets" button. Clicking it runs `run_triage(auto_apply=True)` against all unassigned tickets in the first mailbox — no confirmation prompt, no per-conversation scope. This gives a human operator a one-click way to trigger triage from anywhere inside Help Scout.

## What Changes

| File | Action |
|---|---|
| `scripts/app.py` | New — Flask server (replaces webhook server) |
| `scripts/triage_webhook_server.py` | Deleted |
| `requirements.txt` | Add `flask`, `gunicorn` |
| `render.yaml` | New — Render service config |

`scripts/triage_tickets.py` is unchanged.

## Server Design

A Flask app (`scripts/app.py`) with three routes:

### `GET /`
Serves a self-contained HTML page. The page has a single "Triage Tickets" button. On click, the button:
1. Makes a `fetch` POST to `/triage` (same origin).
2. Shows "Triage started!" inline — no page reload.
3. Does not wait for the job to finish.

### `POST /triage`
Spawns a daemon thread calling `run_triage(auto_apply=True)`. Returns `200 ok` immediately. The background job runs to completion independently.

### `GET /health`
Returns `200 ok` for Render health checks.

### Gunicorn config
`--workers 1 --threads 4` — single worker so the background thread lives in the same process as the request handler, avoiding any IPC complexity.

## Help Scout Custom App Setup (manual, post-deploy)

1. In Help Scout: **Manage → Apps → Custom Apps → Create**
2. **Content URL**: Render service URL (e.g. `https://happier-triage.onrender.com`)
3. **Callback URL**: leave blank
4. **Secret Key**: leave blank (no auth required)

The iframe loads `GET /` and renders the button. Behavior is the same regardless of which conversation is currently open — it always triages all unassigned tickets.

## Render Deployment

Declared via `render.yaml` in the repo root:

- **Type**: web service (Python 3)
- **Build**: `pip install -r requirements.txt`
- **Start**: `gunicorn --workers 1 --threads 4 --chdir scripts app:app`
- **Env vars** (set as secrets in Render dashboard):
  - `HELPSCOUT_APP_ID`
  - `HELPSCOUT_APP_SECRET`
  - `ANTHROPIC_API_KEY`

The CSV files (`tags.csv`, `teams.csv`, `custom_fields.csv`) are committed to the repo and resolved relative to the repo root by `triage_tickets.py` — no path changes needed.

## Non-Goals

- No authentication on the `/triage` endpoint.
- No progress tracking or status display in the UI.
- No webhook handling (the webhook server is being removed).
- No changes to triage logic or prompts.
