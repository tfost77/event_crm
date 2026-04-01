"""
run_lead_intake.py
------------------
Diamondback Brewing Co — Inbound Event Lead Intake

Pipeline:
  1. Connect to Gmail via OAuth and fetch unread emails from Webflow form submissions
  2. Parse each email into a structured lead record
  3. Append new leads to the Google Sheets CRM tab
  4. Check Google Calendar for conflicts on the requested event date (when enabled)
  5. Send a template reply email with pricing PDF attached (when enabled)
  6. Record processed IDs to prevent duplicates

Subject lines monitored:
  - "New form submission for Timonium Form 2"     → location: Timonium
  - "New form submission for Locust Point Form 2" → location: Locust Point

Usage:
  python tools/run_lead_intake.py

Schedule via cron (every hour, 8am–8pm):
  0 8-20 * * * cd /path/to/project && python tools/run_lead_intake.py >> .tmp/lead_intake.log 2>&1

Reply sending is gated by ENABLE_REPLY_SEND in .env. Set to "true" to activate.
"""

import os
import json
import re
import base64
import datetime
import time
from html.parser import HTMLParser
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders as email_encoders
import email.utils
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from googleapiclient.errors import HttpError

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Configuration ────────────────────────────────────────────────────────────

load_dotenv()

SHEET_ID       = os.getenv("GOOGLE_SHEET_ID")
CRM_TAB        = os.getenv("CRM_TAB_NAME", "CRM")
CREDENTIALS      = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
INTAKE_START     = os.getenv("LEAD_INTAKE_START_DATE", "2026/03/08")  # only import emails on/after this date
TOKEN_FILE       = "token.json"           # info@diamondbackbeer.com — read Gmail, Sheets, Calendar
SEND_CREDENTIALS = os.getenv("SEND_CREDENTIALS_FILE", "credentials.json")
SEND_TOKEN_FILE  = "token_send.json"      # reservations@diamondbackbeer.com — send only
PROCESSED_FILE   = Path(".tmp/processed_emails.json")

# Reply sending configuration — gated by ENABLE_REPLY_SEND
LP_CALENDAR_ID      = os.getenv("LP_CALENDAR_ID", "")
TIM_CALENDAR_ID     = os.getenv("TIM_CALENDAR_ID", "")
REPLY_FROM          = os.getenv("REPLY_FROM", "reservations@diamondbackbeer.com")
CUSTOMER_PRICING_LP  = os.getenv("CUSTOMER_PRICING_LP",  "docs/Customer Pricing - Locust Point.pdf")
CUSTOMER_PRICING_TIM = os.getenv("CUSTOMER_PRICING_TIM", "docs/Customer Pricing - TIMONIUM.pdf")
ENABLE_REPLY_SEND   = os.getenv("ENABLE_REPLY_SEND", "false").lower() == "true"

# info@diamondbackbeer.com — reads leads, writes to Sheets, checks Calendar
MAIN_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar.readonly",
]

# reservations@diamondbackbeer.com — sends replies only
SEND_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
]

SUBJECT_MAP = {
    "New form submission for Timonium Form 2":     "Timonium",
    "New form submission for Locust Point Form 2": "Locust Point",
}

# 2-hour pricing tables (both locations use the same rates)
GUEST_BRACKETS   = [(10, 20), (21, 30), (31, 40), (41, 50), (51, 60)]
WEEKNIGHT_2HR    = [500,  750,  1050, 1350, 1650]
WEEKEND_2HR      = [600,  1000, 1400, 1800, 2200]
WEEKNIGHT_4HR    = [900,  1400, 2000, 2600, 3200]
WEEKEND_4HR      = [1000, 1650, 2350, 3050, 3750]

CRM_HEADERS = [
    "Lead ID", "Date Received", "Time Received", "Location", "First Name", "Last Name",
    "Email", "Phone", "Event Date", "Day of Week", "Day Type",
    "Start Time", "End Time", "Duration (hrs)", "Estimated Attendance",
    "Est. Price (2hr)", "Est. Price (4hr)", "# of Pizzas",
    "Event Details", "Status", "Notes", "Gmail Message ID", "Reply Sent", "Follow-up Sent",
]

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_google_credentials():
    """Authorize info@diamondbackbeer.com — Gmail read, Sheets, Calendar."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, MAIN_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, MAIN_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds

def get_send_credentials():
    """Authorize reservations@diamondbackbeer.com — Gmail send only."""
    creds = None
    if os.path.exists(SEND_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(SEND_TOKEN_FILE, SEND_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            print("\n  Authorizing send account (reservations@diamondbackbeer.com)...")
            flow = InstalledAppFlow.from_client_secrets_file(SEND_CREDENTIALS, SEND_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(SEND_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds

# ── Gmail ─────────────────────────────────────────────────────────────────────

def fetch_new_lead_emails(gmail):
    """Fetch unread emails matching our subject lines. Returns list of message dicts."""
    # Search by subject and sender, limited to emails on/after INTAKE_START_DATE.
    # This prevents historical leads from being imported — only new submissions are captured.
    # Deduplication is also handled by processed_emails.json and the sheet's Message ID column.
    subject_query = " OR ".join(f'subject:"{s}"' for s in SUBJECT_MAP)
    query = f"from:no-reply-forms@webflow.com after:{INTAKE_START} ({subject_query})"

    result = gmail.users().messages().list(userId="me", q=query).execute()
    messages = result.get("messages", [])

    processed = load_processed_ids()
    leads = []

    for msg_ref in messages:
        msg_id = msg_ref["id"]
        if msg_id in processed:
            continue

        # Retry up to 3 times on transient 5xx errors
        for attempt in range(3):
            try:
                msg = gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()
                leads.append({"id": msg_id, "message": msg})
                break
            except HttpError as e:
                if e.resp.status >= 500 and attempt < 2:
                    wait = 2 ** attempt  # 1s, 2s
                    print(f"  Gmail API error (attempt {attempt+1}/3), retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"  Failed to fetch message {msg_id} after 3 attempts: {e}")

    print(f"Found {len(leads)} new lead email(s).")
    return leads

class _HTMLTextExtractor(HTMLParser):
    """Strips HTML tags and converts <br> to newlines for field parsing."""
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("br", "p", "div", "tr", "td", "li"):
            self.parts.append("\n")

    def get_text(self):
        return "".join(self.parts)

def _strip_html(raw):
    parser = _HTMLTextExtractor()
    parser.feed(raw)
    return parser.get_text()

def get_email_body(message):
    """Extract plain text body from a Gmail message, stripping HTML if needed."""
    payload = message.get("payload", {})

    def decode_part(part):
        data = part.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    # Single-part message
    if "parts" not in payload:
        raw = decode_part(payload)
        return _strip_html(raw) if "<" in raw else raw

    # Multi-part: prefer text/plain, fall back to text/html
    plain = next((decode_part(p) for p in payload["parts"] if p.get("mimeType") == "text/plain"), None)
    if plain:
        return plain

    html = next((decode_part(p) for p in payload["parts"] if p.get("mimeType") == "text/html"), None)
    if html:
        return _strip_html(html)

    return decode_part(payload["parts"][0])

def get_email_subject(message):
    headers = message.get("payload", {}).get("headers", [])
    for h in headers:
        if h["name"].lower() == "subject":
            return h["value"]
    return ""

def get_email_date(message):
    headers = message.get("payload", {}).get("headers", [])
    for h in headers:
        if h["name"].lower() == "date":
            return h["value"]
    return ""

def mark_as_read(gmail, msg_id):
    # Intentionally a no-op. Gmail scope is read-only for inbox management —
    # deduplication is handled entirely via .tmp/processed_emails.json.
    pass

# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_field(body, label):
    """Extract a value from 'Label: value' lines in the email body."""
    pattern = rf"{re.escape(label)}:[ \t]*(.+)"
    match = re.search(pattern, body, re.IGNORECASE)
    return match.group(1).strip() if match else ""

def parse_lead(msg_id, message):
    """Parse a Gmail message into a structured lead dict."""
    subject  = get_email_subject(message)
    body     = get_email_body(message)
    raw_date = get_email_date(message)

    location = next((v for k, v in SUBJECT_MAP.items() if k.lower() in subject.lower()), "Unknown")

    # Parse date received — convert to Eastern time, split into date and time columns
    try:
        dt = email.utils.parsedate_to_datetime(raw_date)
        dt_eastern = dt.astimezone(ZoneInfo("America/New_York"))
        date_received_date = dt_eastern.strftime("%m/%d/%Y")
        date_received_time = to_12hr(dt_eastern.strftime("%H:%M"))
    except Exception:
        date_received_date = raw_date
        date_received_time = ""

    first_name   = parse_field(body, "First Name")
    last_name    = parse_field(body, "Last Name")
    email        = parse_field(body, "Email Address")
    phone        = parse_field(body, "Phone Number")
    event_date_raw = parse_field(body, "Field 5")  # YYYY-MM-DD from Webflow
    try:
        event_date = datetime.datetime.strptime(event_date_raw.strip(), "%Y-%m-%d").strftime("%m/%d/%Y")
    except Exception:
        event_date = event_date_raw  # leave as-is if format is unexpected
    start_time_raw = parse_field(body, "Field 6")
    end_time_raw   = parse_field(body, "Field 7")
    start_time     = to_12hr(start_time_raw)
    end_time       = to_12hr(end_time_raw)
    attendance     = parse_field(body, "Estimated Attendance")
    event_detail   = parse_field(body, "Event Details")

    # Day of week and type — use raw YYYY-MM-DD for parsing, not the display-formatted version
    day_of_week, day_type = get_day_info(event_date_raw)

    # Duration calculated from raw HH:MM before 12hr conversion
    duration = calculate_duration(start_time_raw, end_time_raw)

    # Pricing
    try:
        att_int = int(attendance)
    except ValueError:
        att_int = 0

    price_2hr, price_4hr, num_pizzas = calculate_pricing(att_int, day_type, duration)

    return {
        "msg_id":              msg_id,
        "date_received_date":  date_received_date,
        "date_received_time":  date_received_time,
        "location":            location,
        "first_name":          first_name,
        "last_name":           last_name,
        "email":               email,
        "phone":               phone,
        "event_date":          event_date,
        "event_date_raw":      event_date_raw,  # YYYY-MM-DD, used for calendar check
        "day_of_week":         day_of_week,
        "day_type":            day_type,
        "start_time":          start_time,
        "start_time_raw":      start_time_raw,  # HH:MM, used for hours/calendar checks
        "end_time":            end_time,
        "end_time_raw":        end_time_raw,    # HH:MM, used for hours/calendar checks
        "duration":            duration,
        "attendance":          attendance,
        "price_2hr":           price_2hr,
        "price_4hr":           price_4hr,
        "num_pizzas":          num_pizzas,
        "event_details":       event_detail,
    }

def to_12hr(time_str):
    """Convert HH:MM (24hr) to h:MM AM/PM. Returns original string if parsing fails."""
    try:
        t = datetime.datetime.strptime(time_str.strip(), "%H:%M")
        return t.strftime("%-I:%M %p")  # e.g. "6:00 PM"
    except Exception:
        return time_str

def get_day_info(event_date_str):
    """Return (day_of_week_name, 'Weeknight'|'Weekend'|'Monday'|'Unknown')."""
    try:
        dt = datetime.datetime.strptime(event_date_str.strip(), "%Y-%m-%d")
        dow = dt.strftime("%A")  # e.g. "Saturday"
        weekday = dt.weekday()   # 0=Mon, 6=Sun
        if weekday == 0:
            return dow, "Monday"
        elif weekday in (1, 2, 3):
            return dow, "Weeknight"
        else:
            return dow, "Weekend"
    except Exception:
        return "", "Unknown"

def calculate_duration(start_str, end_str):
    """Calculate event duration in hours from HH:MM strings."""
    try:
        fmt = "%H:%M"
        start = datetime.datetime.strptime(start_str.strip(), fmt)
        end   = datetime.datetime.strptime(end_str.strip(), fmt)
        delta = (end - start).seconds / 3600
        return round(delta, 1)
    except Exception:
        return ""

def calculate_pricing(attendance, day_type, duration):
    """Return (est_price_2hr, est_price_4hr, num_pizzas) based on attendance and day type."""
    if attendance <= 0 or day_type in ("Unknown", "Monday"):
        return "Custom", "Custom", "—"

    if attendance > 60:
        return "Custom (>60 guests)", "Custom (>60 guests)", "—"

    bracket_idx = None
    for i, (low, high) in enumerate(GUEST_BRACKETS):
        if low <= attendance <= high:
            bracket_idx = i
            break

    if bracket_idx is None:
        return "Custom", "Custom", "—"

    pizza_counts = [8, 12, 16, 20, 24]
    num_pizzas = pizza_counts[bracket_idx]

    if day_type == "Weekend":
        p2 = WEEKEND_2HR[bracket_idx]
        p4 = WEEKEND_4HR[bracket_idx]
    else:  # Weeknight
        p2 = WEEKNIGHT_2HR[bracket_idx]
        p4 = WEEKNIGHT_4HR[bracket_idx]

    return f"${p2:,}", f"${p4:,}", num_pizzas

# ── Google Sheets ─────────────────────────────────────────────────────────────

def ensure_crm_tab(sheets):
    """Create the CRM tab with headers if it doesn't exist."""
    spreadsheet = sheets.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    existing_tabs = [s["properties"]["title"] for s in spreadsheet["sheets"]]

    if CRM_TAB not in existing_tabs:
        print(f"Creating '{CRM_TAB}' tab...")
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": CRM_TAB}}}]},
        ).execute()
        # Write headers
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{CRM_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [CRM_HEADERS]},
        ).execute()
        print(f"'{CRM_TAB}' tab created with headers.")

def get_next_lead_id(sheets):
    """Read existing rows to determine the next sequential lead ID."""
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{CRM_TAB}!A:A",
    ).execute()
    rows = result.get("values", [])
    # rows[0] is header; leads start at row index 1
    return len(rows)  # header row + N leads → next ID = N

def get_existing_message_ids(sheets):
    """Read the Gmail Message ID column (V) to avoid duplicates."""
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{CRM_TAB}!V:V",  # Column V = Gmail Message ID
    ).execute()
    rows = result.get("values", [])
    return {row[0] for row in rows[1:] if row}  # skip header

def append_lead(sheets, lead, lead_id):
    """Append a single lead row to the CRM sheet."""
    row = [
        lead_id,
        lead["date_received_date"],
        lead["date_received_time"],
        lead["location"],
        lead["first_name"],
        lead["last_name"],
        lead["email"],
        lead["phone"],
        lead["event_date"],
        lead["day_of_week"],
        lead["day_type"],
        lead["start_time"],
        lead["end_time"],
        str(lead["duration"]),
        lead["attendance"],
        lead["price_2hr"],
        lead["price_4hr"],
        str(lead["num_pizzas"]),
        lead["event_details"],
        "New Lead",   # Status default
        "",           # Notes (blank)
        lead["msg_id"],
        "",           # Reply Sent — populated after email is sent successfully
        "",           # Follow-up Sent — populated after follow-up is sent
    ]

    sheets.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{CRM_TAB}!A1",
        valueInputOption="RAW",  # RAW prevents Sheets from auto-converting dates/times to serial numbers
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    print(f"  → Added lead #{lead_id}: {lead['first_name']} {lead['last_name']} ({lead['location']}, {lead['event_date']})")

def mark_reply_sent(sheets, lead_id):
    """Write the current timestamp to the Reply Sent column (W) for the given lead row."""
    # lead_id is 1-based; row 1 is header, so lead row = lead_id + 1
    row_num = lead_id + 1
    timestamp = datetime.datetime.now().strftime("%m/%d/%Y %-I:%M %p")
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{CRM_TAB}!W{row_num}",
        valueInputOption="RAW",
        body={"values": [[timestamp]]},
    ).execute()

# ── Calendar ──────────────────────────────────────────────────────────────────

def _within_two_hours(calendar_event, req_start_raw, req_end_raw, event_date_raw):
    """Returns True if requested time overlaps or is within 2 hours of the calendar event."""
    try:
        evt_start_str = calendar_event.get("start", {}).get("dateTime", "")
        evt_end_str   = calendar_event.get("end",   {}).get("dateTime", "")
        if not evt_start_str or not evt_end_str or not req_start_raw or not req_end_raw:
            return True  # fail closed — treat as conflict if times can't be parsed

        # Extract HH:MM from ISO dateTime strings (e.g. "2026-04-21T18:00:00-04:00")
        evt_start_hm = evt_start_str.split("T")[1][:5]
        evt_end_hm   = evt_end_str.split("T")[1][:5]

        fmt  = "%H:%M"
        date = datetime.datetime.strptime(event_date_raw.strip(), "%Y-%m-%d").date()

        def to_dt(hm):
            return datetime.datetime.combine(date, datetime.datetime.strptime(hm, fmt).time())

        req_start = to_dt(req_start_raw)
        req_end   = to_dt(req_end_raw)
        evt_start = to_dt(evt_start_hm)
        evt_end   = to_dt(evt_end_hm)
        two_hours = datetime.timedelta(hours=2)

        return (
            abs(req_start - evt_start) < two_hours or
            abs(req_start - evt_end)   < two_hours or
            abs(req_end   - evt_start) < two_hours or
            abs(req_end   - evt_end)   < two_hours
        )
    except Exception:
        return True  # fail closed


def check_calendar(creds, location, event_date_raw, start_time_raw="", end_time_raw=""):
    """Check Google Calendar for booking conflicts on the requested date.

    Returns 'private_booked', 'booked', or 'clear'.
    - private_booked: a 'Private Reservation' event exists — date fully blocked.
    - booked: a 'Reservation' event exists and requested time is within 2 hours of it.
    - clear: no conflict found, or check failed (fails open).
    """
    cal_id = LP_CALENDAR_ID if location == "Locust Point" else TIM_CALENDAR_ID
    if not cal_id:
        print(f"  Calendar ID not configured for {location} — skipping conflict check.")
        return "clear"

    if not event_date_raw:
        return "clear"

    try:
        calendar = build("calendar", "v3", credentials=creds)
        date = datetime.datetime.strptime(event_date_raw.strip(), "%Y-%m-%d")
        time_min = date.strftime("%Y-%m-%dT00:00:00-05:00")
        time_max = (date + datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00-05:00")

        result = calendar.events().list(
            calendarId=cal_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
        ).execute()

        for event in result.get("items", []):
            title = event.get("summary", "").lower()
            if "private reservation" in title:
                print(f"  Private Reservation found at {location} on {event_date_raw} — date fully blocked.")
                return "private_booked"
            if "reservation" in title:
                if _within_two_hours(event, start_time_raw, end_time_raw, event_date_raw):
                    print(f"  Reservation conflict (within 2hr buffer) at {location} on {event_date_raw}.")
                    return "booked"

        return "clear"

    except Exception as e:
        print(f"  Calendar check failed for {location}: {e}. Proceeding without conflict flag.")
        return "clear"

# ── Email Reply ───────────────────────────────────────────────────────────────

HOURS_OF_OPERATION = {
    "Tuesday":   ("16:00", "21:00"),
    "Wednesday": ("16:00", "21:00"),
    "Thursday":  ("16:00", "21:00"),
    "Friday":    ("14:00", "22:00"),
    "Saturday":  ("12:00", "22:00"),
    "Sunday":    ("12:00", "20:00"),
}


def _is_outside_hours(day_of_week, start_time_raw, end_time_raw):
    """Returns True if the event falls outside normal hours of operation."""
    if day_of_week == "Monday":
        return True
    if day_of_week not in HOURS_OF_OPERATION:
        return False
    open_str, close_str = HOURS_OF_OPERATION[day_of_week]
    try:
        fmt     = "%H:%M"
        start   = datetime.datetime.strptime(start_time_raw, fmt).time()
        end     = datetime.datetime.strptime(end_time_raw,   fmt).time()
        open_t  = datetime.datetime.strptime(open_str,       fmt).time()
        close_t = datetime.datetime.strptime(close_str,      fmt).time()
        return start < open_t or end > close_t
    except Exception:
        return False


def _select_template(lead, calendar_status):
    """Return the template filename for this lead based on routing rules."""
    prefix = "lp" if lead["location"] == "Locust Point" else "tim"

    # 1. Calendar conflict takes top priority
    if calendar_status in ("private_booked", "booked"):
        return f"{prefix}_date_booked.txt"

    # 2. >60 guests → private buyout
    try:
        if int(str(lead["attendance"]).strip()) > 60:
            return f"{prefix}_private_buyout.txt"
    except (ValueError, TypeError):
        pass

    # 3. Monday or outside hours → custom pricing
    if _is_outside_hours(lead["day_of_week"], lead.get("start_time_raw", ""), lead.get("end_time_raw", "")):
        return f"{prefix}_custom_pricing.txt"

    # 4. Default — standard
    return f"{prefix}_standard.txt"


def _load_template(template_name, lead):
    """Load a template file, substitute placeholders, and return (subject, body)."""
    template_path = Path("templates") / template_name
    with open(template_path, "r") as f:
        content = f.read()

    lines = content.split("\n")
    subject = lines[0].replace("SUBJECT: ", "").strip()
    body    = "\n".join(lines[2:])  # skip subject line and --- separator

    replacements = {
        "[FIRST_NAME]":  lead["first_name"] or "there",
        "[EVENT_DATE]":  lead["event_date"],
        "[DAY_OF_WEEK]": lead["day_of_week"],
        "[GUEST_COUNT]": str(lead["attendance"]),
        "[START_TIME]":  lead["start_time"],
        "[END_TIME]":    lead["end_time"],
        "[PRICE_2HR]":   str(lead["price_2hr"]),
    }
    for placeholder, value in replacements.items():
        subject = subject.replace(placeholder, value)
        body    = body.replace(placeholder, value)

    return subject, body


def send_reply(main_creds, send_creds, lead):
    """Compose and send a reply email using the appropriate template.

    Uses main_creds for the calendar check and send_creds for the Gmail send.
    Returns True on success, False on failure.
    Requires ENABLE_REPLY_SEND=true — caller is responsible for checking the flag.
    PDF is attached for all templates except custom_pricing.
    """
    to_email = lead["email"]
    location = lead["location"]

    if not to_email:
        print("  No customer email address — cannot send reply.")
        return False

    # Calendar conflict check — uses main (info@) credentials
    calendar_status = check_calendar(
        main_creds, location, lead.get("event_date_raw", ""),
        lead.get("start_time_raw", ""), lead.get("end_time_raw", "")
    )

    # Select and load template
    template_name = _select_template(lead, calendar_status)
    subject, body_text = _load_template(template_name, lead)

    msg = MIMEMultipart()
    msg["From"]    = REPLY_FROM
    msg["To"]      = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))

    # Attach pricing PDF — skip for custom_pricing templates
    if "custom_pricing" not in template_name:
        pdf_path = CUSTOMER_PRICING_LP if location == "Locust Point" else CUSTOMER_PRICING_TIM
        if not os.path.exists(pdf_path):
            print(f"  Pricing PDF not found at '{pdf_path}' — sending without attachment.")
        else:
            with open(pdf_path, "rb") as f:
                pdf_data = f.read()
            pdf_part = MIMEBase("application", "octet-stream")
            pdf_part.set_payload(pdf_data)
            email_encoders.encode_base64(pdf_part)
            pdf_part.add_header("Content-Disposition", f'attachment; filename="{os.path.basename(pdf_path)}"')
            msg.attach(pdf_part)

    # Encode and send via Gmail API — uses send (reservations@) credentials
    raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    try:
        gmail_send = build("gmail", "v1", credentials=send_creds)
        gmail_send.users().messages().send(
            userId="me",
            body={"raw": raw_message},
        ).execute()
        print(f"  → Reply sent to {to_email} [{template_name}]")
        return True
    except Exception as e:
        print(f"  Reply send failed for {to_email}: {e}")
        return False

# ── State tracking ────────────────────────────────────────────────────────────

def load_processed_ids():
    if PROCESSED_FILE.exists():
        return set(json.loads(PROCESSED_FILE.read_text()))
    return set()

def save_processed_id(msg_id):
    ids = load_processed_ids()
    ids.add(msg_id)
    PROCESSED_FILE.parent.mkdir(exist_ok=True)
    PROCESSED_FILE.write_text(json.dumps(list(ids)))

# ── Follow-up ─────────────────────────────────────────────────────────────────

def _business_days_since(timestamp_str):
    """Return the number of business days (Mon–Fri) between timestamp_str and today."""
    try:
        sent_date = datetime.datetime.strptime(timestamp_str, "%m/%d/%Y %I:%M %p").date()
        today = datetime.date.today()
        count = 0
        current = sent_date + datetime.timedelta(days=1)
        while current <= today:
            if current.weekday() < 5:  # 0=Mon, 4=Fri
                count += 1
            current += datetime.timedelta(days=1)
        return count
    except Exception:
        return 0


def send_followups(sheets, send_creds):
    """Scan CRM for leads that need a 5-business-day follow-up and send them.

    Eligibility: Reply Sent (col W) is set, Follow-up Sent (col X) is blank,
    and at least 5 business days have passed since the reply was sent.
    No PDF attached — follow-up is plain email only.
    """
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{CRM_TAB}!A:X",
    ).execute()
    rows = result.get("values", [])
    if len(rows) <= 1:
        return

    followups_sent = 0
    for i, row in enumerate(rows[1:], start=2):  # row 1 is header; lead rows start at 2
        while len(row) < 24:
            row.append("")

        reply_sent    = row[22]  # col W
        followup_sent = row[23]  # col X
        email         = row[6]   # col G
        first_name    = row[4]   # col E
        location      = row[3]   # col D
        event_date    = row[8]   # col I
        day_of_week   = row[9]   # col J
        start_time    = row[11]  # col L
        end_time      = row[12]  # col M
        price_2hr     = row[15]  # col P
        attendance    = row[14]  # col O

        if not reply_sent or followup_sent or not email:
            continue

        if _business_days_since(reply_sent) < 5:
            continue

        template_name = "lp_followup.txt" if location == "Locust Point" else "tim_followup.txt"
        lead = {
            "first_name": first_name,
            "event_date": event_date,
            "day_of_week": day_of_week,
            "location": location,
            "attendance": attendance,
            "start_time": start_time,
            "end_time": end_time,
            "price_2hr": price_2hr,
        }
        subject, body_text = _load_template(template_name, lead)

        msg = MIMEMultipart()
        msg["From"]    = REPLY_FROM
        msg["To"]      = email
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))

        raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        try:
            gmail_send = build("gmail", "v1", credentials=send_creds)
            gmail_send.users().messages().send(
                userId="me",
                body={"raw": raw_message},
            ).execute()
            timestamp = datetime.datetime.now().strftime("%m/%d/%Y %-I:%M %p")
            sheets.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f"{CRM_TAB}!X{i}",
                valueInputOption="RAW",
                body={"values": [[timestamp]]},
            ).execute()
            print(f"  → Follow-up sent to {email} (lead row {i})")
            followups_sent += 1
        except Exception as e:
            print(f"  Follow-up failed for row {i}: {e}")

    if followups_sent:
        print(f"  {followups_sent} follow-up(s) sent.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not SHEET_ID:
        print("ERROR: GOOGLE_SHEET_ID not set in .env")
        return

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    reply_status = "ENABLED" if ENABLE_REPLY_SEND else "disabled (set ENABLE_REPLY_SEND=true to activate)"
    print(f"\n[{now}] Running lead intake... (reply sending: {reply_status})")

    creds  = get_google_credentials()   # info@ — read Gmail, Sheets, Calendar
    gmail  = build("gmail", "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    send_creds = get_send_credentials() if ENABLE_REPLY_SEND else None

    ensure_crm_tab(sheets)
    existing_msg_ids = get_existing_message_ids(sheets)

    new_emails = fetch_new_lead_emails(gmail)

    next_id = get_next_lead_id(sheets)  # read once, increment locally to avoid double-counting
    added = 0
    for item in new_emails:
        msg_id  = item["id"]
        message = item["message"]

        if msg_id in existing_msg_ids:
            print(f"  Skipping duplicate: {msg_id}")
            mark_as_read(gmail, msg_id)
            save_processed_id(msg_id)
            continue

        lead = parse_lead(msg_id, message)
        append_lead(sheets, lead, next_id)

        if ENABLE_REPLY_SEND:
            reply_sent = send_reply(creds, send_creds, lead)
            if reply_sent:
                mark_reply_sent(sheets, next_id)

        mark_as_read(gmail, msg_id)
        save_processed_id(msg_id)
        next_id += 1
        added += 1

    print(f"Done. {added} new lead(s) added to '{CRM_TAB}' tab.")

    if ENABLE_REPLY_SEND:
        send_followups(sheets, send_creds)

if __name__ == "__main__":
    main()
