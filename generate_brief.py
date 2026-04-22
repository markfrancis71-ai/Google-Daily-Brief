import os
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import anthropic

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
]

TIMEZONE = os.environ.get("USER_TIMEZONE", "America/New_York")
MEETING_NOTES_FOLDER = os.environ.get("MEETING_NOTES_FOLDER", "sent emails")


def get_google_credentials():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def get_todays_events(creds):
    service = build("calendar", "v3", credentials=creds)
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)

    result = service.events().list(
        calendarId="primary",
        timeMin=start_of_day.isoformat(),
        timeMax=end_of_day.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return result.get("items", [])


def get_outstanding_tasks(creds):
    service = build("tasks", "v1", credentials=creds)
    tasklists = service.tasklists().list().execute().get("items", [])
    all_tasks = []
    for tasklist in tasklists:
        tasks = service.tasks().list(
            tasklist=tasklist["id"],
            showCompleted=False,
            showHidden=False,
        ).execute().get("items", [])
        for task in tasks:
            task["_listTitle"] = tasklist["title"]
            all_tasks.append(task)
    return all_tasks


def extract_doc_text(doc):
    """Extract plain text from a Google Docs document body."""
    text_parts = []
    for element in doc.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for run in paragraph.get("elements", []):
            text_run = run.get("textRun")
            if text_run:
                text_parts.append(text_run.get("content", ""))
    return "".join(text_parts).strip()


def get_meeting_notes(creds):
    """Find all Google Docs added to the meeting notes folder in the last 24 hours."""
    drive = build("drive", "v3", credentials=creds)
    docs = build("docs", "v1", credentials=creds)

    # Find the folder
    folder_results = drive.files().list(
        q=f"name='{MEETING_NOTES_FOLDER}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)",
    ).execute()

    folders = folder_results.get("files", [])
    if not folders:
        print(f"Folder '{MEETING_NOTES_FOLDER}' not found in Drive.")
        return []

    folder_id = folders[0]["id"]

    # Find docs created in the last 24 hours
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    files_result = drive.files().list(
        q=(
            f"'{folder_id}' in parents"
            f" and mimeType='application/vnd.google-apps.document'"
            f" and createdTime >= '{since}'"
            f" and trashed=false"
        ),
        fields="files(id, name, createdTime)",
        orderBy="createdTime desc",
    ).execute()

    files = files_result.get("files", [])
    if not files:
        return []

    meeting_notes = []
    for f in files:
        doc = docs.documents().get(documentId=f["id"]).execute()
        text = extract_doc_text(doc)
        if text:
            meeting_notes.append({
                "title": f["name"],
                "created": f["createdTime"],
                "content": text,
            })

    return meeting_notes


def get_ai_summary(context):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = f"""You are a personal productivity assistant. Based on the following schedule and tasks, provide:
1. A brief 2-3 sentence overview of the day
2. 3-5 specific, actionable suggestions for optimizing the day (time blocking, preparation, prioritization, etc.)

Be concise, warm, and practical. Format your response as JSON with keys "overview" and "suggestions" (array of strings).

{context}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(raw)


def get_ai_meeting_reviews(meeting_notes):
    """Ask Claude to review each meeting doc and return structured summaries."""
    if not meeting_notes:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    reviews = []

    for note in meeting_notes:
        prompt = f"""You are reviewing a meeting summary document. Based on the content below, provide:
1. A 2-3 sentence summary of the key discussion points
2. A list of takeaways or action items (be specific and actionable)

Format your response as JSON with keys:
- "summary": string
- "takeaways": array of strings

Meeting document title: {note["title"]}

Content:
{note["content"][:4000]}"""

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
        reviews.append({
            "title": note["title"],
            "summary": parsed.get("summary", ""),
            "takeaways": parsed.get("takeaways", []),
        })

    return reviews


def format_event_time(event):
    start = event.get("start", {})
    if "dateTime" in start:
        dt = datetime.fromisoformat(start["dateTime"])
        return dt.strftime("%-I:%M %p")
    return "All day"


def format_event_end_time(event):
    end = event.get("end", {})
    if "dateTime" in end:
        dt = datetime.fromisoformat(end["dateTime"])
        return dt.strftime("%-I:%M %p")
    return ""


def build_context(events, tasks):
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).strftime("%A, %B %d, %Y")
    lines = [f"Today is {today}.\n"]

    lines.append("CALENDAR EVENTS TODAY:")
    if events:
        for e in events:
            start = format_event_time(e)
            end = format_event_end_time(e)
            title = e.get("summary", "Untitled")
            time_str = f"{start} - {end}" if end else start
            location = e.get("location", "")
            loc_str = f" @ {location}" if location else ""
            lines.append(f"- {time_str}: {title}{loc_str}")
    else:
        lines.append("- No events scheduled.")

    lines.append("\nOUTSTANDING TASKS:")
    if tasks:
        for t in tasks:
            title = t.get("title", "Untitled")
            due = t.get("due", "")
            due_str = f" (due {datetime.fromisoformat(due[:10]).strftime('%b %d')})" if due else ""
            list_title = t.get("_listTitle", "")
            lines.append(f"- [{list_title}] {title}{due_str}")
    else:
        lines.append("- No outstanding tasks.")

    return "\n".join(lines)


def render_html(events, tasks, ai, meeting_reviews):
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    today_str = now.strftime("%A, %B %d, %Y")
    generated_at = now.strftime("%-I:%M %p")

    events_html = ""
    if events:
        for e in events:
            start = format_event_time(e)
            end = format_event_end_time(e)
            title = e.get("summary", "Untitled")
            location = e.get("location", "")
            loc_html = f'<span class="location">📍 {location}</span>' if location else ""
            time_str = f"{start} – {end}" if end else start
            events_html += f"""
            <div class="event">
                <span class="event-time">{time_str}</span>
                <span class="event-title">{title}</span>
                {loc_html}
            </div>"""
    else:
        events_html = '<p class="empty">No events scheduled for today.</p>'

    tasks_html = ""
    if tasks:
        for t in tasks:
            title = t.get("title", "Untitled")
            due = t.get("due", "")
            due_str = f'<span class="due">Due {datetime.fromisoformat(due[:10]).strftime("%b %d")}</span>' if due else ""
            list_title = t.get("_listTitle", "")
            tasks_html += f"""
            <div class="task">
                <span class="task-list">{list_title}</span>
                <span class="task-title">{title}</span>
                {due_str}
            </div>"""
    else:
        tasks_html = '<p class="empty">No outstanding tasks.</p>'

    suggestions_html = "".join(
        f"<li>{s}</li>" for s in ai.get("suggestions", [])
    )

    if meeting_reviews:
        yesterday_html = ""
        for review in meeting_reviews:
            takeaways_html = "".join(
                f"<li>{t}</li>" for t in review["takeaways"]
            )
            yesterday_html += f"""
            <div class="meeting">
                <h3 class="meeting-title">{review["title"]}</h3>
                <p class="meeting-summary">{review["summary"]}</p>
                <p class="meeting-label">Takeaways &amp; Action Items</p>
                <ul class="meeting-takeaways">{takeaways_html}</ul>
            </div>"""
        yesterday_section = f"""
        <div class="card">
            <h2>Yesterday in Review</h2>
            {yesterday_html}
        </div>"""
    else:
        yesterday_section = f"""
        <div class="card">
            <h2>Yesterday in Review</h2>
            <p class="empty">No meeting notes found from the last 24 hours.</p>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Daily Brief – {today_str}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f5f5f7;
            color: #1d1d1f;
            min-height: 100vh;
            padding: 2rem 1rem;
        }}
        .container {{ max-width: 720px; margin: 0 auto; }}
        header {{ margin-bottom: 2rem; }}
        header h1 {{ font-size: 2rem; font-weight: 700; }}
        header p {{ color: #6e6e73; margin-top: 0.25rem; }}
        .card {{
            background: #fff;
            border-radius: 16px;
            padding: 1.5rem;
            margin-bottom: 1.5rem;
            box-shadow: 0 2px 8px rgba(0,0,0,0.07);
        }}
        .card h2 {{
            font-size: 1rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #6e6e73;
            margin-bottom: 1rem;
        }}
        .overview {{ font-size: 1.05rem; line-height: 1.6; color: #1d1d1f; }}
        .suggestions ul {{ padding-left: 1.25rem; }}
        .suggestions li {{ margin-bottom: 0.6rem; line-height: 1.5; }}
        .event {{
            display: flex;
            flex-wrap: wrap;
            align-items: baseline;
            gap: 0.5rem;
            padding: 0.75rem 0;
            border-bottom: 1px solid #f0f0f0;
        }}
        .event:last-child {{ border-bottom: none; }}
        .event-time {{ font-size: 0.85rem; color: #6e6e73; min-width: 140px; }}
        .event-title {{ font-weight: 500; flex: 1; }}
        .location {{ font-size: 0.8rem; color: #6e6e73; width: 100%; padding-left: 140px; }}
        .task {{
            display: flex;
            flex-wrap: wrap;
            align-items: baseline;
            gap: 0.5rem;
            padding: 0.75rem 0;
            border-bottom: 1px solid #f0f0f0;
        }}
        .task:last-child {{ border-bottom: none; }}
        .task-list {{ font-size: 0.8rem; background: #f0f0f5; border-radius: 4px; padding: 2px 6px; color: #6e6e73; }}
        .task-title {{ font-weight: 500; flex: 1; }}
        .due {{ font-size: 0.8rem; color: #ff6b35; }}
        .meeting {{
            padding: 1rem 0;
            border-bottom: 1px solid #f0f0f0;
        }}
        .meeting:last-child {{ border-bottom: none; }}
        .meeting-title {{ font-size: 1rem; font-weight: 600; margin-bottom: 0.4rem; }}
        .meeting-summary {{ font-size: 0.95rem; line-height: 1.6; color: #3a3a3c; margin-bottom: 0.75rem; }}
        .meeting-label {{ font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: #6e6e73; margin-bottom: 0.4rem; }}
        .meeting-takeaways {{ padding-left: 1.25rem; }}
        .meeting-takeaways li {{ margin-bottom: 0.4rem; font-size: 0.9rem; line-height: 1.5; }}
        .empty {{ color: #6e6e73; font-style: italic; }}
        footer {{ text-align: center; color: #aeaeb2; font-size: 0.8rem; margin-top: 2rem; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Daily Brief</h1>
            <p>{today_str} &middot; Generated at {generated_at}</p>
        </header>

        <div class="card">
            <h2>Today's Overview</h2>
            <p class="overview">{ai.get("overview", "")}</p>
        </div>

        <div class="card suggestions">
            <h2>Optimization Suggestions</h2>
            <ul>{suggestions_html}</ul>
        </div>

        <div class="card">
            <h2>Schedule</h2>
            {events_html}
        </div>

        <div class="card">
            <h2>Outstanding Tasks</h2>
            {tasks_html}
        </div>

        {yesterday_section}

        <footer>Powered by Google Calendar, Google Tasks, Google Drive &amp; Claude AI</footer>
    </div>
</body>
</html>"""


def main():
    creds = get_google_credentials()
    events = get_todays_events(creds)
    tasks = get_outstanding_tasks(creds)
    meeting_notes = get_meeting_notes(creds)
    context = build_context(events, tasks)
    ai = get_ai_summary(context)
    meeting_reviews = get_ai_meeting_reviews(meeting_notes)
    html = render_html(events, tasks, ai, meeting_reviews)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Generated index.html")


if __name__ == "__main__":
    main()
