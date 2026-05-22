"""Fetch only today's calendar events + outstanding tasks and write live.json.

Runs every ~10 minutes via .github/workflows/refresh_live.yml. Deliberately
does NOT call the Gemini API — the AI overview/plays/yesterday content is
rebuilt once a day by generate_brief.py. The Daily Brief page fetches this
file client-side to keep the Schedule and Open work sections current.
"""
from generate_brief import (
    get_google_credentials,
    get_todays_events,
    get_outstanding_tasks,
    write_live_json,
)


def main():
    creds = get_google_credentials()
    events = get_todays_events(creds)
    tasks = get_outstanding_tasks(creds)
    write_live_json(events, tasks)
    print(f"Wrote live.json — {len(events)} events, {len(tasks)} tasks")


if __name__ == "__main__":
    main()
