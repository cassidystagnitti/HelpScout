import sys
import os

# Keep the daily triage scheduler thread out of test runs (set before app import).
os.environ.setdefault("TRIAGE_SCHEDULE_ENABLED", "0")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
