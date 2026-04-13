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
SPAM_TAG = "spam"


SPAM_PROMPT_TEMPLATE = """\
You are an expert spam classifier for a meditation app's customer support help desk.

Below are support tickets. For EACH ticket, classify it as SPAM or NOT_SPAM.

Mark as **SPAM** if the ticket matches ANY of these patterns:

1. **Commercial solicitation** — Unsolicited sales pitches, SEO/marketing service offers, \
partnership or guest-post proposals, link-building requests, "business opportunity" emails, \
or cold outreach from agencies/freelancers.
2. **Phishing / scam** — Requests for passwords or credentials, suspicious links, fake \
account-security warnings, lottery/prize notifications, advance-fee fraud, crypto scams.
3. **Bulk automated junk** — Generic mass-mailed content that doesn't address a specific \
product issue: unsolicited newsletters, press releases, event invitations from unknown \
senders, automated directory-listing notices.
4. **Gibberish / test messages** — Random characters, Lorem Ipsum filler, keyboard mashing, \
or content with no discernible intent.
5. **Irrelevant product promotions** — Advertising for completely unrelated products or \
services (pharmaceuticals, gambling, adult content, forex/crypto trading platforms, etc.).
6. **Spoofed system messages** — Fake invoices, fake shipping notifications, fake payment \
confirmations, or other messages impersonating systems the company wouldn't actually use.
7. **Automated marketing replies** — Auto-generated "thank you for subscribing" or \
promotional follow-ups from external marketing tools the company didn't opt into.

Mark as **NOT_SPAM** (legitimate) even if the message is:
- A frustrated, rude, or angry customer — that's a real support request.
- A very short or vague question ("help", "how do I cancel?", "this doesn't work").
- An auto-reply or out-of-office from a real customer — noise, but not spam.
- A reply to an existing support conversation, even if terse.
- Feedback, bug reports, feature requests, or complaints, however poorly written.
- About billing, subscriptions, account access, app crashes, meditation content, \
notifications, or any topic plausibly related to a meditation app.
- In a language other than English — non-English support requests are legitimate.

**Be CONSERVATIVE**: when in doubt, classify as NOT_SPAM. Mislabeling a real customer \
as spam is far worse than letting a spam message through.

Here are the tickets to classify:

{tickets_text}

Respond with ONLY a JSON array, one object per ticket, in this exact format:
[
  {{"id": <ticket_id>, "classification": "SPAM" or "NOT_SPAM", "reason": "<brief one-line reason>"}}
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


def api_put(session, url, json_body):
    while True:
        resp = session.put(url, json=json_body)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited — waiting {retry_after}s …")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp


def get_first_mailbox_id(session):
    data = api_get(session, f"{BASE_URL}/mailboxes")
    mailboxes = data.get("_embedded", {}).get("mailboxes", [])
    if not mailboxes:
        sys.exit("Error: no mailboxes found in this Help Scout account.")
    mb = mailboxes[0]
    print(f"Using mailbox: \"{mb['name']}\" (ID {mb['id']})")
    return mb["id"]


def fetch_unassigned_conversations(session, mailbox_id):
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
        unassigned = [c for c in page_convos if not c.get("assignee")]
        conversations.extend(unassigned)

        total_pages = data.get("page", {}).get("totalPages", 1)
        print(f"  Page {page}/{total_pages} — {len(unassigned)} unassigned of {len(page_convos)}")

        if page >= total_pages:
            break
        page += 1

    return conversations


def get_conversation_text(session, conversation_id):
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

    customer_threads = [t for t in threads if t.get("type") == "customer"]
    if not customer_threads:
        return None

    first_thread = customer_threads[-1]
    body = first_thread.get("body", "")
    return strip_html(body) if body else None


def extract_tag_names(tags_field):
    """Normalize the tags list from a conversation object into plain strings."""
    names = []
    for t in tags_field or []:
        if isinstance(t, dict):
            names.append(t.get("tag", t.get("name", "")))
        else:
            names.append(str(t))
    return [n for n in names if n]


def classify_spam_batch(client, tickets):
    tickets_text = ""
    for t in tickets:
        body_preview = t["body"][:3000] if len(t["body"]) > 3000 else t["body"]
        tickets_text += (
            f"--- TICKET ID: {t['id']} ---\n"
            f"Subject: {t['subject']}\n"
            f"Body:\n{body_preview}\n"
            f"--- END TICKET ---\n\n"
        )

    prompt = SPAM_PROMPT_TEMPLATE.format(tickets_text=tickets_text)

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
        response_text = re.sub(r"\s*```$", "", response_text)

    return json.loads(response_text)


def add_spam_tag(session, conversation_id, existing_tags):
    tags = list(set(existing_tags + [SPAM_TAG]))
    api_put(session, f"{BASE_URL}/conversations/{conversation_id}/tags", {"tags": tags})


def set_spam_status(session, conversation_id):
    """Move a conversation to Help Scout's spam folder by changing its status."""
    url = f"{BASE_URL}/conversations/{conversation_id}"
    while True:
        resp = session.patch(url, json={"op": "replace", "path": "/status", "value": "spam"})
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited — waiting {retry_after}s …")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp


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

    conversations = fetch_unassigned_conversations(session, mailbox_id)
    print(f"\nFound {len(conversations)} unassigned conversations.\n")

    if not conversations:
        print("Nothing to process.")
        return

    tickets = []
    for i, convo in enumerate(conversations, 1):
        convo_id = convo["id"]
        subject = convo.get("subject", "(no subject)")
        existing_tags = extract_tag_names(convo.get("tags", []))

        if SPAM_TAG in existing_tags:
            print(f"[{i}/{len(conversations)}] #{convo_id} — already tagged spam, skipping.")
            continue

        print(f"[{i}/{len(conversations)}] Fetching #{convo_id}: {subject[:60]}")
        body = get_conversation_text(session, convo_id)

        tickets.append({
            "id": convo_id,
            "subject": subject,
            "body": body or "(empty)",
            "existing_tags": existing_tags,
        })

    if not tickets:
        print("\nNo tickets to classify (all already tagged or empty).")
        return

    print(f"\nClassifying {len(tickets)} tickets with Claude …\n")
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    spam_tickets = []
    for batch_start in range(0, len(tickets), BATCH_SIZE):
        batch = tickets[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(tickets) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"Batch {batch_num}/{total_batches} ({len(batch)} tickets) → Claude …")

        try:
            results = classify_spam_batch(claude, batch)
        except json.JSONDecodeError as e:
            print(f"  Failed to parse Claude response as JSON: {e}")
            print("  Skipping batch (no tickets tagged).")
            continue
        except Exception as e:
            print(f"  Claude API error: {e}")
            print("  Skipping batch.")
            continue

        result_map = {r["id"]: r for r in results}

        for ticket in batch:
            result = result_map.get(ticket["id"])
            if not result:
                print(f"  #{ticket['id']} — no result from Claude, skipping.")
                continue

            if result["classification"] == "SPAM":
                reason = result.get("reason", "")
                print(f"  🚫 #{ticket['id']} → SPAM — {reason}")
                spam_tickets.append(ticket)
            else:
                reason = result.get("reason", "")
                print(f"  ✓  #{ticket['id']} → OK — {reason}")

    if not spam_tickets:
        print(f"\nDone. No spam found in {len(tickets)} tickets.")
        return

    print(f"\n{'=' * 60}")
    print(f"  {len(spam_tickets)} of {len(tickets)} tickets identified as spam:")
    print(f"{'=' * 60}")
    for t in spam_tickets:
        print(f"  #{t['id']}  {t['subject'][:70]}")
    print(f"{'=' * 60}")

    answer = input(f"\nTag and move all {len(spam_tickets)} to spam? (y/n): ").strip().lower()
    if answer != "y":
        print("Aborted — no changes made.")
        return

    print()
    tagged = 0
    status_changed = 0
    for i, ticket in enumerate(spam_tickets, 1):
        print(f"[{i}/{len(spam_tickets)}] #{ticket['id']} — ", end="")
        try:
            add_spam_tag(session, ticket["id"], ticket["existing_tags"])
            tagged += 1
            print("tagged … ", end="")
        except requests.HTTPError as e:
            print(f"tag failed ({e}) … ", end="")

        try:
            set_spam_status(session, ticket["id"])
            status_changed += 1
            print("status → spam ✓")
        except requests.HTTPError as e:
            print(f"status change failed ({e})")

    print(f"\nDone. Tagged {tagged}, moved {status_changed} to spam (of {len(spam_tickets)}).")


if __name__ == "__main__":
    main()
