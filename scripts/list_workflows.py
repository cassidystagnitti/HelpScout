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


def fetch_manual_workflows(session):
    workflows = []
    page = 1

    while True:
        print(f"Fetching workflows page {page} …")
        data = api_get(session, f"{BASE_URL}/workflows", params={
            "type": "manual",
            "page": page,
        })

        page_workflows = data.get("_embedded", {}).get("workflows", [])
        workflows.extend(page_workflows)

        total_pages = data.get("page", {}).get("totalPages", 1)
        print(f"  Got {len(page_workflows)} workflows (page {page}/{total_pages})")

        if page >= total_pages:
            break
        page += 1

    return workflows


def main():
    if not APP_ID or not APP_SECRET:
        sys.exit("Error: set HELPSCOUT_APP_ID and HELPSCOUT_APP_SECRET in your .env file.")

    print("Authenticating with Help Scout …")
    token = get_access_token()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    workflows = fetch_manual_workflows(session)
    active = [wf for wf in workflows if wf.get("status") == "active"]

    if not active:
        print("\nNo active manual workflows found.")
        return

    output_path = os.path.join(ROOT_DIR, "manual_workflows.csv")
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name", "mailboxId", "createdAt", "modifiedAt"])
        for wf in active:
            writer.writerow([
                wf["id"],
                wf["name"],
                wf.get("mailboxId", ""),
                wf.get("createdAt", ""),
                wf.get("modifiedAt", ""),
            ])

    print(f"\nFound {len(active)} active manual workflow(s) (of {len(workflows)} total).")
    print(f"Saved to {output_path}")


if __name__ == "__main__":
    main()
