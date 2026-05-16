import os
import re
import json
import html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
# anthropic is imported lazily inside the AI functions so fetch_live.py can
# reuse the Google helpers without installing the Anthropic SDK.

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


def extract_meeting_prep(event):
    """Return the prep block text from an event's description, or None.

    Convention: a line starting with 'Meeting Prep:' (case-insensitive) marks
    the block; everything after the colon through the end of the description is
    treated as prep content. Google Calendar descriptions are HTML, so <br>/<p>
    are normalized to newlines and remaining tags stripped before matching.
    """
    desc = event.get("description") or ""
    if not desc.strip():
        return None
    desc = re.sub(r"<br\s*/?>", "\n", desc, flags=re.IGNORECASE)
    desc = re.sub(r"</p\s*>", "\n", desc, flags=re.IGNORECASE)
    desc = re.sub(r"<[^>]+>", "", desc)
    desc = html.unescape(desc)
    match = re.search(r"meeting\s*prep\s*:\s*(.*)", desc, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return match.group(1).strip()


_PREP_STOP_WORDS = {
    "meeting", "prep", "the", "and", "for", "with", "this", "that", "our",
    "sync", "review", "standup", "call", "discussion", "weekly", "monthly",
    "quarterly", "daily", "1:1", "one", "on", "one-on-one", "agenda",
    "follow", "followup", "follow-up", "kickoff", "kick", "off",
}


def _extract_prep_search_keys(prep_block, event_summary, attendees):
    """Build a list of search terms for the sent emails folder.

    The folder is full of summary docs labeled by the person/group/meeting
    name, so name-based filename matches are the strongest signal. Sources,
    in priority order:
      1. Names/topics named in the 'Meeting Prep:' block.
      2. Names in the event title itself ('1:1 with Jonathan' → Jonathan).
      3. Attendee display names / email local-parts.
    """
    keys = []
    seen = set()

    def add(term):
        t = (term or "").strip()
        if not t or t.lower() in seen or len(t) < 3 or t.lower() in _PREP_STOP_WORDS:
            return
        seen.add(t.lower())
        keys.append(t)

    def harvest(text):
        for match in re.findall(r"[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2}", text or ""):
            # Strip trailing stop words from multi-token matches
            # ("Andre With" → "Andre"). Skip the match entirely if every token
            # is a stop word.
            tokens = [t for t in match.split() if t.lower() not in _PREP_STOP_WORDS]
            if tokens:
                add(" ".join(tokens))

    harvest(prep_block)
    harvest(event_summary)

    for a in attendees or []:
        name = a.get("displayName") or ""
        if name:
            add(name)
        email = a.get("email") or ""
        if email and "@" in email:
            add(email.split("@")[0].replace(".", " ").replace("_", " "))

    return keys


def _search_sent_emails_for_keys(drive, docs_api, folder_id, keys, total_limit=6):
    """Return up to total_limit unique sent-email docs that match any key.

    The user labels docs in this folder by the person/group/meeting name, so
    'name contains' (filename match) is the strongest signal and is queried
    first per key. 'fullText contains' (body match) is queried as a fallback
    when the filename query returns nothing. Results are deduped by file id
    across keys; each doc is truncated so we don't blow up the AI prompt
    with year-long meeting histories.
    """
    seen = set()
    results = []
    for key in keys:
        if len(results) >= total_limit:
            break
        safe = key.replace("\\", "\\\\").replace("'", "\\'")
        base = (
            f"'{folder_id}' in parents"
            f" and mimeType='application/vnd.google-apps.document'"
            f" and trashed=false"
        )
        files = []
        for clause in (f"name contains '{safe}'", f"fullText contains '{safe}'"):
            try:
                files = drive.files().list(
                    q=f"{base} and {clause}",
                    fields="files(id, name, modifiedTime)",
                    orderBy="modifiedTime desc",
                    pageSize=5,
                ).execute().get("files", [])
            except Exception as e:
                print(f"Drive search failed for {key!r} ({clause}): {type(e).__name__}: {e}")
                continue
            if files:
                break  # Don't run the body-text query if the filename query already hit.
        for f in files:
            if f["id"] in seen or len(results) >= total_limit:
                continue
            seen.add(f["id"])
            try:
                doc = docs_api.documents().get(documentId=f["id"]).execute()
                text = extract_doc_text(doc)
            except Exception as e:
                print(f"Doc fetch failed for {f.get('name')!r}: {type(e).__name__}: {e}")
                continue
            if text:
                results.append({"title": f["name"], "content": text[:1800]})
    return results


def get_ai_summary(context):
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=8)
    prompt = f"""You are a personal productivity assistant for Francis Inc.

Voice: Declarative. Plain. Em-dashes liberally. Lead with the verb. No emoji, no marketing fluff, no hedging. Address the reader as "you". Make decisions — don't ask the reader to.

Based on the schedule and tasks below, produce JSON with:
- "overview": 2-3 sentences naming the shape of the day, where the friction is, and what matters most.
- "suggestions": 3-5 specific plays — each one a single declarative sentence, lead with the verb (e.g., "Block 9-11 for the architecture doc — your only deep-work window before the 2pm review.").

Return JSON only. Keys: "overview" (string), "suggestions" (array of strings).

{context}"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as e:
        # Don't let a transient Anthropic outage (e.g. 529 Overloaded) sink the
        # whole publish — ship the schedule + open work with a degraded overview.
        print(f"AI summary unavailable ({type(e).__name__}: {e}); using fallback.")
        return {
            "overview": "AI insights are unavailable right now — the schedule and open work below are current.",
            "suggestions": [],
        }


def get_ai_meeting_prep(event, prep_block, attendees, related_docs):
    """Ask Claude for a summary + task list to prep for one meeting.

    If the prep block contains explicit overriding instructions Claude follows
    them; otherwise it falls back to summarizing the meeting person/topic from
    the sent-email excerpts and producing a generic 'to prepare' task list.
    """
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=8)

    title = event.get("summary") or "Untitled"
    attendee_lines = []
    for a in attendees or []:
        nm = (a.get("displayName") or "").strip()
        em = (a.get("email") or "").strip()
        if nm and em:
            attendee_lines.append(f"- {nm} <{em}>")
        elif nm or em:
            attendee_lines.append(f"- {nm or em}")
    attendee_str = "\n".join(attendee_lines) or "- (no attendees listed)"

    if related_docs:
        doc_str = "\n\n---\n\n".join(
            f"### {d['title']}\n{d['content']}" for d in related_docs
        )
    else:
        doc_str = "(no past meeting summaries matched — the folder may not have a doc for this person/group yet)"

    prep_block_str = (prep_block or "").strip() or "(empty — use default behavior)"

    prompt = f"""You are preparing the user for a meeting at Francis Inc.

Voice: Declarative. Plain. Em-dashes liberally. Lead with the verb. No emoji, no hedging, no marketing fluff. Address the reader as "you".

Meeting: {title}

Attendees:
{attendee_str}

Prep instructions (from the meeting description):
{prep_block_str}

Past meeting summaries with this person/group/topic (these are the user's own notes from prior meetings — typically labeled by the person's name, the group's name, or the meeting name; ordered most recent first):
{doc_str}

Behavior:
- If the prep instructions contain explicit overriding directions (e.g., "review the spec doc", "draft three options", "compare A vs B"), follow those instructions exactly — tune the summary and tasks to match.
- Otherwise, default to: a 2-3 sentence background summary of the working relationship/recurring themes based on the past meeting summaries (name the most recent concrete commitments, decisions, or open threads), plus a task list of what you should research, decide, follow up on, or be ready to discuss in today's meeting.
- Lean heavily on the past summaries when they exist — call out unresolved items by name, and reference dates/specifics from the notes when useful. If no past summaries were found, say so plainly and produce a generic prep list based on the meeting title.

Return JSON only:
- "summary": string (2-3 sentences)
- "tasks": array of 3-5 strings, each a single declarative sentence leading with a verb

JSON only."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
        return {
            "summary": parsed.get("summary", ""),
            "tasks": parsed.get("tasks", []) or [],
        }
    except Exception as e:
        print(f"Meeting prep unavailable for {title!r} ({type(e).__name__}: {e}); skipping.")
        return {
            "summary": "Prep unavailable — Claude was unreachable when this brief was built.",
            "tasks": [],
        }


def build_meeting_preps(creds, events):
    """For each event with a 'Meeting Prep:' marker, gather references from
    the sent emails folder and produce a summary + task list. Keyed by event
    id so the live layer can map prep back onto whichever event is in focus.
    """
    needs = []
    for e in events:
        block = extract_meeting_prep(e)
        if block is not None:
            needs.append((e, block))
    if not needs:
        return {}

    drive = build("drive", "v3", credentials=creds)
    docs_api = build("docs", "v1", credentials=creds)

    folder_id = None
    try:
        folders = drive.files().list(
            q=f"name='{MEETING_NOTES_FOLDER}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id, name)",
        ).execute().get("files", [])
        if folders:
            folder_id = folders[0]["id"]
        else:
            print(f"Folder {MEETING_NOTES_FOLDER!r} not found — prep will run without email context.")
    except Exception as e:
        print(f"Folder lookup failed ({type(e).__name__}: {e}); prep will run without email context.")

    preps = {}
    for event, block in needs:
        attendees = event.get("attendees") or []
        related = []
        if folder_id:
            keys = _extract_prep_search_keys(block, event.get("summary") or "", attendees)
            if keys:
                related = _search_sent_emails_for_keys(drive, docs_api, folder_id, keys)
        result = get_ai_meeting_prep(event, block, attendees, related)
        result["title"] = event.get("summary") or "Untitled"
        result["start_label"] = format_event_time(event)
        result["end_label"] = format_event_end_time(event)
        preps[event["id"]] = result
    return preps


def get_ai_meeting_reviews(meeting_notes):
    """Ask Claude to review each meeting doc and return structured summaries."""
    if not meeting_notes:
        return []

    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=8)
    reviews = []

    for note in meeting_notes:
        prompt = f"""You are reviewing a meeting summary document for Francis Inc.

Voice: Declarative. Plain. Em-dashes liberally. Lead with the verb. No emoji, no hedging, no marketing fluff.

Based on the content below, produce JSON with:
- "summary": 2-3 sentences naming what was discussed and what shape the conversation took.
- "takeaways": specific action items or decisions — each one a single declarative sentence, lead with the verb.

Return JSON only.

Meeting: {note["title"]}

Content:
{note["content"][:4000]}"""

        try:
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
        except Exception as e:
            print(f"Meeting review unavailable for {note['title']!r} ({type(e).__name__}: {e}); skipping.")
            reviews.append({
                "title": note["title"],
                "summary": "Review unavailable — Claude was unreachable when this brief was built.",
                "takeaways": [],
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


def get_focus_event(events, now=None):
    """Return the currently-running event, or the next upcoming one."""
    if not events:
        return None
    if now is None:
        now = datetime.now(ZoneInfo(TIMEZONE))
    upcoming = []
    for e in events:
        start = e.get("start", {})
        end = e.get("end", {})
        if "dateTime" not in start:
            continue
        start_dt = datetime.fromisoformat(start["dateTime"])
        end_dt = datetime.fromisoformat(end["dateTime"]) if "dateTime" in end else None
        if end_dt and start_dt <= now <= end_dt:
            return e
        if start_dt > now:
            upcoming.append((start_dt, e))
    if upcoming:
        upcoming.sort(key=lambda x: x[0])
        return upcoming[0][1]
    return None


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


# ── Reusable section renderers (shared by the daily build and live.json) ──

def _render_focus_prep_html(prep):
    """Inline prep block rendered inside the focus card (and matched by the
    client-side liveFocusHTML below — keep the two in sync)."""
    if not prep:
        return ""
    summary = html.escape(prep.get("summary") or "")
    tasks = prep.get("tasks") or []
    tasks_html = "".join(f"<li>{html.escape(t)}</li>" for t in tasks)
    tasks_block = f'<ul class="focus-prep-tasks">{tasks_html}</ul>' if tasks_html else ""
    summary_block = f'<p class="focus-prep-summary">{summary}</p>' if summary else ""
    return f"""
            <div class="focus-prep">
                <div class="focus-prep-label">Prep</div>
                {summary_block}
                {tasks_block}
            </div>"""


def render_focus_card(events, now, preps=None):
    """HTML for the focus card (currently-running or next-up event); '' if none.

    When ``preps`` is supplied and contains an entry for the focus event's id,
    a prep block is rendered below the title — same data the 06 Meeting Prep
    card uses, just inline so an in-session meeting shows everything at once.
    """
    focus = get_focus_event(events, now)
    if not focus:
        return ""
    f_start = format_event_time(focus)
    f_end = format_event_end_time(focus)
    f_title = html.escape(focus.get("summary") or "Untitled")
    f_loc = html.escape(focus.get("location") or "")
    f_time_str = f"{f_start} &mdash; {f_end}" if f_end else f_start
    f_loc_html = f'<div class="focus-loc">{f_loc}</div>' if f_loc else ""
    start_iso = focus.get("start", {}).get("dateTime", "")
    if start_iso and datetime.fromisoformat(start_iso) <= now:
        label = "Focus &middot; In session"
    else:
        label = "Focus &middot; Next up"
    prep_html = _render_focus_prep_html((preps or {}).get(focus.get("id")))
    return f"""
        <section class="focus-card">
            <div class="eyebrow">{label}</div>
            <div class="focus-time">{f_time_str}</div>
            <h2 class="focus-title">{f_title}</h2>
            {f_loc_html}
            {prep_html}
        </section>"""


def render_meeting_prep(events, preps):
    """HTML rows for the 06 Meeting Prep card — one entry per event whose
    description contained a 'Meeting Prep:' marker, in calendar order."""
    if not preps:
        return '<p class="empty">No meetings with prep notes today.</p>'
    rows = ""
    for e in events:
        prep = preps.get(e.get("id"))
        if not prep:
            continue
        title = html.escape(e.get("summary") or "Untitled")
        start = format_event_time(e)
        end = format_event_end_time(e)
        time_str = f"{start} &mdash; {end}" if end else start
        summary = html.escape(prep.get("summary") or "")
        tasks = prep.get("tasks") or []
        tasks_html = "".join(f"<li>{html.escape(t)}</li>" for t in tasks)
        tasks_block = (
            f'<div class="meeting-label">To prepare</div>'
            f'<ul class="meeting-takeaways">{tasks_html}</ul>'
            if tasks_html else ""
        )
        summary_block = f'<p class="meeting-summary">{summary}</p>' if summary else ""
        rows += f"""
            <div class="meeting">
                <div class="prep-time">{time_str}</div>
                <h3 class="meeting-title">{title}</h3>
                {summary_block}
                {tasks_block}
            </div>"""
    return rows or '<p class="empty">No meetings with prep notes today.</p>'


def render_schedule(events, now):
    """HTML for the timeline rows, or the empty-state paragraph."""
    if not events:
        return '<p class="empty">Nothing on the calendar.</p>'
    rows = ""
    for e in events:
        start = format_event_time(e)
        end = format_event_end_time(e)
        title = html.escape(e.get("summary") or "Untitled")
        location = html.escape(e.get("location") or "")
        loc_html = f'<div class="t-loc">{location}</div>' if location else ""
        end_html = f'<div class="t-end">&mdash; {end}</div>' if end else ""
        rows += f"""
            <div class="t-row">
                <div class="t-time">
                    <div class="t-start">{start}</div>
                    {end_html}
                </div>
                <div class="t-rail"><span></span></div>
                <div class="t-body">
                    <div class="t-title">{title}</div>
                    {loc_html}
                </div>
            </div>"""
    return rows


def render_tasks(tasks, now):
    """HTML for the task rows, or the empty-state paragraph."""
    if not tasks:
        return '<p class="empty">Nothing outstanding.</p>'
    rows = ""
    for t in tasks:
        title = html.escape(t.get("title") or "Untitled")
        due = t.get("due", "")
        list_title = html.escape(t.get("_listTitle") or "")
        due_html = ""
        if due:
            due_dt = datetime.fromisoformat(due[:10]).date()
            delta = (due_dt - now.date()).days
            if delta < 0:
                cls, label = "capsule--decision", f"Overdue &middot; {due_dt.strftime('%b %-d')}"
            elif delta == 0:
                cls, label = "capsule--decision", "Due today"
            elif delta <= 3:
                cls, label = "capsule--caution", f"Due {due_dt.strftime('%b %-d')}"
            else:
                cls, label = "capsule--neutral", f"Due {due_dt.strftime('%b %-d')}"
            due_html = f'<span class="capsule {cls}">{label}</span>'
        rows += f"""
            <div class="task-row">
                <span class="capsule capsule--tinted">{list_title}</span>
                <span class="task-title">{title}</span>
                {due_html}
            </div>"""
    return rows


def _live_event(e, preps=None):
    """Trimmed, pre-formatted event record for the client-side tick layer.

    start_iso/end_iso are null for all-day events (skipped by the now/next
    logic in the browser). Labels are formatted server-side so the page never
    re-formats times — keeping client and server output identical. ``prep``
    carries the meeting-prep summary + tasks so liveFocusHTML can re-render
    the focus card with prep when the in-session meeting changes.
    """
    start, end = e.get("start", {}), e.get("end", {})
    if "dateTime" in start:
        start_iso = start["dateTime"]
        start_label = datetime.fromisoformat(start_iso).strftime("%-I:%M %p")
    else:
        start_iso, start_label = None, "All day"
    if "dateTime" in end:
        end_iso = end["dateTime"]
        end_label = datetime.fromisoformat(end_iso).strftime("%-I:%M %p")
    else:
        end_iso, end_label = None, ""
    return {
        "id": e.get("id") or "",
        "start_iso": start_iso,
        "end_iso": end_iso,
        "start_label": start_label,
        "end_label": end_label,
        "summary": e.get("summary") or "Untitled",
        "location": e.get("location") or "",
        "prep": (preps or {}).get(e.get("id")),
    }


def build_live_data(events, tasks, preps=None):
    """Structured payload for client-side hydration of calendar + open work.

    Carries both pre-rendered HTML (for an instant hydration swap) and a
    structured event list (so the page can recompute the in-session / next-up
    state every ~30s without a server round-trip). Row order in schedule_html
    matches the events list, so the page can highlight the current row by index.
    ``preps`` is keyed by event id and round-trips through live.json so the
    10-min refresh worker (fetch_live.py) doesn't need to re-call Claude.
    """
    now = datetime.now(ZoneInfo(TIMEZONE))
    preps = preps or {}
    return {
        "refreshed_at": now.isoformat(),
        "refreshed_label": now.strftime("%-I:%M %p"),
        "tz_label": TIMEZONE.replace("_", " ").split("/")[-1],
        "events": [_live_event(e, preps) for e in events],
        "focus_html": render_focus_card(events, now, preps),
        "schedule_html": render_schedule(events, now),
        "openwork_html": render_tasks(tasks, now),
        "preps": preps,
    }


def write_live_json(events, tasks, preps=None, path="live.json"):
    """Write live.json. When ``preps`` is omitted, preserve whatever preps the
    previous live.json carried so the cheap 10-min refresh keeps the prep
    block intact without re-running the Claude calls."""
    if preps is None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                preps = json.load(f).get("preps", {}) or {}
        except (FileNotFoundError, json.JSONDecodeError):
            preps = {}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(build_live_data(events, tasks, preps), f, indent=2)


def render_html(events, tasks, ai, meeting_reviews, preps=None):
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    today_full = now.strftime("%A, %B %-d, %Y")
    today_short = now.strftime("%b %-d, %Y").upper()
    generated_at = now.strftime("%-I:%M %p")
    iso_date = now.strftime("%m.%d.%y")
    weekday = now.strftime("%A")
    tz_label = TIMEZONE.replace("_", " ").split("/")[-1]

    focus_card = render_focus_card(events, now, preps)
    schedule_html = render_schedule(events, now)
    tasks_html = render_tasks(tasks, now)
    meeting_prep_html = render_meeting_prep(events, preps or {})

    # ── PLAYS ──────────────────────────────────────────────
    plays_html = "".join(
        f'<li class="play"><span class="play-num">{i:02d}</span><span class="play-text">{s}</span></li>'
        for i, s in enumerate(ai.get("suggestions", []), 1)
    ) or '<li class="empty">No plays generated.</li>'

    # ── YESTERDAY ──────────────────────────────────────────
    if meeting_reviews:
        yesterday_inner = ""
        for review in meeting_reviews:
            takeaways_html = "".join(
                f"<li>{t}</li>" for t in review["takeaways"]
            )
            yesterday_inner += f"""
            <div class="meeting">
                <h3 class="meeting-title">{review["title"]}</h3>
                <p class="meeting-summary">{review["summary"]}</p>
                <div class="meeting-label">Takeaways</div>
                <ul class="meeting-takeaways">{takeaways_html}</ul>
            </div>"""
    else:
        yesterday_inner = '<p class="empty">No meeting notes from the last 24 hours.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Daily Brief &middot; {today_full}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"></script>
    <style>
        :root {{
            /* Navy ramp */
            --navy-950: #000F1E;
            --navy-900: #041628;
            --navy-800: #001F38;
            --navy-700: #112847;
            --navy-600: #1A3358;

            /* Amber accent */
            --amber-400: #FFB845;
            --amber-500: #F5A623;
            --amber-600: #D98E0E;

            /* Semantic */
            --red:    #FF686B;
            --yellow: #FFC93C;

            /* Foreground */
            --off-white: #F7F7F2;
            --bone:      #E8E8E0;
            --muted:     rgba(247, 247, 242, 0.62);
            --quiet:     rgba(247, 247, 242, 0.38);

            /* Surfaces */
            --card-bg:     rgba(245, 166, 35, 0.045);
            --card-border: rgba(245, 166, 35, 0.14);
            --hairline:    rgba(247, 247, 242, 0.06);

            /* Type scale */
            --fs-display: 78px;
            --fs-h1:      50px;
            --fs-h2:      28px;
            --fs-h3:      21px;
            --fs-body:    16px;
            --fs-eyebrow: 13px;
            --fs-micro:   11px;

            /* Radii */
            --r-xs:  4px; --r-sm:  6px; --r-md: 10px;
            --r-lg: 14px; --r-xl: 22px;

            /* 8pt spacing */
            --s-1:  4px; --s-2:  8px; --s-3: 12px; --s-4: 16px;
            --s-5: 24px; --s-6: 32px; --s-7: 48px; --s-8: 64px;

            --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            --font-mono: 'JetBrains Mono', 'Consolas', 'Monaco', monospace;
        }}

        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
        html {{ scroll-behavior: smooth; }}

        body {{
            font-family: var(--font-sans);
            font-size: var(--fs-body);
            line-height: 1.55;
            color: var(--off-white);
            background: var(--navy-900);
            background-image:
                radial-gradient(ellipse 65% 55% at 88% -8%, rgba(245, 166, 35, 0.10), transparent 60%),
                radial-gradient(ellipse 80% 40% at -10% 100%, rgba(26, 51, 88, 0.55), transparent 60%);
            background-attachment: fixed;
            min-height: 100vh;
            padding: var(--s-7) var(--s-4) var(--s-8);
            -webkit-font-smoothing: antialiased;
            overflow-x: hidden;
        }}

        .container {{ max-width: 1180px; margin: 0 auto; position: relative; }}

        /* ── Grids ────────────────────────────────────── */
        .grid-hero, .grid-2 {{
            display: grid;
            gap: var(--s-4);
            grid-template-columns: 1fr;
            margin-bottom: var(--s-4);
        }}
        .grid-hero:has(.focus-card) {{ grid-template-columns: 5fr 7fr; }}
        .grid-2 {{ grid-template-columns: 5fr 7fr; }}
        .grid-hero > .card, .grid-hero > .focus-card,
        .grid-2 > .card {{ margin-bottom: 0; height: 100%; }}
        @media (max-width: 960px) {{
            .grid-hero:has(.focus-card), .grid-2 {{ grid-template-columns: 1fr; }}
        }}

        /* Header main split — title block on the left, Overview card on the right */
        .header-main {{
            display: grid;
            grid-template-columns: 5fr 7fr;
            gap: var(--s-6);
            align-items: end;
            margin-bottom: var(--s-6);
        }}
        .header-main > .card {{ margin-bottom: 0; }}
        @media (max-width: 960px) {{
            .header-main {{ grid-template-columns: 1fr; gap: var(--s-5); }}
        }}

        /* Faint grid backdrop */
        .container::before {{
            content: '';
            position: fixed;
            inset: 0;
            background-image:
                linear-gradient(rgba(245, 166, 35, 0.025) 1px, transparent 1px),
                linear-gradient(90deg, rgba(245, 166, 35, 0.025) 1px, transparent 1px);
            background-size: 64px 64px;
            pointer-events: none;
            z-index: -1;
            mask-image: radial-gradient(ellipse 80% 60% at 50% 30%, #000 30%, transparent 75%);
        }}

        /* ── Header ────────────────────────────────────── */
        header {{ margin-bottom: var(--s-7); }}
        .header-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: var(--s-7);
        }}
        .logo {{
            display: flex;
            align-items: center;
            gap: var(--s-3);
            opacity: 0;
        }}
        .logo-chip {{
            filter: drop-shadow(0 8px 22px rgba(245, 166, 35, 0.28));
        }}
        /* Bars are visible by default — GSAP animates them in from scaleY(0). */
        /* If JS fails to load, the F mark still renders correctly. */
        .logo-wordmark {{
            font-family: var(--font-sans);
            font-size: 18px;
            font-weight: 800;
            color: var(--off-white);
            letter-spacing: -0.015em;
        }}
        .logo-wordmark .dot {{ color: var(--amber-500); }}

        .clock {{
            font-family: var(--font-mono);
            font-size: var(--fs-eyebrow);
            font-weight: 500;
            color: var(--muted);
            letter-spacing: 0.04em;
            opacity: 0;
        }}
        .clock .tz {{
            color: var(--quiet);
            margin-left: 6px;
            font-size: var(--fs-micro);
        }}

        .hero-eyebrow {{
            font-family: var(--font-mono);
            font-size: var(--fs-eyebrow);
            font-weight: 600;
            color: var(--amber-500);
            text-transform: uppercase;
            letter-spacing: 0.18em;
            margin-bottom: var(--s-4);
            opacity: 0;
            transform: translateY(14px);
        }}
        .display-wrap {{
            overflow: hidden;
            padding: 0 0 10px;
        }}
        .display {{
            font-family: var(--font-sans);
            font-size: var(--fs-display);
            font-weight: 800;
            line-height: 0.95;
            letter-spacing: -0.02em;
            color: var(--off-white);
            transform: translateY(112%);
        }}
        .display .dot {{ color: var(--amber-500); }}
        .display .day {{
            display: block;
            font-size: var(--fs-h3);
            font-weight: 500;
            letter-spacing: 0;
            color: var(--muted);
            margin-top: var(--s-3);
        }}
        .hero-sub {{
            font-family: var(--font-mono);
            font-size: var(--fs-micro);
            font-weight: 500;
            color: var(--quiet);
            text-transform: uppercase;
            letter-spacing: 0.18em;
            margin-top: var(--s-5);
            opacity: 0;
            transform: translateY(8px);
        }}
        .header-rule {{
            margin-top: var(--s-6);
            border: none;
            border-top: 1px solid var(--hairline);
        }}

        /* ── Eyebrows ──────────────────────────────────── */
        .eyebrow {{
            font-family: var(--font-mono);
            font-size: var(--fs-eyebrow);
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.18em;
            color: var(--amber-500);
        }}

        /* ── Cards ─────────────────────────────────────── */
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: var(--r-lg);
            padding: var(--s-6) var(--s-6);
            margin-bottom: var(--s-4);
            opacity: 0;
            transform: translateY(24px);
        }}
        .card-head {{
            display: flex;
            align-items: baseline;
            gap: var(--s-3);
            margin-bottom: var(--s-5);
        }}
        .card-num {{
            font-family: var(--font-mono);
            font-size: var(--fs-eyebrow);
            font-weight: 600;
            color: var(--amber-500);
        }}
        .live-stamp {{
            margin-left: auto;
            font-family: var(--font-mono);
            font-size: var(--fs-micro);
            font-weight: 500;
            letter-spacing: 0.04em;
            color: var(--quiet);
            white-space: nowrap;
        }}

        /* ── Focus card ────────────────────────────────── */
        .focus-card {{
            position: relative;
            background:
                linear-gradient(135deg, rgba(245, 166, 35, 0.10), rgba(245, 166, 35, 0.02) 70%),
                var(--navy-800);
            border: 1px solid rgba(245, 166, 35, 0.40);
            border-radius: var(--r-lg);
            padding: var(--s-6);
            margin-bottom: var(--s-5);
            opacity: 0;
            transform: translateY(24px);
            animation: pulse-amber 3.8s ease-in-out 1.6s infinite;
        }}
        .focus-card::before {{
            content: '';
            position: absolute;
            left: 0; top: var(--s-5); bottom: var(--s-5);
            width: 3px;
            background: var(--amber-500);
            border-radius: 0 2px 2px 0;
            box-shadow: 0 0 14px rgba(245, 166, 35, 0.6);
        }}
        .focus-card .eyebrow {{
            display: block;
            margin-bottom: var(--s-4);
        }}
        .focus-time {{
            font-family: var(--font-mono);
            font-size: var(--fs-h3);
            font-weight: 500;
            color: var(--amber-400);
            letter-spacing: -0.005em;
            margin-bottom: var(--s-2);
        }}
        .focus-title {{
            font-size: var(--fs-h2);
            font-weight: 700;
            line-height: 1.15;
            letter-spacing: -0.015em;
            color: var(--off-white);
            margin-bottom: var(--s-3);
        }}
        .focus-loc {{
            font-size: var(--fs-body);
            color: var(--muted);
        }}
        @keyframes pulse-amber {{
            0%, 100% {{ box-shadow: 0 0 24px rgba(245, 166, 35, 0.10); }}
            50%      {{ box-shadow: 0 0 44px rgba(245, 166, 35, 0.26); }}
        }}

        /* ── Overview ──────────────────────────────────── */
        .overview {{
            font-size: var(--fs-h3);
            line-height: 1.5;
            font-weight: 400;
            color: var(--bone);
            letter-spacing: -0.005em;
        }}

        /* ── Plays ─────────────────────────────────────── */
        .plays {{ list-style: none; }}
        .play {{
            display: flex;
            align-items: baseline;
            gap: var(--s-4);
            padding: var(--s-3) 0;
            border-bottom: 1px solid var(--hairline);
        }}
        .play:first-child {{ padding-top: 0; }}
        .play:last-child {{ border-bottom: none; padding-bottom: 0; }}
        .play-num {{
            font-family: var(--font-mono);
            font-size: var(--fs-eyebrow);
            font-weight: 600;
            color: var(--amber-500);
            min-width: 28px;
            flex-shrink: 0;
        }}
        .play-text {{
            font-size: var(--fs-body);
            line-height: 1.55;
            color: var(--off-white);
        }}

        /* ── Timeline ──────────────────────────────────── */
        .t-row {{
            display: grid;
            grid-template-columns: 100px 18px 1fr;
            gap: var(--s-4);
            padding: var(--s-4) 0;
            border-bottom: 1px solid var(--hairline);
        }}
        .t-row:first-child {{ padding-top: 0; }}
        .t-row:last-child {{ border-bottom: none; padding-bottom: 0; }}
        .t-time {{ font-family: var(--font-mono); text-align: right; }}
        .t-start {{
            font-size: var(--fs-body);
            font-weight: 600;
            color: var(--amber-400);
            letter-spacing: -0.005em;
        }}
        .t-end {{
            font-size: var(--fs-eyebrow);
            color: var(--muted);
            margin-top: 2px;
        }}
        .t-rail {{ position: relative; width: 18px; }}
        .t-rail::before {{
            content: '';
            position: absolute;
            left: 50%; top: 0; bottom: -16px;
            width: 1px;
            background: rgba(245, 166, 35, 0.22);
            transform: translateX(-50%);
        }}
        .t-row:last-child .t-rail::before {{ bottom: 50%; }}
        .t-rail span {{
            position: absolute;
            left: 50%; top: 8px;
            width: 8px; height: 8px;
            border-radius: 50%;
            background: var(--amber-500);
            transform: translateX(-50%);
            box-shadow: 0 0 0 4px var(--navy-900), 0 0 14px rgba(245, 166, 35, 0.55);
        }}
        .t-title {{
            font-size: var(--fs-body);
            font-weight: 500;
            color: var(--off-white);
        }}
        .t-loc {{
            font-size: var(--fs-eyebrow);
            color: var(--muted);
            margin-top: 4px;
        }}
        /* Current meeting — set by the client tick layer every ~30s. */
        .t-row.is-now {{
            background: linear-gradient(90deg, rgba(245, 166, 35, 0.11), rgba(245, 166, 35, 0.02));
            border-radius: var(--r-md);
            border-bottom-color: transparent;
        }}
        .t-row.is-now .t-start {{ color: var(--amber-400); }}
        .t-row.is-now .t-title {{ font-weight: 600; }}
        .t-row.is-now .t-title::after {{
            content: ' · NOW';
            font-family: var(--font-mono);
            font-size: var(--fs-micro);
            font-weight: 600;
            letter-spacing: 0.14em;
            color: var(--amber-400);
        }}
        .t-row.is-now .t-rail span {{
            width: 11px; height: 11px;
            background: var(--amber-400);
            animation: now-pulse 2.2s ease-in-out infinite;
        }}
        @keyframes now-pulse {{
            0%, 100% {{ box-shadow: 0 0 0 4px var(--navy-900), 0 0 0 6px rgba(245, 166, 35, 0.28), 0 0 16px rgba(245, 166, 35, 0.55); }}
            50%      {{ box-shadow: 0 0 0 4px var(--navy-900), 0 0 0 9px rgba(245, 166, 35, 0.10), 0 0 26px rgba(245, 166, 35, 0.85); }}
        }}

        /* ── Tasks ─────────────────────────────────────── */
        .task-row {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: var(--s-3);
            padding: var(--s-4) 0;
            border-bottom: 1px solid var(--hairline);
        }}
        .task-row:first-child {{ padding-top: 0; }}
        .task-row:last-child {{ border-bottom: none; padding-bottom: 0; }}
        .task-title {{
            font-size: var(--fs-body);
            font-weight: 500;
            color: var(--off-white);
            flex: 1;
            min-width: 0;
        }}

        /* ── Capsules ──────────────────────────────────── */
        .capsule {{
            display: inline-flex;
            align-items: center;
            font-family: var(--font-sans);
            font-size: var(--fs-micro);
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            padding: 4px 10px;
            border-radius: 999px;
            flex-shrink: 0;
            white-space: nowrap;
        }}
        .capsule--tinted   {{ background: rgba(245, 166, 35, 0.14); color: var(--amber-400); }}
        .capsule--neutral  {{ background: rgba(247, 247, 242, 0.08); color: var(--muted); }}
        .capsule--caution  {{ background: rgba(255, 201, 60, 0.15); color: var(--yellow); }}
        .capsule--decision {{ background: rgba(255, 104, 107, 0.15); color: var(--red); }}

        /* ── Meetings ──────────────────────────────────── */
        .meeting {{
            padding: var(--s-5) 0;
            border-bottom: 1px solid var(--hairline);
        }}
        .meeting:first-child {{ padding-top: 0; }}
        .meeting:last-child {{ border-bottom: none; padding-bottom: 0; }}
        .meeting-title {{
            font-size: var(--fs-h3);
            font-weight: 600;
            color: var(--off-white);
            letter-spacing: -0.01em;
            margin-bottom: var(--s-2);
        }}
        .meeting-summary {{
            font-size: var(--fs-body);
            line-height: 1.6;
            color: var(--bone);
            margin-bottom: var(--s-4);
        }}
        .meeting-label {{
            font-family: var(--font-mono);
            font-size: var(--fs-micro);
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.18em;
            color: var(--amber-500);
            margin-bottom: var(--s-3);
        }}
        .meeting-takeaways {{ list-style: none; }}
        .meeting-takeaways li {{
            font-size: var(--fs-body);
            line-height: 1.55;
            color: var(--bone);
            padding: var(--s-2) 0 var(--s-2) 22px;
            position: relative;
        }}
        .meeting-takeaways li::before {{
            content: '›';
            position: absolute;
            left: 4px;
            top: var(--s-2);
            color: var(--amber-500);
            font-family: var(--font-mono);
            font-weight: 700;
        }}

        /* ── Focus prep (inline inside focus-card) ─────── */
        .focus-prep {{
            margin-top: var(--s-5);
            padding-top: var(--s-4);
            border-top: 1px solid rgba(245, 166, 35, 0.18);
        }}
        .focus-prep-label {{
            font-family: var(--font-mono);
            font-size: var(--fs-micro);
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.18em;
            color: var(--amber-500);
            margin-bottom: var(--s-3);
        }}
        .focus-prep-summary {{
            font-size: var(--fs-body);
            line-height: 1.55;
            color: var(--bone);
            margin-bottom: var(--s-3);
        }}
        .focus-prep-tasks {{ list-style: none; }}
        .focus-prep-tasks li {{
            font-size: var(--fs-eyebrow);
            line-height: 1.5;
            color: var(--bone);
            padding: 4px 0 4px 20px;
            position: relative;
        }}
        .focus-prep-tasks li::before {{
            content: '›';
            position: absolute;
            left: 4px;
            top: 4px;
            color: var(--amber-500);
            font-family: var(--font-mono);
            font-weight: 700;
        }}

        /* ── Meeting prep card (06) ────────────────────── */
        .prep-time {{
            font-family: var(--font-mono);
            font-size: var(--fs-eyebrow);
            font-weight: 500;
            color: var(--amber-400);
            margin-bottom: var(--s-1);
            letter-spacing: -0.005em;
        }}

        /* ── Empty state ───────────────────────────────── */
        .empty {{
            font-family: var(--font-mono);
            font-size: var(--fs-eyebrow);
            color: var(--quiet);
            text-transform: uppercase;
            letter-spacing: 0.14em;
        }}

        /* ── Footer ────────────────────────────────────── */
        footer {{
            font-family: var(--font-mono);
            text-align: center;
            color: var(--quiet);
            font-size: var(--fs-micro);
            margin-top: var(--s-7);
            text-transform: uppercase;
            letter-spacing: 0.18em;
        }}

        @media (max-width: 540px) {{
            :root {{ --fs-display: 52px; --fs-h2: 23px; --fs-h3: 18px; }}
            .card, .focus-card {{ padding: var(--s-5); }}
            .t-row {{ grid-template-columns: 76px 14px 1fr; gap: var(--s-3); }}
            .display .day {{ font-size: 15px; }}
        }}

        @media (prefers-reduced-motion: reduce) {{
            .focus-card {{ animation: none; }}
            .logo, .clock, .hero-eyebrow, .hero-sub, .card, .focus-card {{
                opacity: 1; transform: none;
            }}
            .display {{ transform: none; }}
            .logo-chip .bar {{ transform: none; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-top">
                <div class="logo">
                    <svg class="logo-chip" width="44" height="44" viewBox="0 0 44 44" fill="none" aria-hidden="true">
                        <rect width="44" height="44" rx="10" fill="#F5A623"/>
                        <g fill="#041628">
                            <rect class="bar" x="11" y="12" width="22" height="5" rx="1.2"/>
                            <rect class="bar" x="11" y="20" width="15" height="5" rx="1.2"/>
                            <rect class="bar" x="11" y="28" width="8"  height="5" rx="1.2"/>
                        </g>
                    </svg>
                    <span class="logo-wordmark">Francis<span class="dot">.</span></span>
                </div>
                <div class="clock" id="clock">--:--:-- --<span class="tz">{tz_label}</span></div>
            </div>

            <div class="header-main">
                <div class="header-titles">
                    <div class="hero-eyebrow">Daily Brief &middot; {today_short}</div>
                    <div class="display-wrap">
                        <h1 class="display">Today<span class="dot">.</span><span class="day">{weekday} &middot; <span id="js-refreshed" title="Calendar &amp; tasks auto-refresh every ~10 min">updated {generated_at}</span></span></h1>
                    </div>
                </div>
                <section class="card">
                    <div class="card-head">
                        <span class="eyebrow">Overview</span>
                    </div>
                    <p class="overview">{ai.get("overview", "")}</p>
                </section>
            </div>
            <hr class="header-rule">
        </header>

        <div class="grid-hero">
            {focus_card}
            <section class="card">
                <div class="card-head">
                    <span class="card-num">01</span>
                    <span class="eyebrow">Schedule</span>
                    <span class="live-stamp" title="Calendar auto-refreshes every 10 minutes">&#8635; {generated_at}</span>
                </div>
                <div class="timeline" id="js-schedule">{schedule_html}</div>
            </section>
        </div>

        <div class="grid-2">
            <section class="card">
                <div class="card-head">
                    <span class="card-num">02</span>
                    <span class="eyebrow">Plays</span>
                </div>
                <ol class="plays">{plays_html}</ol>
            </section>

            <section class="card">
                <div class="card-head">
                    <span class="card-num">03</span>
                    <span class="eyebrow">Open work</span>
                    <span class="live-stamp" title="Open work auto-refreshes every 10 minutes">&#8635; {generated_at}</span>
                </div>
                <div class="tasks" id="js-openwork">{tasks_html}</div>
            </section>
        </div>

        <div class="grid-2">
            <section class="card">
                <div class="card-head">
                    <span class="card-num">04</span>
                    <span class="eyebrow">Yesterday</span>
                </div>
                {yesterday_inner}
            </section>

            <section class="card">
                <div class="card-head">
                    <span class="card-num">05</span>
                    <span class="eyebrow">Meeting Prep</span>
                </div>
                {meeting_prep_html}
            </section>
        </div>

        <footer>Francis Inc &middot; Daily Brief v1.0 &middot; {iso_date}</footer>
    </div>

    <script>
        // Live clock — JetBrains Mono, matches brand voice
        const clockEl = document.getElementById('clock');
        const tzLabel = clockEl ? clockEl.querySelector('.tz') : null;
        const tzText = tzLabel ? tzLabel.outerHTML : '';
        function pad(n) {{ return String(n).padStart(2, '0'); }}
        function tick() {{
            if (!clockEl) return;
            const d = new Date();
            let h = d.getHours();
            const m = pad(d.getMinutes());
            const s = pad(d.getSeconds());
            const ampm = h >= 12 ? 'PM' : 'AM';
            h = h % 12 || 12;
            clockEl.innerHTML = h + ':' + m + ':' + s + ' ' + ampm + tzText;
        }}
        tick();
        setInterval(tick, 1000);

        // Entrance choreography
        const tl = gsap.timeline({{ defaults: {{ ease: 'power3.out' }} }});
        tl.to('.logo',           {{ opacity: 1, duration: 0.45 }})
          .from('.logo-chip',    {{ scale: 0.4, opacity: 0, transformOrigin: '50% 50%', duration: 0.6, ease: 'back.out(2)' }}, '-=0.35')
          .to('.clock',          {{ opacity: 1, duration: 0.35 }}, '-=0.4')
          .to('.hero-eyebrow',   {{ opacity: 1, y: 0, duration: 0.5 }}, '-=0.2')
          .to('.display',        {{ y: 0, duration: 0.95, ease: 'expo.out' }}, '-=0.35')
          .to('.hero-sub',       {{ opacity: 1, y: 0, duration: 0.4 }}, '-=0.55')
          .to('.focus-card',     {{ opacity: 1, y: 0, duration: 0.65 }}, '-=0.3')
          .to('.card',           {{ opacity: 1, y: 0, duration: 0.55, stagger: 0.09 }}, '-=0.45');

        // ── Live layer ──────────────────────────────────────────────────
        // The daily build embeds today's data; a separate workflow rewrites
        // live.json every ~10 min with just calendar + tasks (no AI calls).
        // Between those refreshes a 30s tick recomputes — from the local clock —
        // which meeting is in session (the "NOW" row) and what's next up (the
        // focus card), so both stay current without a server round-trip.
        var liveEvents = [];
        var liveLoaded = false;
        var shownFocusKey = null;

        function liveEsc(s) {{
            return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {{
                return {{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }}[c];
            }});
        }}
        function liveParse(iso) {{ return iso ? new Date(iso) : null; }}
        function liveFocusKey(f) {{
            if (!f) return '';
            var prepKey = f.ev.prep ? '1' : '0';
            return (f.ev.start_iso || '') + '|' + (f.ev.summary || '') + '|' + (f.inSession ? '1' : '0') + '|' + prepKey;
        }}
        function liveFindFocus(events, now) {{
            var upcoming = [];
            for (var i = 0; i < events.length; i++) {{
                var e = events[i];
                if (!e.start_iso) continue;
                var s = liveParse(e.start_iso), en = liveParse(e.end_iso);
                if (en && s <= now && now <= en) return {{ ev: e, inSession: true }};
                if (s > now) upcoming.push(e);
            }}
            if (upcoming.length) {{
                upcoming.sort(function (a, b) {{ return liveParse(a.start_iso) - liveParse(b.start_iso); }});
                return {{ ev: upcoming[0], inSession: false }};
            }}
            return null;
        }}
        function liveFocusHTML(f) {{
            var e = f.ev;
            var timeStr = e.end_label ? (e.start_label + ' \\u2014 ' + e.end_label) : e.start_label;
            var label = f.inSession ? 'Focus \\u00B7 In session' : 'Focus \\u00B7 Next up';
            var loc = e.location ? '<div class="focus-loc">' + liveEsc(e.location) + '</div>' : '';
            var prep = '';
            if (e.prep) {{
                var p = e.prep;
                var summary = p.summary ? '<p class="focus-prep-summary">' + liveEsc(p.summary) + '</p>' : '';
                var tasksHtml = '';
                if (p.tasks && p.tasks.length) {{
                    for (var i = 0; i < p.tasks.length; i++) {{
                        tasksHtml += '<li>' + liveEsc(p.tasks[i]) + '</li>';
                    }}
                    tasksHtml = '<ul class="focus-prep-tasks">' + tasksHtml + '</ul>';
                }}
                prep = '<div class="focus-prep">'
                     + '<div class="focus-prep-label">Prep</div>'
                     + summary + tasksHtml
                     + '</div>';
            }}
            return '<section class="focus-card">'
                 + '<div class="eyebrow">' + label + '</div>'
                 + '<div class="focus-time">' + liveEsc(timeStr) + '</div>'
                 + '<h2 class="focus-title">' + liveEsc(e.summary || 'Untitled') + '</h2>'
                 + loc + prep + '</section>';
        }}
        function liveApplyFocus(now) {{
            if (!liveLoaded) return;
            var grid = document.querySelector('.grid-hero');
            if (!grid) return;
            var f = liveFindFocus(liveEvents, now);
            var key = liveFocusKey(f);
            if (key === shownFocusKey) return;          // unchanged — leave the DOM alone
            shownFocusKey = key;
            var fc = grid.querySelector('.focus-card');
            if (f) {{
                var markup = liveFocusHTML(f);
                if (fc) fc.outerHTML = markup; else grid.insertAdjacentHTML('afterbegin', markup);
                fc = grid.querySelector('.focus-card');
                if (fc) {{ fc.style.opacity = '1'; fc.style.transform = 'none'; }}
            }} else if (fc) {{
                fc.remove();
            }}
        }}
        function liveMarkNow(now) {{
            if (!liveLoaded) return;
            var rows = document.querySelectorAll('#js-schedule .t-row');
            if (!rows.length) return;
            var nowIdx = -1;
            for (var i = 0; i < liveEvents.length; i++) {{
                var e = liveEvents[i];
                if (!e.start_iso) continue;
                var s = liveParse(e.start_iso), en = liveParse(e.end_iso);
                if (en && s <= now && now <= en) {{ nowIdx = i; break; }}
            }}
            for (var j = 0; j < rows.length; j++) rows[j].classList.toggle('is-now', j === nowIdx);
        }}
        function liveTick() {{
            var now = new Date();
            liveMarkNow(now);
            liveApplyFocus(now);
        }}

        async function hydrateLive() {{
            try {{
                var d;
                if (window.__BRIEF_LIVE__) {{
                    d = window.__BRIEF_LIVE__;
                }} else {{
                    var res = await fetch('live.json?t=' + Date.now(), {{ cache: 'no-store' }});
                    if (!res.ok) return;
                    d = await res.json();
                }}
                var sched = document.getElementById('js-schedule');
                if (sched && typeof d.schedule_html === 'string') sched.innerHTML = d.schedule_html;
                var work = document.getElementById('js-openwork');
                if (work && typeof d.openwork_html === 'string') work.innerHTML = d.openwork_html;
                var grid = document.querySelector('.grid-hero');
                if (grid && typeof d.focus_html === 'string') {{
                    var fc = grid.querySelector('.focus-card');
                    if (d.focus_html.trim()) {{
                        if (fc) fc.outerHTML = d.focus_html; else grid.insertAdjacentHTML('afterbegin', d.focus_html);
                        fc = grid.querySelector('.focus-card');
                        if (fc) {{ fc.style.opacity = '1'; fc.style.transform = 'none'; }}
                    }} else if (fc) {{
                        fc.remove();
                    }}
                }}
                if (d.refreshed_label) {{
                    document.querySelectorAll('.live-stamp').forEach(function (el) {{
                        el.textContent = '\\u21BB ' + d.refreshed_label;
                    }});
                    var r = document.getElementById('js-refreshed');
                    if (r) r.textContent = 'updated ' + d.refreshed_label;
                }}
                liveEvents = Array.isArray(d.events) ? d.events : [];
                liveLoaded = true;
                // Match what the server just rendered, so the immediate tick only
                // re-renders the focus card if it has actually drifted since then.
                shownFocusKey = liveFocusKey(liveFindFocus(liveEvents, d.refreshed_at ? new Date(d.refreshed_at) : new Date()));
                liveTick();
            }} catch (e) {{ /* keep the server-rendered content on any failure */ }}
        }}

        setTimeout(hydrateLive, 2800);          // after the entrance animations
        setInterval(hydrateLive, 120000);       // re-fetch live.json every 2 min
        setInterval(liveTick, 30000);           // recompute now/next every 30 s
    </script>
</body>
</html>"""


def main():
    creds = get_google_credentials()
    events = get_todays_events(creds)
    tasks = get_outstanding_tasks(creds)
    meeting_notes = get_meeting_notes(creds)
    preps = build_meeting_preps(creds, events)
    context = build_context(events, tasks)
    ai = get_ai_summary(context)
    meeting_reviews = get_ai_meeting_reviews(meeting_notes)
    html = render_html(events, tasks, ai, meeting_reviews, preps)

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    # Seed live.json with fresh preps so the 10-min refresh worker preserves
    # them without re-running Claude.
    write_live_json(events, tasks, preps=preps)
    print(f"Generated index.html ({len(preps)} meeting prep entries)")


if __name__ == "__main__":
    main()
