# Help Scout Scripts

A collection of scripts for exporting data from Help Scout.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   ```
   You can generate Help Scout credentials under **Your Profile > My Apps > Create My App**. For the contradiction finder, add your Anthropic API key as well.

## Scripts

### fetch_feedback.py

Pulls the initial customer message from all Help Scout conversations tagged **feedback** created in the last 4 months and writes them to a CSV.

```bash
python scripts/fetch_feedback.py
```

Output is written to `feedback.csv` with two columns: `text` and `id`.

### fetch_saved_replies.py

Fetches all saved replies from every mailbox in the Help Scout account and stores them in a SQLite database.

```bash
python scripts/fetch_saved_replies.py
```

Output is written to `saved_replies.db` with the following schema:

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Saved reply ID (primary key) |
| `mailbox_id` | INTEGER | Mailbox the reply belongs to |
| `mailbox_name` | TEXT | Mailbox name |
| `name` | TEXT | Saved reply name/title |
| `text` | TEXT | Full HTML body (email replies) |
| `chat_text` | TEXT | Plain-text body (chat replies) |
| `fetched_at` | TEXT | ISO 8601 timestamp of when the data was fetched |

Each run replaces all rows so the database always reflects the current state of saved replies.

### find_contradictions.py

Reads all saved replies from `saved_replies.db`, groups them by the name-prefix category (e.g. `CancelRefund`, `AccountManagement`, `TechSupport`), and sends them to Claude to identify contradictions between replies.

Requires an `ANTHROPIC_API_KEY` in your `.env` file.

```bash
python scripts/find_contradictions.py
```

Output is written to `contradictions.md` and printed to the console.
