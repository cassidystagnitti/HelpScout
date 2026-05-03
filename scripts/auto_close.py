import json
import os
import re
import sys
import time
from html import unescape

import anthropic
import requests
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT_DIR, ".env"))

BASE_URL = "https://api.helpscout.net/v2"
TOKEN_URL = f"{BASE_URL}/oauth2/token"

APP_ID = os.getenv("HELPSCOUT_APP_ID")
APP_SECRET = os.getenv("HELPSCOUT_APP_SECRET")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

BATCH_SIZE = 20


REVIEW_PROMPT = """\
You are reviewing customer support tickets for a meditation app (Happier Meditation).

For EACH ticket below, you will see the full conversation history (all messages in order).
Your job is to determine whether the ticket still needs a response from the support team,
or whether it is effectively done and can be closed.

## Classification rules

Mark as **NO_RESPONSE_NEEDED** if:
- The last customer reply is a simple "thank you", "thanks!", "got it", "that worked", \
or similar acknowledgement that does not ask a new question or raise a new issue.
- The conversation has clearly been resolved and the customer confirmed satisfaction.
- The last message is an automated "out of office" or delivery-status notification \
with no actionable content.

Mark as **NEEDS_RESPONSE** if:
- The customer is asking a question, reporting a problem, or requesting something.
- There is ANY ambiguity about whether the customer still needs help.
- The last customer message contains new information or a follow-up issue.
- The conversation looks unresolved or the customer sounds unsatisfied.

**ERR ON THE SIDE OF CAUTION.** If there is any doubt at all, mark it as NEEDS_RESPONSE. \
It is far better to leave a resolved ticket open than to close one that still needs attention.

Here are the tickets to review:

{tickets_text}

Respond with ONLY a JSON array, one object per ticket:
[
  {{"id": <ticket_id>, "classification": "NO_RESPONSE_NEEDED" or "NEEDS_RESPONSE", \
"reason": "<brief one-line explanation>"}}
]

Return valid JSON only — no markdown fences, no text before or after."""


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
    while True:
        resp = session.get(url, params=params)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited — waiting {retry_after}s …")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp.json()


def get_first_mailbox_id(session):
    data = api_get(session, f"{BASE_URL}/mailboxes")
    mailboxes = data.get("_embedded", {}).get("mailboxes", [])
    if not mailboxes:
        sys.exit("Error: no mailboxes found in this Help Scout account.")
    mb = mailboxes[0]
    print(f"Using mailbox: \"{mb['name']}\" (ID {mb['id']})")
    return mb["id"]


def fetch_active_conversations(session, mailbox_id):
    """Fetch all active conversations from the mailbox."""
    conversations = []
    page = 1

    while True:
        print(f"Fetching conversations page {page} …")
        data = api_get(session, f"{BASE_URL}/conversations", params={
            "mailbox": mailbox_id,
            "status": "active",
            "sortField": "createdAt",
            "sortOrder": "desc",
            "page": page,
        })

        page_convos = data.get("_embedded", {}).get("conversations", [])
        conversations.extend(page_convos)

        total_pages = data.get("page", {}).get("totalPages", 1)
        print(f"  Page {page}/{total_pages} — {len(page_convos)} conversations")

        if page >= total_pages:
            break
        page += 1

    return conversations


def get_full_thread(session, conversation_id):
    """Fetch all threads for a conversation and return them in chronological order."""
    threads = []
    page = 1

    while True:
        data = api_get(
            session,
            f"{BASE_URL}/conversations/{conversation_id}/threads",
            params={"page": page},
        )
        page_threads = data.get("_embedded", {}).get("threads", [])
        threads.extend(page_threads)

        total_pages = data.get("page", {}).get("totalPages", 1)
        if page >= total_pages:
            break
        page += 1

    visible_types = {"customer", "message", "reply", "note"}
    relevant = [t for t in threads if t.get("type") in visible_types]
    relevant.sort(key=lambda t: t.get("createdAt", ""))

    messages = []
    for t in relevant:
        body = t.get("body", "")
        plain = strip_html(body) if body else ""
        if not plain:
            continue

        author_type = t.get("type", "unknown")
        if author_type == "customer":
            role = "CUSTOMER"
        elif author_type == "note":
            role = "AGENT NOTE"
        else:
            role = "AGENT"

        created = t.get("createdAt", "")
        messages.append(f"[{role}] ({created})\n{plain}")

    return "\n\n".join(messages) if messages else None


def close_conversation(session, conversation_id):
    """Close a conversation by setting its status to 'closed'."""
    url = f"{BASE_URL}/conversations/{conversation_id}"
    while True:
        resp = session.patch(url, json={
            "op": "replace",
            "path": "/status",
            "value": "closed",
        })
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited — waiting {retry_after}s …")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp


def review_batch(client, tickets, max_retries=5):
    tickets_text = ""
    for t in tickets:
        thread_preview = t["thread"][:5000] if len(t["thread"]) > 5000 else t["thread"]
        tickets_text += (
            f"--- TICKET ID: {t['id']} ---\n"
            f"Subject: {t['subject']}\n"
            f"Conversation:\n{thread_preview}\n"
            f"--- END TICKET ---\n\n"
        )

    prompt = REVIEW_PROMPT.format(tickets_text=tickets_text)

    for attempt in range(max_retries):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            break
        except anthropic.RateLimitError as e:
            retry_after = getattr(e.response.headers, "retry-after", None)
            wait = int(retry_after) if retry_after else min(60, 15 * (attempt + 1))
            print(f"  Anthropic rate limited — waiting {wait}s (attempt {attempt + 1}/{max_retries}) …")
            time.sleep(wait)
    else:
        raise RuntimeError("Exhausted retries on Anthropic rate limit")

    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
        response_text = re.sub(r"\s*```$", "", response_text)

    return json.loads(response_text)


def main():
    if not APP_ID or not APP_SECRET:
        sys.exit("Error: set HELPSCOUT_APP_ID and HELPSCOUT_APP_SECRET in your .env file.")
    if not ANTHROPIC_API_KEY:
        sys.exit("Error: set ANTHROPIC_API_KEY in your .env file.")

    print("Authenticating with Help Scout …")
    token = get_access_token()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    mailbox_id = get_first_mailbox_id(session)

    conversations = fetch_active_conversations(session, mailbox_id)
    print(f"\nFound {len(conversations)} active conversations.\n")

    if not conversations:
        print("Nothing to process.")
        return

    tickets = []
    for i, convo in enumerate(conversations, 1):
        convo_id = convo["id"]
        subject = convo.get("subject", "(no subject)")

        print(f"[{i}/{len(conversations)}] Fetching #{convo_id}: {subject[:60]}")
        thread = get_full_thread(session, convo_id)
        if not thread:
            print(f"  → no visible messages, skipping.")
            continue

        tickets.append({
            "id": convo_id,
            "subject": subject,
            "thread": thread,
        })

    if not tickets:
        print("\nNo tickets to review.")
        return

    print(f"\nReviewing {len(tickets)} tickets with Claude …\n")
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    closeable = []
    needs_response = []

    for batch_start in range(0, len(tickets), BATCH_SIZE):
        batch = tickets[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(tickets) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"Batch {batch_num}/{total_batches} ({len(batch)} tickets) → Claude …")

        try:
            results = review_batch(claude, batch)
        except json.JSONDecodeError as e:
            print(f"  Failed to parse Claude response as JSON: {e}")
            print("  Skipping batch (no tickets will be closed).")
            continue
        except Exception as e:
            print(f"  Claude API error: {e}")
            print("  Skipping batch.")
            continue

        result_map = {int(r["id"]): r for r in results}

        for ticket in batch:
            result = result_map.get(int(ticket["id"]))
            if not result:
                print(f"  #{ticket['id']} — no result from Claude, skipping.")
                continue

            reason = result.get("reason", "")
            if result["classification"] == "NO_RESPONSE_NEEDED":
                print(f"  ✓ #{ticket['id']} → CLOSE — {reason}")
                closeable.append(ticket)
            else:
                print(f"  … #{ticket['id']} → KEEP OPEN — {reason}")
                needs_response.append(ticket)

    print(f"\n{'=' * 70}")
    print(f"  Review complete: {len(closeable)} to close, "
          f"{len(needs_response)} need response")
    print(f"{'=' * 70}")

    if closeable:
        print(f"\n  Tickets to close:")
        for t in closeable:
            print(f"    #{t['id']}  {t['subject'][:65]}")

    if needs_response:
        print(f"\n  Tickets that need a response (no action taken):")
        for t in needs_response:
            print(f"    #{t['id']}  {t['subject'][:65]}")

    print(f"{'=' * 70}")

    if not closeable:
        print("\nNo tickets to close. Done.")
        return

    answer = input(
        f"\nClose {len(closeable)} tickets in Help Scout? (y/n): "
    ).strip().lower()
    if answer != "y":
        print("Aborted — no changes made.")
        return

    print()
    closed_count = 0
    for i, ticket in enumerate(closeable, 1):
        print(f"[{i}/{len(closeable)}] #{ticket['id']} — ", end="")
        try:
            close_conversation(session, ticket["id"])
            closed_count += 1
            print("closed ✓")
        except requests.HTTPError as e:
            print(f"failed ({e})")

    print(f"\nDone. Closed {closed_count} of {len(closeable)} tickets.")


if __name__ == "__main__":
    main()
