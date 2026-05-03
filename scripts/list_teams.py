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

OUTPUT_FILE = os.path.join(ROOT_DIR, "teams.csv")


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


def fetch_all_teams(session):
    teams = []
    page = 1

    while True:
        print(f"Fetching teams page {page} …")
        data = api_get(session, f"{BASE_URL}/teams", params={"page": page})

        page_teams = data.get("_embedded", {}).get("teams", [])
        teams.extend(page_teams)

        total_pages = data.get("page", {}).get("totalPages", 1)
        print(f"  Got {len(page_teams)} teams (page {page}/{total_pages})")

        if page >= total_pages:
            break
        page += 1

    return teams


def write_csv(teams):
    teams.sort(key=lambda t: t.get("name", "").lower())

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "id"])

        for team in teams:
            writer.writerow([
                team.get("name", ""),
                team.get("id", ""),
            ])

    return OUTPUT_FILE


def main():
    if not APP_ID or not APP_SECRET:
        sys.exit("Error: set HELPSCOUT_APP_ID and HELPSCOUT_APP_SECRET in your .env file.")

    print("Authenticating with Help Scout …")
    token = get_access_token()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    teams = fetch_all_teams(session)
    print(f"\nFound {len(teams)} teams total.\n")

    if not teams:
        print("No teams found.")
        return

    output_path = write_csv(teams)

    print(f"{'=' * 50}")
    for team in sorted(teams, key=lambda t: t.get("name", "").lower()):
        print(f"  {team['name']}  (ID: {team['id']})")
    print(f"{'=' * 50}")
    print(f"\nSaved {len(teams)} teams to {output_path}")


if __name__ == "__main__":
    main()
