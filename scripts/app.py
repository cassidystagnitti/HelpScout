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
