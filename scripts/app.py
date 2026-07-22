import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, Response

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from triage_tickets import run_triage  # noqa: E402

app = Flask(__name__)

# Daily automatic run. Wall-clock time in SCHEDULE_TZ, so DST is handled.
# Set TRIAGE_SCHEDULE_ENABLED=0 to disable (tests, local dev).
SCHEDULE_TZ = ZoneInfo("America/New_York")
SCHEDULE_HOUR = 8
SCHEDULE_MINUTE = 30

# One triage run at a time, whether started by the button or the schedule.
_triage_lock = threading.Lock()

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
    #note { margin-top: 4px; font-size: 12px; color: #999; }
  </style>
</head>
<body>
  <button id="btn" onclick="triage()">Triage Tickets</button>
  <div id="status"></div>
  <div id="note">Also runs automatically every day at 8:30 AM ET.</div>
  <script>
    async function triage() {
      const btn = document.getElementById('btn');
      const status = document.getElementById('status');
      btn.disabled = true;
      status.textContent = '';
      try {
        const resp = await fetch('/triage', { method: 'POST' });
        if (resp.status === 409) {
          status.textContent = 'A triage run is already in progress.';
        } else if (resp.ok) {
          status.textContent = 'Triage started!';
        } else {
          status.textContent = 'Error — check server logs.';
        }
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


def start_triage(source):
    """Start a triage run in a background thread.

    Returns False (and does nothing) if a run is already in progress.
    """
    if not _triage_lock.acquire(blocking=False):
        print(f"Triage requested ({source}) but a run is already in progress — skipping.")
        return False

    def worker():
        try:
            run_triage(auto_apply=True)
        except Exception:
            traceback.print_exc()
        finally:
            _triage_lock.release()

    print(f"Starting triage run ({source}) …")
    threading.Thread(target=worker, daemon=True, name=f"triage-{source}").start()
    return True


def next_scheduled_run(now):
    """First SCHEDULE_HOUR:SCHEDULE_MINUTE in SCHEDULE_TZ strictly after ``now``."""
    target = now.replace(
        hour=SCHEDULE_HOUR, minute=SCHEDULE_MINUTE, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=1)
    return target


def _scheduler_loop():
    while True:
        target = next_scheduled_run(datetime.now(SCHEDULE_TZ))
        print(f"Next scheduled triage: {target:%Y-%m-%d %H:%M %Z}")
        while True:
            # Epoch-based: subtracting same-tz aware datetimes would use naive
            # wall-clock values and miscount across DST transitions.
            remaining = target.timestamp() - time.time()
            if remaining <= 0:
                break
            # Sleep in bounded chunks and re-check, so clock adjustments
            # (NTP, DST) can't push the run far off target.
            time.sleep(min(remaining, 3600))
        start_triage("schedule")


def _start_scheduler():
    if os.getenv("TRIAGE_SCHEDULE_ENABLED", "1").lower() in ("0", "false", "no"):
        print("Triage schedule disabled (TRIAGE_SCHEDULE_ENABLED).")
        return
    threading.Thread(target=_scheduler_loop, daemon=True, name="triage-scheduler").start()


@app.get("/health")
def health():
    return Response("ok", mimetype="text/plain")


@app.get("/")
def index():
    return Response(_HTML, mimetype="text/html")


@app.post("/triage")
def triage():
    if start_triage("button"):
        return Response("started", mimetype="text/plain")
    return Response("already running", status=409, mimetype="text/plain")


# The scheduler lives in the web process; render.yaml runs gunicorn with a
# single worker so exactly one scheduler thread exists.
_start_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8765")))
