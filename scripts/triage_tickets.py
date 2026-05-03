import argparse
import csv
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

TAGS_CSV = os.path.join(ROOT_DIR, "tags.csv")
TEAMS_CSV = os.path.join(ROOT_DIR, "teams.csv")
CUSTOM_FIELDS_CSV = os.path.join(ROOT_DIR, "custom_fields.csv")
BATCH_SIZE = 20


TRIAGE_PROMPT = """\
You are an expert at triaging customer support tickets for a meditation app (Happier Meditation).

For EACH ticket below, you must:
1. Assign one or more **tags** from the allowed list.
2. Assign exactly one **team** from the allowed list.
3. Assign a **priority** (P1, P2, or P3).
4. Assign a **tier** (T1, T2, or T3).

## Allowed Tags
{tags_list}

## Allowed Teams
{teams_list}

## Guidelines

**Tagging:**
- Only use tags from the allowed list above — never invent new ones.
- Assign every tag that genuinely applies. Most tickets will have 1 tags, but some may have 2 or 3.
- "spam" should only be used for clear junk/solicitation, not real customer messages.
- Prefer specific tags over generic ones when both apply (e.g. prefer "cancel subscription" \
over just "subscription" if the customer wants to cancel).
- Add "maven_draft" tag to any ticket that does not need to have action taken by a support agent. Add it to all tickets that are purely informational. This is for any ticket that can be answered purely from documentation.
- Add "technical" to any ticket that requires action. This is any ticket that does not have "maven_draft" applied to it. 

**Team assignment:**
- Pick the single best team for each ticket based on the primary intent.
- "Account Management" — account access, login issues, password resets, profile changes.
- "Cancel Refund" — cancellation requests, refund requests, charge disputes.
- "Subscription Get" — new subscription inquiries, pricing questions, trial conversions, gifting, need based comps.
- "Subscription Use" — help using an active subscription, feature access, plan benefits, login issues.
- "Tech Support" — bugs, crashes, technical errors, troubleshooting.
- "Feedback" — general feedback, feature requests, content requests, compliments.
- "Challenge" — questions about meditation challenges or programs.
- "Engagement" — app navigation, content suggestions, re-engagement, usage questions, onboarding help, everything related to or mentioning "podcasts" or "Dan Harris".
- "Social" — social media mentions, Instagram, Messenger inquiries.
- "Other" — anything that doesn't fit the above categories.

**Priority (urgency):**
- P1 — Urgent: account locked out, payment charged incorrectly, app completely broken, \
angry/escalated customer, potential PR issue.
- P2 — Normal: standard support requests, subscription questions, feature access issues, \
cancellation/refund requests.
- P3 — Low: general feedback, feature requests, content suggestions, non-urgent questions.

**Tier (complexity):**
- T1 — Simple: quick answer, password reset, basic how-to, clear-cut cancellation.
- T2 — Moderate: requires investigation, multi-step troubleshooting, nuanced subscription issues.
- T3 — Complex: escalation needed, cross-team coordination, edge-case bugs, sensitive situations.

NOTE: Cancellation Requests are T1 P1 always.

Here are the tickets to triage:

{tickets_text}

Respond with ONLY a JSON array, one object per ticket:
[
  {{
    "id": <ticket_id>,
    "tags": ["tag1", "tag2"],
    "team": "Team Name",
    "priority": "P1",
    "tier": "T1",
    "reason": "<brief one-line explanation>"
  }}
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


def api_patch(session, url, json_body):
    while True:
        resp = session.patch(url, json=json_body)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited — waiting {retry_after}s …")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp


def load_tags():
    tags = {}
    with open(TAGS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tags[row["name"].strip()] = int(row["id"])
    return tags


def load_teams():
    teams = {}
    with open(TEAMS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row["name"].strip()
            if name:
                teams[name] = int(row["id"])
    return teams


def load_custom_fields():
    """Return dict: {field_name: {"id": int, "options": {label: option_id}}}"""
    fields = {}
    with open(CUSTOM_FIELDS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row["name"].strip()
            field_id = int(row["id"])
            options = {}
            if row.get("options"):
                for pair in row["options"].split("; "):
                    label, opt_id = pair.rsplit(":", 1)
                    options[label.strip()] = int(opt_id)
            fields[name] = {"id": field_id, "options": options}
    return fields


def get_first_mailbox_id(session):
    data = api_get(session, f"{BASE_URL}/mailboxes")
    mailboxes = data.get("_embedded", {}).get("mailboxes", [])
    if not mailboxes:
        sys.exit("Error: no mailboxes found in this Help Scout account.")
    mb = mailboxes[0]
    print(f"Using mailbox: \"{mb['name']}\" (ID {mb['id']})")
    return mb["id"]


def fetch_conversation(session, conversation_id):
    return api_get(session, f"{BASE_URL}/conversations/{conversation_id}")


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


def conversation_to_ticket(session, convo, log_prefix=""):
    """Build a ticket dict from a conversation API object; return None if skipped."""
    convo_id = convo["id"]
    subject = convo.get("subject", "(no subject)")
    existing_tags = extract_tag_names(convo.get("tags", []))
    if convo.get("assignee"):
        print(f"{log_prefix}#{convo_id}: already assigned — skipping.")
        return None
    body = get_conversation_text(session, convo_id)
    return {
        "id": convo_id,
        "subject": subject,
        "body": body or "(empty)",
        "existing_tags": existing_tags,
    }


def build_tickets_for_ids(session, conversation_ids):
    tickets = []
    for cid in conversation_ids:
        try:
            convo = fetch_conversation(session, cid)
        except requests.HTTPError as e:
            print(f"#{cid}: failed to fetch conversation ({e})")
            continue
        t = conversation_to_ticket(session, convo)
        if t:
            tickets.append(t)
    return tickets


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
    names = []
    for t in tags_field or []:
        if isinstance(t, dict):
            names.append(t.get("tag", t.get("name", "")))
        else:
            names.append(str(t))
    return [n for n in names if n]


def triage_batch(client, tickets, tag_names, team_names):
    tickets_text = ""
    for t in tickets:
        body_preview = t["body"][:3000] if len(t["body"]) > 3000 else t["body"]
        tickets_text += (
            f"--- TICKET ID: {t['id']} ---\n"
            f"Subject: {t['subject']}\n"
            f"Body:\n{body_preview}\n"
            f"--- END TICKET ---\n\n"
        )

    tags_list = "\n".join(f"- {name}" for name in sorted(tag_names))
    teams_list = "\n".join(f"- {name}" for name in sorted(team_names))

    prompt = TRIAGE_PROMPT.format(
        tags_list=tags_list,
        teams_list=teams_list,
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


def apply_tags(session, conversation_id, existing_tags, new_tags):
    merged = list(set(existing_tags + new_tags))
    api_put(
        session,
        f"{BASE_URL}/conversations/{conversation_id}/tags",
        {"tags": merged},
    )


def assign_team(session, conversation_id, team_id):
    api_patch(
        session,
        f"{BASE_URL}/conversations/{conversation_id}",
        {"op": "replace", "path": "/assignTo", "value": team_id},
    )


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


def set_custom_fields(session, conversation_id, fields_payload):
    """PUT the full list of custom field values onto a conversation."""
    while True:
        resp = session.put(
            f"{BASE_URL}/conversations/{conversation_id}/fields",
            json={"fields": fields_payload},
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            print(f"  Rate limited — waiting {retry_after}s …")
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp


def run_triage(
    *,
    conversation_ids=None,
    auto_apply=False,
    skip_unassigned_scan=False,
):
    """Run triage: either all unassigned in the first mailbox, or specific conversation IDs.

    If ``skip_unassigned_scan`` is True and ``conversation_ids`` is empty after fetches
    (e.g. all already assigned), return quietly (for webhook).
    """
    if not APP_ID or not APP_SECRET:
        sys.exit("Error: set HELPSCOUT_APP_ID and HELPSCOUT_APP_SECRET in your .env file.")
    if not ANTHROPIC_API_KEY:
        sys.exit("Error: set ANTHROPIC_API_KEY in your .env file.")

    print("Loading tags, teams, and custom fields …")
    valid_tags = load_tags()
    valid_teams = load_teams()
    custom_fields = load_custom_fields()
    priority_field = custom_fields.get("Priority - Urgency")
    tier_field = custom_fields.get("Tier - Complexity")
    print(f"  {len(valid_tags)} tags, {len(valid_teams)} teams, {len(custom_fields)} custom fields loaded.\n")

    print("Authenticating with Help Scout …")
    token = get_access_token()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    if conversation_ids:
        ids = [int(x) for x in conversation_ids]
        print(f"Triage mode: {len(ids)} conversation ID(s) specified.\n")
        tickets = build_tickets_for_ids(session, ids)
    else:
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
            print(f"[{i}/{len(conversations)}] Fetching #{convo_id}: {subject[:60]}")
            t = conversation_to_ticket(session, convo)
            if t:
                tickets.append(t)

    if not tickets:
        if skip_unassigned_scan:
            return
        print("\nNo tickets to triage.")
        return

    print(f"\nTriaging {len(tickets)} tickets with Claude …\n")
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    team_lookup = {name.lower(): (name, tid) for name, tid in valid_teams.items()}

    all_results = []
    for batch_start in range(0, len(tickets), BATCH_SIZE):
        batch = tickets[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(tickets) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"Batch {batch_num}/{total_batches} ({len(batch)} tickets) → Claude …")

        try:
            results = triage_batch(claude, batch, valid_tags.keys(), valid_teams.keys())
        except json.JSONDecodeError as e:
            print(f"  Failed to parse Claude response: {e}")
            print("  Skipping batch.")
            continue
        except Exception as e:
            print(f"  Claude API error: {e}")
            print("  Skipping batch.")
            continue

        result_map = {int(r["id"]): r for r in results}
        for ticket in batch:
            result = result_map.get(ticket["id"])
            if not result:
                print(f"  #{ticket['id']} — no result from Claude, skipping.")
                continue

            raw_tags = result.get("tags", [])
            filtered_tags = [t for t in raw_tags if t in valid_tags]

            raw_team = result.get("team", "")
            team_match = team_lookup.get(raw_team.lower())
            team_name = team_match[0] if team_match else None
            team_id = team_match[1] if team_match else None

            raw_priority = result.get("priority", "")
            priority_option_id = (
                priority_field["options"].get(raw_priority)
                if priority_field else None
            )
            raw_tier = result.get("tier", "")
            tier_option_id = (
                tier_field["options"].get(raw_tier)
                if tier_field else None
            )

            reason = result.get("reason", "")
            tag_str = ", ".join(filtered_tags) if filtered_tags else "(none)"
            team_str = team_name or f"? ({raw_team})"
            pri_str = raw_priority if priority_option_id else f"? ({raw_priority})"
            tier_str = raw_tier if tier_option_id else f"? ({raw_tier})"
            print(f"  #{ticket['id']} → tags: [{tag_str}]  team: {team_str}"
                  f"  priority: {pri_str}  tier: {tier_str}  — {reason}")

            all_results.append({
                **ticket,
                "new_tags": filtered_tags,
                "team_name": team_name,
                "team_id": team_id,
                "priority": raw_priority,
                "priority_option_id": priority_option_id,
                "tier": raw_tier,
                "tier_option_id": tier_option_id,
                "reason": reason,
            })

    if not all_results:
        print("\nNo triage results. Nothing to apply.")
        return

    print(f"\n{'=' * 70}")
    print(f"  Triage plan for {len(all_results)} tickets:")
    print(f"{'=' * 70}")
    for r in all_results:
        tag_str = ", ".join(r["new_tags"]) if r["new_tags"] else "(none)"
        is_spam = "spam" in r["new_tags"]
        team_str = "SPAM" if is_spam else (r["team_name"] or "unknown")
        pri_str = r["priority"] if r["priority_option_id"] else "?"
        tier_str = r["tier"] if r["tier_option_id"] else "?"
        print(f"  #{r['id']}  {r['subject'][:50]}")
        print(f"          tags: [{tag_str}]  →  team: {team_str}  |  {pri_str} / {tier_str}")
    print(f"{'=' * 70}")

    if not auto_apply:
        answer = input(
            f"\nApply tags, teams, and custom fields for all {len(all_results)} tickets? (y/n): "
        ).strip().lower()
        if answer != "y":
            print("Aborted — no changes made.")
            return

    print()
    tagged_count = 0
    assigned_count = 0
    spam_count = 0
    fields_count = 0
    for i, r in enumerate(all_results, 1):
        is_spam = "spam" in r["new_tags"]
        print(f"[{i}/{len(all_results)}] #{r['id']} — ", end="")

        if r["new_tags"]:
            try:
                apply_tags(session, r["id"], r["existing_tags"], r["new_tags"])
                tagged_count += 1
                print(f"tagged [{', '.join(r['new_tags'])}] … ", end="")
            except requests.HTTPError as e:
                print(f"tag failed ({e}) … ", end="")
        else:
            print("no new tags … ", end="")

        if is_spam:
            try:
                set_spam_status(session, r["id"])
                spam_count += 1
                print("status → spam … ", end="")
            except requests.HTTPError as e:
                print(f"spam status failed ({e}) … ", end="")
        elif r["team_id"]:
            try:
                assign_team(session, r["id"], r["team_id"])
                assigned_count += 1
                print(f"→ {r['team_name']} … ", end="")
            except requests.HTTPError as e:
                print(f"assign failed ({e}) … ", end="")
        else:
            print("no team match … ", end="")

        fields_payload = []
        if r["priority_option_id"] and priority_field:
            fields_payload.append({"id": priority_field["id"], "value": str(r["priority_option_id"])})
        if r["tier_option_id"] and tier_field:
            fields_payload.append({"id": tier_field["id"], "value": str(r["tier_option_id"])})

        if fields_payload:
            try:
                set_custom_fields(session, r["id"], fields_payload)
                fields_count += 1
                print(f"{r['priority']}/{r['tier']} ✓")
            except requests.HTTPError as e:
                print(f"custom fields failed ({e})")
        else:
            print("no custom fields")

    print(f"\nDone. Tagged {tagged_count}, assigned {assigned_count}, "
          f"spammed {spam_count}, set fields on {fields_count} "
          f"of {len(all_results)} tickets.")


def main():
    parser = argparse.ArgumentParser(description="Triage Help Scout tickets with Claude.")
    parser.add_argument(
        "--conversation-id",
        "-c",
        action="append",
        dest="conversation_ids",
        metavar="ID",
        help="Triage only this conversation (repeat for multiple). Default: all unassigned in first mailbox.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Apply triage without confirmation (for automation / webhooks).",
    )
    args = parser.parse_args()
    run_triage(
        conversation_ids=args.conversation_ids,
        auto_apply=args.yes,
    )


if __name__ == "__main__":
    main()
