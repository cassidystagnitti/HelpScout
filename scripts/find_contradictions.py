import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser

import anthropic
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT_DIR, ".env"))

DB_FILE = os.path.join(ROOT_DIR, "saved_replies.db")
OUTPUT_FILE = os.path.join(ROOT_DIR, "contradictions.md")

MIN_CATEGORY_PREFIX_LEN = 2


class HTMLTextExtractor(HTMLParser):
    """Strip HTML tags and return plain text."""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    extractor = HTMLTextExtractor()
    extractor.feed(html)
    text = extractor.get_text()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def category_from_name(name: str) -> str:
    prefix = name.split()[0] if name.split() else "Other"
    if len(prefix) < MIN_CATEGORY_PREFIX_LEN:
        return "Other"
    return prefix


def load_replies() -> list[dict]:
    if not os.path.exists(DB_FILE):
        sys.exit(f"Error: database not found at {DB_FILE}\nRun fetch_saved_replies.py first.")

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT name, text, chat_text FROM saved_replies ORDER BY name"
    ).fetchall()
    conn.close()

    replies = []
    for row in rows:
        plain = html_to_text(row["text"])
        chat = (row["chat_text"] or "").strip()
        body = plain or chat
        if not body:
            continue
        replies.append({
            "name": row["name"],
            "category": category_from_name(row["name"]),
            "body": body,
        })
    return replies


def group_by_category(replies: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in replies:
        groups[r["category"]].append(r)
    return dict(sorted(groups.items()))


def build_prompt(groups: dict[str, list[dict]]) -> str:
    sections: list[str] = []
    for category, replies in groups.items():
        lines = [f"## Category: {category}"]
        for r in replies:
            lines.append(f"### Reply: \"{r['name']}\"")
            lines.append(r["body"])
            lines.append("")
        sections.append("\n".join(lines))

    all_replies_text = "\n---\n\n".join(sections)

    return f"""\
Below are all saved replies used by a customer-support team, organized by
category. Each reply is a template that agents send to customers.

{all_replies_text}

---

Analyze ALL of these saved replies and identify any contradictions — places
where two or more replies give conflicting information, instructions, or
policies. Look for:

1. **Within-category contradictions** — replies in the same category that
   disagree with each other (e.g., two refund replies stating different
   refund windows or eligibility rules).
2. **Cross-category contradictions** — replies in different categories that
   make conflicting claims about the same topic (e.g., an account-management
   reply and a cancellation reply disagreeing on how billing works).

For each contradiction found, provide:
- The names of the conflicting replies
- A direct quote from each reply showing the conflict
- A brief explanation of why these statements contradict each other

If you find no contradictions in a particular area, you can note that briefly,
but focus your output on actual contradictions. Organize your findings clearly
with markdown headings."""


def main():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("Error: set ANTHROPIC_API_KEY in your .env file.")

    print("Loading saved replies from database …")
    replies = load_replies()
    if not replies:
        sys.exit("No saved replies found in the database.")

    groups = group_by_category(replies)

    total_chars = sum(len(r["body"]) for r in replies)
    print(
        f"Loaded {len(replies)} replies across {len(groups)} categories "
        f"({total_chars:,} chars of text)"
    )

    prompt = build_prompt(groups)
    print(f"Prompt size: {len(prompt):,} chars")
    print("Sending to Claude for analysis (this may take a minute) …")

    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        system=(
            "You are an expert at analyzing customer support documentation "
            "for consistency. Be thorough and precise. When quoting reply "
            "text, use short direct quotes."
        ),
        messages=[{"role": "user", "content": prompt}],
    )

    result_text = message.content[0].text
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = (
        f"# Saved Replies — Contradiction Analysis\n\n"
        f"*Generated {now} from {len(replies)} replies "
        f"across {len(groups)} categories*\n\n---\n\n"
    )
    report = header + result_text

    with open(OUTPUT_FILE, "w") as f:
        f.write(report)

    print(f"\nReport written to {OUTPUT_FILE}")
    print("\n" + "=" * 60 + "\n")
    print(result_text)


if __name__ == "__main__":
    main()
