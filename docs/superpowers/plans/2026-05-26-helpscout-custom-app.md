# Help Scout Custom App — Triage Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unused webhook server with a Flask app that serves a Help Scout Custom App iframe containing a single "Triage Tickets" button, deployed on Render.

**Architecture:** A Flask app (`scripts/app.py`) exposes three routes: `GET /` returns the button UI, `POST /triage` kicks off `run_triage(auto_apply=True)` in a background daemon thread and returns immediately, and `GET /health` serves Render's health check. Gunicorn runs with one worker and four threads so the background thread shares the same process.

**Tech Stack:** Python 3, Flask, Gunicorn, Render (web service), pytest

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Create | `scripts/app.py` | Flask server — serves UI and triggers triage |
| Create | `conftest.py` | Adds `scripts/` to `sys.path` for pytest |
| Create | `tests/test_app.py` | Route tests using Flask test client |
| Create | `render.yaml` | Render web service declaration |
| Modify | `requirements.txt` | Add `flask`, `gunicorn`, `pytest` |
| Delete | `scripts/triage_webhook_server.py` | Replaced by `scripts/app.py` |

---

## Task 1: Update requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add flask, gunicorn, and pytest**

Open `requirements.txt`. Replace the entire contents with:

```
anthropic>=0.40.0
flask>=3.0.0
gunicorn>=21.0.0
pytest>=8.0.0
python-dotenv>=1.0.0
requests>=2.31.0
```

- [ ] **Step 2: Install new dependencies**

```bash
pip install -r requirements.txt
```

Expected: installs without errors.

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "feat: add flask, gunicorn, pytest to requirements"
```

---

## Task 2: Create conftest.py and failing tests

**Files:**
- Create: `conftest.py`
- Create: `tests/test_app.py`

- [ ] **Step 1: Create conftest.py at repo root**

```python
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
```

This lets pytest find `app.py` (and `triage_tickets.py`) in `scripts/` without needing `PYTHONPATH` to be set manually.

- [ ] **Step 2: Create tests/test_app.py**

```python
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

import app as flask_app_module
from app import app


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.data == b"ok"


def test_index_returns_html_with_button(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.content_type.startswith("text/html")
    assert b"Triage Tickets" in resp.data


def test_triage_returns_200_immediately(client):
    with patch.object(flask_app_module, "run_triage"):
        resp = client.post("/triage")
    assert resp.status_code == 200


def test_triage_calls_run_triage_with_auto_apply(client):
    called = threading.Event()

    def fake_triage(**kwargs):
        assert kwargs == {"auto_apply": True}
        called.set()

    with patch.object(flask_app_module, "run_triage", side_effect=fake_triage):
        client.post("/triage")

    assert called.wait(timeout=2), "run_triage was not called within 2 seconds"
```

- [ ] **Step 3: Run tests to confirm they fail (app.py doesn't exist yet)**

```bash
pytest tests/test_app.py -v
```

Expected: `ModuleNotFoundError: No module named 'app'` or similar import failure. This confirms TDD baseline.

- [ ] **Step 4: Commit**

```bash
git add conftest.py tests/test_app.py
git commit -m "test: add Flask app route tests"
```

---

## Task 3: Create scripts/app.py

**Files:**
- Create: `scripts/app.py`

- [ ] **Step 1: Create scripts/app.py**

```python
import os
import sys
import threading

from flask import Flask, Response

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from triage_tickets import run_triage  # noqa: E402

app = Flask(__name__)

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Triage</title>
  <style>
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      padding: 16px;
      background: #fff;
    }
    button {
      background: #3d68ff;
      color: #fff;
      border: none;
      border-radius: 6px;
      padding: 10px 20px;
      font-size: 14px;
      font-weight: 500;
      cursor: pointer;
      width: 100%;
    }
    button:hover { background: #2a52d4; }
    button:disabled { background: #aaa; cursor: default; }
    #status { margin-top: 12px; font-size: 13px; color: #555; min-height: 1.2em; }
  </style>
</head>
<body>
  <button id="btn" onclick="triage()">Triage Tickets</button>
  <div id="status"></div>
  <script>
    async function triage() {
      const btn = document.getElementById('btn');
      const status = document.getElementById('status');
      btn.disabled = true;
      status.textContent = '';
      try {
        await fetch('/triage', { method: 'POST' });
        status.textContent = 'Triage started!';
      } catch (e) {
        status.textContent = 'Error — check server logs.';
      } finally {
        btn.disabled = false;
      }
    }
  </script>
</body>
</html>
"""


@app.get("/health")
def health():
    return Response("ok", mimetype="text/plain")


@app.get("/")
def index():
    return Response(_HTML, mimetype="text/html")


@app.post("/triage")
def triage():
    threading.Thread(
        target=run_triage,
        kwargs={"auto_apply": True},
        daemon=True,
    ).start()
    return Response("ok", mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8765")))
```

- [ ] **Step 2: Run tests to confirm they pass**

```bash
pytest tests/test_app.py -v
```

Expected output (all four tests passing):
```
tests/test_app.py::test_health_returns_ok PASSED
tests/test_app.py::test_index_returns_html_with_button PASSED
tests/test_app.py::test_triage_returns_200_immediately PASSED
tests/test_app.py::test_triage_calls_run_triage_with_auto_apply PASSED
```

- [ ] **Step 3: Commit**

```bash
git add scripts/app.py
git commit -m "feat: add Flask custom app server with triage button"
```

---

## Task 4: Create render.yaml

**Files:**
- Create: `render.yaml`

- [ ] **Step 1: Create render.yaml at repo root**

```yaml
services:
  - type: web
    name: helpscout-triage
    env: python
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn --workers 1 --threads 4 --chdir scripts app:app
    envVars:
      - key: HELPSCOUT_APP_ID
        sync: false
      - key: HELPSCOUT_APP_SECRET
        sync: false
      - key: ANTHROPIC_API_KEY
        sync: false
```

`sync: false` means these values must be set manually in the Render dashboard (they're secrets, not committed).

- [ ] **Step 2: Commit**

```bash
git add render.yaml
git commit -m "feat: add Render deployment config"
```

---

## Task 5: Delete the webhook server

**Files:**
- Delete: `scripts/triage_webhook_server.py`

- [ ] **Step 1: Delete the file**

```bash
git rm scripts/triage_webhook_server.py
```

- [ ] **Step 2: Run the full test suite to confirm nothing broke**

```bash
pytest tests/test_app.py -v
```

Expected: all four tests still pass.

- [ ] **Step 3: Commit**

```bash
git commit -m "chore: remove unused webhook server (replaced by Flask app)"
```

---

## Task 6: Deploy to Render

This task is manual and done in the Render dashboard / CLI.

- [ ] **Step 1: Push the branch to GitHub**

```bash
git push origin help-scout-triage-webhooks-and-scripts
```

- [ ] **Step 2: Create the Render service**

Option A — Blueprint (recommended if `render.yaml` is on main/merged):
1. In the Render dashboard, click **New → Blueprint**
2. Connect the GitHub repo — Render detects `render.yaml` and creates the service automatically.

Option B — Manual:
1. In the Render dashboard, click **New → Web Service**
2. Connect the GitHub repo, select the branch
3. Set:
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `gunicorn --workers 1 --threads 4 --chdir scripts app:app`
4. Add the three env vars (`HELPSCOUT_APP_ID`, `HELPSCOUT_APP_SECRET`, `ANTHROPIC_API_KEY`) under **Environment**.

- [ ] **Step 3: Verify the deploy**

Once the service is live, open the Render URL in a browser. You should see the "Triage Tickets" button on a white page.

Visit `<render-url>/health` — should return `ok`.

- [ ] **Step 4: Register the Custom App in Help Scout**

1. In Help Scout: **Manage → Apps → Custom Apps → Create**
2. **App Name**: Triage
3. **Content URL**: `https://<your-render-url>`
4. Leave **Callback URL** and **Secret Key** blank.
5. Save.

The app will now appear in the conversation sidebar. Click the button to verify triage fires (watch Render logs to confirm `run_triage` starts).
