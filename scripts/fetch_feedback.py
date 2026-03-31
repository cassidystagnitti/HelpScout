import csv
import os
import sys
import time
import re
from datetime import datetime, timedelta, timezone
from html import unescape

import requests
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT_DIR, ".env"))

BASE_URL = "https://api.helpscout.net/v2"
TOKEN_URL = f"{BASE_URL}/oauth2/token"

APP_ID = os.getenv("HELPSCOUT_APP_ID")
APP_SECRET = os.getenv("HELPSCOUT_APP_SECRET")
OUTPUT_FILE = os.path.join(ROOT_DIR, "feedback.csv")


def get_access_token():
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def strip_html(html):
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def api_get(session, url, params=None):
    """GET with automatic retry on 429 rate-limit responses."""
    while True:
        resp = session.get(url, params=params)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited — waiting {retry_after}s …")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp.json()


def fetch_conversations(session):
    four_months_ago = datetime.now(timezone.utc) - timedelta(days=120)
    since_str = four_months_ago.strftime("%Y-%m-%dT%H:%M:%SZ")

    conversations = []
    page = 1

    while True:
        print(f"Fetching conversations page {page} …")
        data = api_get(session, f"{BASE_URL}/conversations", params={
            "tag": "feedback",
            "status": "all",
            "query": f'(createdAt:[{since_str} TO *])',
            "sortField": "createdAt",
            "sortOrder": "desc",
            "page": page,
        })

        page_convos = data.get("_embedded", {}).get("conversations", [])
        conversations.extend(page_convos)

        total_pages = data.get("page", {}).get("totalPages", 1)
        print(f"  Got {len(page_convos)} conversations (page {page}/{total_pages})")

        if page >= total_pages:
            break
        page += 1

    return conversations


def get_initial_customer_text(session, conversation_id):
    """Return the body of the oldest customer-type thread in a conversation."""
    threads = []
    page = 1

    while True:
        data = api_get(session, f"{BASE_URL}/conversations/{conversation_id}/threads", params={
            "page": page,
        })

        page_threads = data.get("_embedded", {}).get("threads", [])
        threads.extend(page_threads)

        total_pages = data.get("page", {}).get("totalPages", 1)
        if page >= total_pages:
            break
        page += 1

    customer_threads = [t for t in threads if t.get("type") == "customer"]
    if not customer_threads:
        return None

    # Threads come newest-first; the initial customer message is the last one.
    first_thread = customer_threads[-1]
    body = first_thread.get("body", "")
    return strip_html(body) if body else None


def main():
    if not APP_ID or not APP_SECRET:
        sys.exit("Error: set HELPSCOUT_APP_ID and HELPSCOUT_APP_SECRET in your .env file.")

    print("Authenticating …")
    token = get_access_token()

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    conversations = fetch_conversations(session)
    print(f"\nFound {len(conversations)} conversations with tag 'feedback' in the last 4 months.\n")

    rows = []
    for i, convo in enumerate(conversations, 1):
        convo_id = convo["id"]
        print(f"[{i}/{len(conversations)}] Fetching threads for conversation {convo_id} …")
        text = get_initial_customer_text(session, convo_id)
        if text:
            rows.append({"text": text, "id": convo_id})
        else:
            print(f"  Skipped — no customer thread found.")

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["text", "id"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nDone — wrote {len(rows)} rows to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
