"""
Read customer feedback CSV, ask Claude to extract feature requests / product ideas,
merge across batches, and rank by popularity (number of citing feedback rows).
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime

import anthropic
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT_DIR, ".env"))

DEFAULT_INPUT = os.path.join(ROOT_DIR, "feedback.csv")
DEFAULT_OUTPUT = os.path.join(ROOT_DIR, "feature_requests.md")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

BATCH_SIZE = 18
BODY_PREVIEW_CHARS = 2500


def parse_json_response(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


EXTRACT_PROMPT = """\
You analyze customer feedback for a meditation / mindfulness app (Happier Meditation).

Below are {n} feedback messages. Each starts with FEEDBACK ID then the text.

Task:
1. Identify **feature requests**, **product ideas**, and **actionable improvements** users want \
(desktop app, timers, instructors, playlists, Siri, subtitles, downloads, reminders, billing \
features — anything asking for new or changed functionality).
2. **Ignore**: pure compliments with no asks, unrelated personal stories, spam, or generic support \
with no product angle — unless there is also a concrete product ask.
3. Group similar wording into **one idea** per theme when the same underlying request appears once \
in this batch — attach every relevant FEEDBACK ID to that idea.

Respond with ONLY valid JSON:
{{
  "ideas": [
    {{
      "title": "Short label (few words)",
      "description": "One or two sentences on what users want.",
      "feedback_ids": [ <int IDs from headers in this batch> ]
    }}
  ]
}}

Rules:
- `feedback_ids` must only contain IDs listed in this batch; each ID at most once per idea entry.
- If there are **no** product/feature asks in this batch, return `"ideas": []`.
- JSON only — no markdown fences or extra text.


{feedback_block}
"""


MERGE_PROMPT = """\
You merge Claude extractions about feature requests / product ideas for the same meditation app.

Below are JSON results from {batch_count} separate batches (`ideas` arrays). Merge overlapping \
themes into one idea each. Prefer clear, reusable titles.

For each merged idea:
- Combine and dedupe `feedback_ids` (integers — same ask in two batches becomes one combined list).

Respond with ONLY valid JSON:
{{
  "summary": "2-4 sentences on overall themes.",
  "ideas": [
    {{
      "title": "...",
      "description": "...",
      "feedback_ids": [ ... unique integers ... ]
    }}
  ]
}}

Sort `ideas` by **descreasing** number of `feedback_ids` (most cited first — this is popularity). \
JSON only — no fences.


{batch_blob}
"""


def load_feedback_rows(path: str):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "text" not in reader.fieldnames or "id" not in reader.fieldnames:
            sys.exit(f"CSV must have columns text,id — got {reader.fieldnames!r}")
        for row in reader:
            text = (row.get("text") or "").strip()
            raw_id = (row.get("id") or "").strip()
            if not text or not raw_id:
                continue
            try:
                fid = int(raw_id)
            except ValueError:
                continue
            rows.append({"id": fid, "text": text})
    return rows


def format_feedback_block(batch):
    chunks = []
    for row in batch:
        body = row["text"]
        if len(body) > BODY_PREVIEW_CHARS:
            body = body[:BODY_PREVIEW_CHARS] + "\n[…truncated]"
        chunks.append(f"--- FEEDBACK ID: {row['id']} ---\n{body}\n--- END ---\n")
    return "".join(chunks)


def extract_batch(client, batch):
    block = format_feedback_block(batch)
    prompt = EXTRACT_PROMPT.format(n=len(batch), feedback_block=block)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text
    data = parse_json_response(raw)
    ideas = data.get("ideas") or []
    batch_ids = {r["id"] for r in batch}
    filtered = []
    for idea in ideas:
        ids = []
        for x in idea.get("feedback_ids") or []:
            try:
                xi = int(x)
            except (TypeError, ValueError):
                continue
            if xi in batch_ids:
                ids.append(xi)
        seen = set()
        deduped = []
        for i in ids:
            if i not in seen:
                seen.add(i)
                deduped.append(i)
        filtered.append({
            "title": (idea.get("title") or "Untitled").strip(),
            "description": (idea.get("description") or "").strip(),
            "feedback_ids": deduped,
        })
    return {"ideas": filtered}


def merge_results(client, batch_payloads):
    blob = ""
    for i, p in enumerate(batch_payloads, 1):
        blob += f"=== BATCH {i} ===\n{json.dumps(p, ensure_ascii=False, indent=2)}\n\n"

    prompt = MERGE_PROMPT.format(batch_count=len(batch_payloads), batch_blob=blob)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_json_response(msg.content[0].text)


def sort_ideas_by_popularity(ideas, valid_ids=None):
    valid_ids = valid_ids or set()

    def pop_count(idea):
        ids = list(idea.get("feedback_ids") or [])
        if valid_ids:
            ids = [i for i in ids if i in valid_ids]
        return len(set(ids))

    out = []
    for idea in ideas:
        merged_ids = sorted(set(idea.get("feedback_ids") or []))
        if valid_ids:
            merged_ids = [i for i in merged_ids if i in valid_ids]
        title = (idea.get("title") or "Untitled").strip()
        desc = (idea.get("description") or "").strip()
        out.append({
            "title": title,
            "description": desc,
            "feedback_ids": merged_ids,
            "mentions": len(set(merged_ids)),
        })
    out.sort(key=lambda x: (-x["mentions"], x["title"].lower()))
    return out


def write_markdown(path, merged, total_feedback_rows_used, batches):
    summary = merged.get("summary", "").strip()
    lines = [
        "# Feature requests & product ideas (from feedback)",
        "",
        f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} — "
        f"{total_feedback_rows_used} feedback rows in {batches} batch(es)*",
        "",
        "## Summary",
        "",
        summary or "_(none)_",
        "",
        "## Ideas by popularity",
        "",
        "| Rank | Mentions | Idea | Sample IDs |",
        "|-----:|:---------|:-----|:-----------|",
    ]

    ideas = merged.get("ideas") if isinstance(merged.get("ideas"), list) else []
    ideas = sort_ideas_by_popularity(ideas)

    for rank, idea in enumerate(ideas, 1):
        mentions = idea.get("mentions", 0)
        title = idea.get("title", "")
        ids = idea.get("feedback_ids") or []
        preview = ", ".join(str(i) for i in ids[:5])
        if len(ids) > 5:
            preview += f", … (+{len(ids) - 5} more)"

        escaped_title = title.replace("|", "\\|")
        escaped_desc = (idea.get("description") or "").replace("|", "\\|")
        cell = f"**{escaped_title}** — {escaped_desc}" if escaped_desc else f"**{escaped_title}**"
        lines.append(f"| {rank} | {mentions} | {cell} | {preview or '—'} |")

    lines.extend([
        "",
        "## Detail (sorted by mentions)",
        "",
    ])

    for idea in ideas:
        mentions = idea.get("mentions", 0)
        title = idea.get("title", "")
        lines.append(f"### {title} ({mentions} feedback row(s))")
        lines.append("")
        if idea.get("description"):
            lines.append(idea["description"])
            lines.append("")
        ids = idea.get("feedback_ids") or []
        lines.append("Feedback IDs: " + ", ".join(str(i) for i in ids))
        lines.append("")

    body = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return body


def main():
    parser = argparse.ArgumentParser(
        description="Extract feature ideas from feedback CSV with Claude and rank by popularity.",
    )
    parser.add_argument(
        "--input",
        "-i",
        default=DEFAULT_INPUT,
        help=f"CSV with text,id (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=DEFAULT_OUTPUT,
        help=f"Markdown report path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only analyze the first N feedback rows (after filtering).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        metavar="N",
        help=f"Rows per Claude request (default: {BATCH_SIZE}).",
    )
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        sys.exit("Error: set ANTHROPIC_API_KEY in your .env file.")

    if not os.path.isfile(args.input):
        sys.exit(f"Error: file not found: {args.input}")

    rows = load_feedback_rows(args.input)
    if args.limit is not None:
        rows = rows[: max(0, args.limit)]

    if not rows:
        sys.exit("No feedback rows loaded (need non-empty text and numeric id columns).")

    batch_size = max(1, args.batch_size)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    payloads = []
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        bn = start // batch_size + 1
        total_batches = (len(rows) + batch_size - 1) // batch_size
        print(f"Batch {bn}/{total_batches} ({len(batch)} rows) → Claude …")

        try:
            payloads.append(extract_batch(client, batch))
        except json.JSONDecodeError as e:
            print(f"  JSON error: {e}")
            raise
        except Exception as e:
            print(f"  API error: {e}")
            raise

    if len(payloads) == 1:
        merged = {
            "summary": "Single-batch run (themes from one Claude pass; no merge step).",
            "ideas": payloads[0].get("ideas") or [],
        }
    else:
        print(f"Merging {len(payloads)} batch results …")
        merged_raw = merge_results(client, payloads)
        valid = {r["id"] for r in rows}
        merged = {
            "summary": merged_raw.get("summary", ""),
            "ideas": [],
        }
        merged["ideas"] = sort_ideas_by_popularity(
            merged_raw.get("ideas") or [],
            valid_ids=valid,
        )

    text = write_markdown(
        args.output,
        merged,
        total_feedback_rows_used=len(rows),
        batches=len(payloads),
    )
    print()
    print("=" * 60)
    print(text[:4000] + (" …\n(showing first 4000 chars)" if len(text) > 4000 else ""))
    print("=" * 60)
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
