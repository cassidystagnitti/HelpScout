"""
HTTP server for Help Scout webhooks that triages new conversations.

Help Scout signs each request with HMAC-SHA1 over the raw body; see:
https://developer.helpscout.com/webhooks/

Setup:
1. Set HELPSCOUT_WEBHOOK_SECRET in .env (same value you configure in Help Scout for the webhook).
2. In Help Scout: Manage → Apps → Webhooks (or API), subscribe to ``convo.created``
   pointing to this server, e.g. https://your-host/webhook
3. Run::

    python scripts/triage_webhook_server.py

   Use a reverse proxy (HTTPS) in production. For local testing, use ngrok Cloudflared, etc.

Note: Help Scout allows one webhook configuration per company; include any other events
you need in that same app.
"""

from __future__ import annotations

import argparse
from typing import Optional
import base64
import hashlib
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from dotenv import load_dotenv


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT_DIR, ".env"))

HELPSCOUT_WEBHOOK_SECRET = os.getenv("HELPSCOUT_WEBHOOK_SECRET", "").strip()

# Events that mean “new ticket we should triage” (body is v2 Conversation).
TRIAGE_EVENTS = frozenset({
    "convo.created",
    "convo.ai-answers.created",
})


def _verify_help_scout_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    if not HELPSCOUT_WEBHOOK_SECRET:
        return False
    if not signature_header:
        return False
    expected = base64.b64encode(
        hmac.new(
            HELPSCOUT_WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha1,
        ).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected.strip(), signature_header.strip())


def _conversation_id_from_payload(event: str, payload: object) -> Optional[int]:
    if event not in TRIAGE_EVENTS:
        return None
    if not isinstance(payload, dict):
        return None
    cid = payload.get("id")
    if cid is None:
        return None
    try:
        return int(cid)
    except (TypeError, ValueError):
        return None


def _triage_in_background(conversation_id: int) -> None:
    # Import after env is loaded so triage_tickets sees the same .env
    import triage_tickets

    try:
        triage_tickets.run_triage(
            conversation_ids=[str(conversation_id)],
            auto_apply=True,
            skip_unassigned_scan=True,
        )
    except Exception as e:
        print(f"[triage_webhook] error triaging #{conversation_id}: {e}", file=sys.stderr)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "HelpScoutTriageWebhook/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(
            "%s - - [%s] %s"
            % (self.address_string(), self.log_date_time_string(), fmt % args)
        )

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path not in ("/", "/webhook"):
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length else b""

        sig = self.headers.get("X-HelpScout-Signature") or self.headers.get(
            "X-Helpscout-Signature"
        )
        if not _verify_help_scout_signature(raw_body, sig):
            self.send_response(401)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"invalid signature")
            return

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"invalid json")
            return

        event = (self.headers.get("X-HelpScout-Event") or "").strip()
        cid = _conversation_id_from_payload(event, payload)

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

        if cid is not None:
            print(f"[triage_webhook] {event} → scheduling triage for #{cid}")
            threading.Thread(
                target=_triage_in_background,
                args=(cid,),
                daemon=True,
            ).start()
        else:
            print(f"[triage_webhook] {event} — ignored (no triage for this event)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Help Scout webhook → triage server.")
    parser.add_argument(
        "--host",
        default=os.getenv("WEBHOOK_HOST", "0.0.0.0"),
        help="Bind address (default: 0.0.0.0 or WEBHOOK_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("WEBHOOK_PORT", "8765")),
        help="Listen port (default: 8765 or WEBHOOK_PORT).",
    )
    args = parser.parse_args()

    if not HELPSCOUT_WEBHOOK_SECRET:
        sys.exit(
            "Error: set HELPSCOUT_WEBHOOK_SECRET in .env (must match the secret in Help Scout webhook settings)."
        )

    httpd = ThreadingHTTPServer((args.host, args.port), WebhookHandler)
    print(f"Triage webhook listening on http://{args.host}:{args.port}/webhook")
    print("Subscribe Help Scout to POST convo.created (and optionally convo.ai-answers.created).")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
