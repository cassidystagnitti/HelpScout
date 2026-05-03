import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
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

BATCH_SIZE = 15
OUTPUT_FILE = os.path.join(ROOT_DIR, "suggested_tags.md")


TAG_SUGGESTION_PROMPT = """\
You are an expert at organizing customer support tickets for a meditation app (Happier Meditation).

Below are {ticket_count} support tickets from the last 2 days. Your job is to analyze ALL of \
them and come up with a set of descriptive **tags** that could be used to categorize these tickets \
going forward.

Guidelines:
- Tags should be short (1-3 words), lowercase, and use hyphens for spaces (e.g. "billing-issue", \
"app-crash", "cancel-subscription").
- Aim for 10-25 tags that cover the major themes across all tickets. Don't create overly niche \
tags that only apply to 1-2 tickets — those aren't useful for categorization.
- Every tag should apply to at least a few tickets.
- Think about what categories would be most useful for a support team to filter and prioritize work.
- Common categories for a meditation app might include things like billing, subscriptions, \
technical issues, content requests, account access, notifications, etc. — but let the actual \
ticket content drive your choices.

Here are the tickets:

{tickets_text}

Respond with ONLY a JSON object in this exact format:
{{
  "tags": [
    {{
      "name": "tag-name",
      "description": "One-sentence description of what this tag covers",
      "ticket_ids": [list of ticket IDs that match this tag],
      "count": <number of matching tickets>
    }}
  ],
  "summary": "A brief 2-3 sentence summary of the overall themes you see in these tickets"
}}

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


def fetch_conversations(session):
    two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
    since_str = two_days_ago.strftime("%Y-%m-%dT%H:%M:%SZ")

    conversations = []
    page = 1

    while True:
        print(f"Fetching conversations page {page} …")
        data = api_get(session, f"{BASE_URL}/conversations", params={
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


def get_customer_text(session, conversation_id):
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


def suggest_tags_batch(client, tickets):
    tickets_text = ""
    for t in tickets:
        body_preview = t["body"][:2000] if len(t["body"]) > 2000 else t["body"]
        tickets_text += (
            f"--- TICKET ID: {t['id']} ---\n"
            f"Subject: {t['subject']}\n"
            f"Body:\n{body_preview}\n"
            f"--- END TICKET ---\n\n"
        )

    prompt = TAG_SUGGESTION_PROMPT.format(
        ticket_count=len(tickets),
        tickets_text=tickets_text,
    )

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
        response_text = re.sub(r"\s*```$", "", response_text)

    return json.loads(response_text)


MERGE_PROMPT = """\
You are merging tag suggestions from multiple batches of support tickets for a meditation app.

Below are tag suggestion results from {batch_count} separate batches. Merge them into a single \
coherent set of tags. Combine duplicates or near-duplicates (e.g. "billing-issue" and \
"billing-problem" should become one tag). Keep the best name for each merged tag.

{batch_results}

Respond with ONLY a JSON object in this exact format:
{{
  "tags": [
    {{
      "name": "tag-name",
      "description": "One-sentence description of what this tag covers",
      "ticket_ids": [list of ALL ticket IDs that match this tag, merged from all batches],
      "count": <number of matching tickets>
    }}
  ],
  "summary": "A brief 2-3 sentence summary of the overall themes across all tickets"
}}

Sort tags by count (highest first). Return valid JSON only — no markdown fences, no text before or after."""


def merge_batch_results(client, batch_results):
    batch_text = ""
    for i, result in enumerate(batch_results, 1):
        batch_text += f"=== BATCH {i} ===\n{json.dumps(result, indent=2)}\n\n"

    prompt = MERGE_PROMPT.format(
        batch_count=len(batch_results),
        batch_results=batch_text,
    )

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = re.sub(r"^```(?:json)?\s*", "", response_text)
        response_text = re.sub(r"\s*```$", "", response_text)

    return json.loads(response_text)


def write_report(result, total_tickets):
    lines = [
        f"# Suggested Tags for Help Scout Tickets",
        f"",
        f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} — "
        f"based on {total_tickets} tickets from the last 2 days*",
        f"",
        f"## Summary",
        f"",
        result.get("summary", ""),
        f"",
        f"## Suggested Tags",
        f"",
        f"| Tag | Description | # Tickets |",
        f"|-----|-------------|-----------|",
    ]

    for tag in result.get("tags", []):
        name = tag["name"]
        desc = tag.get("description", "")
        count = tag.get("count", len(tag.get("ticket_ids", [])))
        lines.append(f"| `{name}` | {desc} | {count} |")

    lines.append("")
    lines.append("## Tag Details")
    lines.append("")

    for tag in result.get("tags", []):
        name = tag["name"]
        ticket_ids = tag.get("ticket_ids", [])
        lines.append(f"### `{name}` ({len(ticket_ids)} tickets)")
        lines.append(f"")
        lines.append(f"{tag.get('description', '')}")
        lines.append(f"")
        lines.append(f"Ticket IDs: {', '.join(str(tid) for tid in ticket_ids)}")
        lines.append(f"")

    report = "\n".join(lines)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(report)

    return report


def main():
    if not APP_ID or not APP_SECRET:
        sys.exit("Error: set HELPSCOUT_APP_ID and HELPSCOUT_APP_SECRET in your .env file.")
    if not ANTHROPIC_API_KEY:
        sys.exit("Error: set ANTHROPIC_API_KEY in your .env file.")

    print("Authenticating with Help Scout …")
    token = get_access_token()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    conversations = fetch_conversations(session)
    print(f"\nFound {len(conversations)} conversations from the last 2 days.\n")

    if not conversations:
        print("No conversations found. Nothing to do.")
        return

    tickets = []
    for i, convo in enumerate(conversations, 1):
        convo_id = convo["id"]
        subject = convo.get("subject", "(no subject)")

        print(f"[{i}/{len(conversations)}] Fetching #{convo_id}: {subject[:60]}")
        body = get_customer_text(session, convo_id)

        if body:
            tickets.append({
                "id": convo_id,
                "subject": subject,
                "body": body,
            })
        else:
            print(f"  Skipped — no customer message found.")

    if not tickets:
        print("\nNo tickets with customer messages found.")
        return

    print(f"\nAnalyzing {len(tickets)} tickets with Claude …\n")
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if len(tickets) <= BATCH_SIZE:
        print(f"Sending all {len(tickets)} tickets to Claude …")
        try:
            result = suggest_tags_batch(claude, tickets)
        except json.JSONDecodeError as e:
            sys.exit(f"Failed to parse Claude response as JSON: {e}")
        except Exception as e:
            sys.exit(f"Claude API error: {e}")
    else:
        batch_results = []
        for batch_start in range(0, len(tickets), BATCH_SIZE):
            batch = tickets[batch_start:batch_start + BATCH_SIZE]
            batch_num = batch_start // BATCH_SIZE + 1
            total_batches = (len(tickets) + BATCH_SIZE - 1) // BATCH_SIZE
            print(f"Batch {batch_num}/{total_batches} ({len(batch)} tickets) → Claude …")

            try:
                batch_result = suggest_tags_batch(claude, batch)
                batch_results.append(batch_result)
            except json.JSONDecodeError as e:
                print(f"  Failed to parse response for batch {batch_num}: {e}")
                continue
            except Exception as e:
                print(f"  Claude API error on batch {batch_num}: {e}")
                continue

        if not batch_results:
            sys.exit("All batches failed. No tags to suggest.")

        if len(batch_results) == 1:
            result = batch_results[0]
        else:
            print(f"\nMerging results from {len(batch_results)} batches …")
            try:
                result = merge_batch_results(claude, batch_results)
            except json.JSONDecodeError as e:
                sys.exit(f"Failed to parse merge response: {e}")
            except Exception as e:
                sys.exit(f"Claude API error during merge: {e}")

    report = write_report(result, len(tickets))

    print(f"\n{'=' * 60}")
    print(report)
    print(f"{'=' * 60}")
    print(f"\nReport saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
