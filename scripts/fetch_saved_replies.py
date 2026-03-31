import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT_DIR, ".env"))

BASE_URL = "https://api.helpscout.net/v2"
TOKEN_URL = f"{BASE_URL}/oauth2/token"

APP_ID = os.getenv("HELPSCOUT_APP_ID")
APP_SECRET = os.getenv("HELPSCOUT_APP_SECRET")
DB_FILE = os.path.join(ROOT_DIR, "saved_replies.db")


def get_access_token():
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


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


def fetch_mailboxes(session):
    mailboxes = []
    page = 1

    while True:
        print(f"Fetching mailboxes page {page} …")
        data = api_get(session, f"{BASE_URL}/mailboxes", params={"page": page})

        page_mailboxes = data.get("_embedded", {}).get("mailboxes", [])
        mailboxes.extend(page_mailboxes)

        total_pages = data.get("page", {}).get("totalPages", 1)
        print(f"  Got {len(page_mailboxes)} mailboxes (page {page}/{total_pages})")

        if page >= total_pages:
            break
        page += 1

    return mailboxes


def fetch_saved_reply_list(session, mailbox_id):
    """Fetch the list of saved replies (id + name + preview) for a mailbox."""
    data = api_get(
        session,
        f"{BASE_URL}/mailboxes/{mailbox_id}/saved-replies",
        params={"includeChatReplies": "true"},
    )
    # This endpoint returns a flat JSON array, not paginated _embedded.
    if isinstance(data, list):
        return data
    return data.get("_embedded", {}).get("savedReplies", data if isinstance(data, list) else [])


def fetch_saved_reply_detail(session, mailbox_id, reply_id):
    """Fetch the full text of a single saved reply."""
    return api_get(session, f"{BASE_URL}/mailboxes/{mailbox_id}/saved-replies/{reply_id}")


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS saved_replies (
            id            INTEGER PRIMARY KEY,
            mailbox_id    INTEGER NOT NULL,
            mailbox_name  TEXT    NOT NULL,
            name          TEXT    NOT NULL,
            text          TEXT,
            chat_text     TEXT,
            fetched_at    TEXT    NOT NULL
        )
    """)
    conn.commit()
    return conn


def main():
    if not APP_ID or not APP_SECRET:
        sys.exit("Error: set HELPSCOUT_APP_ID and HELPSCOUT_APP_SECRET in your .env file.")

    print("Authenticating …")
    token = get_access_token()

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    mailboxes = fetch_mailboxes(session)
    print(f"\nFound {len(mailboxes)} mailbox(es).\n")

    conn = init_db(DB_FILE)
    now = datetime.now(timezone.utc).isoformat()
    total_saved = 0

    # Clear previous data so the DB always reflects the current state.
    conn.execute("DELETE FROM saved_replies")
    conn.commit()

    for mb in mailboxes:
        mb_id = mb["id"]
        mb_name = mb.get("name", "Unknown")
        print(f"Mailbox: {mb_name} (id={mb_id})")

        reply_list = fetch_saved_reply_list(session, mb_id)
        print(f"  {len(reply_list)} saved replies found")

        for i, stub in enumerate(reply_list, 1):
            reply_id = stub["id"]
            print(f"  [{i}/{len(reply_list)}] Fetching saved reply {reply_id} ({stub.get('name', '')}) …")

            detail = fetch_saved_reply_detail(session, mb_id, reply_id)

            conn.execute(
                """
                INSERT OR REPLACE INTO saved_replies
                    (id, mailbox_id, mailbox_name, name, text, chat_text, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    detail["id"],
                    mb_id,
                    mb_name,
                    detail.get("name", ""),
                    detail.get("text"),
                    detail.get("chatText"),
                    now,
                ),
            )
            total_saved += 1

        conn.commit()

    conn.close()
    print(f"\nDone — saved {total_saved} replies to {DB_FILE}")


if __name__ == "__main__":
    main()
