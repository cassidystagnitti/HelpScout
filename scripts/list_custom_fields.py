import csv
import os
import sys
import time

import requests
from dotenv import load_dotenv

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT_DIR, ".env"))

BASE_URL = "https://api.helpscout.net/v2"
TOKEN_URL = f"{BASE_URL}/oauth2/token"

APP_ID = os.getenv("HELPSCOUT_APP_ID")
APP_SECRET = os.getenv("HELPSCOUT_APP_SECRET")

OUTPUT_FILE = os.path.join(ROOT_DIR, "custom_fields.csv")


def get_access_token():
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


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


def fetch_custom_fields(session, mailbox_id):
    fields = []
    page = 1

    while True:
        print(f"Fetching custom fields page {page} …")
        data = api_get(
            session,
            f"{BASE_URL}/mailboxes/{mailbox_id}/fields",
            params={"page": page},
        )

        page_fields = data.get("_embedded", {}).get("fields", [])
        fields.extend(page_fields)

        total_pages = data.get("page", {}).get("totalPages", 1)
        print(f"  Got {len(page_fields)} fields (page {page}/{total_pages})")

        if page >= total_pages:
            break
        page += 1

    return fields


def write_csv(fields):
    fields.sort(key=lambda f: f.get("name", "").lower())

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "id", "type", "required", "order", "options"])

        for field in fields:
            options = field.get("options", [])
            options_str = "; ".join(
                f"{opt.get('label', '')}:{opt.get('id', '')}"
                for opt in options
            ) if options else ""

            writer.writerow([
                field.get("name", ""),
                field.get("id", ""),
                field.get("type", ""),
                field.get("required", False),
                field.get("order", ""),
                options_str,
            ])

    return OUTPUT_FILE


def main():
    if not APP_ID or not APP_SECRET:
        sys.exit("Error: set HELPSCOUT_APP_ID and HELPSCOUT_APP_SECRET in your .env file.")

    print(f"Authenticating with Help Scout …")
    token = get_access_token()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    mailbox_id = get_first_mailbox_id(session)

    fields = fetch_custom_fields(session, mailbox_id)
    print(f"\nFound {len(fields)} custom fields in mailbox {mailbox_id}.\n")

    if not fields:
        print("No custom fields found.")
        return

    output_path = write_csv(fields)

    print(f"{'=' * 50}")
    for field in sorted(fields, key=lambda f: f.get("name", "").lower()):
        ftype = field.get("type", "unknown")
        required = " (required)" if field.get("required") else ""
        options = field.get("options", [])
        opts_str = ""
        if options:
            parts = [f"{o.get('label', '')}({o.get('id', '')})" for o in options]
            opts_str = f"  [{', '.join(parts)}]"
        print(f"  {field['name']}  (type: {ftype}{required}){opts_str}")
    print(f"{'=' * 50}")
    print(f"\nSaved {len(fields)} custom fields to {output_path}")


if __name__ == "__main__":
    main()
