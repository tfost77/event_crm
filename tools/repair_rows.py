"""
repair_rows.py
--------------
One-time repair for CRM rows corrupted by the <td>-tag / \\s* HTML parsing bugs.

Bugs caused:
  - End Time (col M) to contain attendance text appended to the time string
  - Start Time (col L) to contain the next field label when start time was blank
  - Estimated Attendance (col O) to contain the next field's text when blank
  - Duration (col N) blank because time parse failed
  - Pricing (cols P/Q/R) potentially wrong if attendance was misread

This script targets SHEET ROW numbers directly (row 1 = header).

Usage:
  python tools/repair_rows.py
"""

import os
import sys
import base64
import datetime
import re
from html.parser import HTMLParser
from dotenv import load_dotenv
from googleapiclient.errors import HttpError

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv(dotenv_path=".env")

SHEET_ID    = os.getenv("GOOGLE_SHEET_ID")
CRM_TAB     = os.getenv("CRM_TAB_NAME", "CRM")
CREDENTIALS = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE  = "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Sheet row numbers to repair (row 1 = header, row 2 = first lead)
SHEET_ROWS_TO_REPAIR = [14, 15, 18, 23]

# Column indices (1-based)
COL_DAY_TYPE   = 11   # K
COL_START      = 12   # L  ← first column we patch
COL_END        = 13   # M
COL_DURATION   = 14   # N
COL_ATTENDANCE = 15   # O
COL_PRICE_2HR  = 16   # P
COL_PRICE_4HR  = 17   # Q
COL_PIZZAS     = 18   # R  ← last column we patch
COL_MSG_ID     = 22   # V

# Pricing tables (same as run_lead_intake.py)
GUEST_BRACKETS = [(10, 20), (21, 30), (31, 40), (41, 50), (51, 60)]
WEEKNIGHT_2HR  = [500,  750,  1050, 1350, 1650]
WEEKEND_2HR    = [600,  1000, 1400, 1800, 2200]
WEEKNIGHT_4HR  = [900,  1400, 2000, 2600, 3200]
WEEKEND_4HR    = [1000, 1650, 2350, 3050, 3750]

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_google_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds

# ── HTML parser (fixed — includes td and li) ─────────────────────────────────

class _HTMLTextExtractor(HTMLParser):
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

# ── Email helpers ─────────────────────────────────────────────────────────────

def get_email_body(message):
    payload = message.get("payload", {})

    def decode_part(part):
        data = part.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    if "parts" not in payload:
        raw = decode_part(payload)
        return _strip_html(raw) if "<" in raw else raw

    plain = next((decode_part(p) for p in payload["parts"] if p.get("mimeType") == "text/plain"), None)
    if plain:
        return plain

    html = next((decode_part(p) for p in payload["parts"] if p.get("mimeType") == "text/html"), None)
    if html:
        return _strip_html(html)

    return decode_part(payload["parts"][0])

def parse_field(body, label):
    # Use [ \t]* (not \s*) to avoid crossing newlines when a field is blank
    pattern = rf"{re.escape(label)}:[ \t]*(.+)"
    match = re.search(pattern, body, re.IGNORECASE)
    return match.group(1).strip() if match else ""

def to_12hr(time_str):
    try:
        t = datetime.datetime.strptime(time_str.strip(), "%H:%M")
        return t.strftime("%-I:%M %p")
    except Exception:
        return time_str

def calculate_duration(start_str, end_str):
    try:
        fmt = "%H:%M"
        start = datetime.datetime.strptime(start_str.strip(), fmt)
        end   = datetime.datetime.strptime(end_str.strip(), fmt)
        return round((end - start).seconds / 3600, 1)
    except Exception:
        return ""

def calculate_pricing(attendance, day_type):
    try:
        att_int = int(attendance)
    except (ValueError, TypeError):
        att_int = 0

    if att_int <= 0 or day_type in ("Unknown", "Monday"):
        return "Custom", "Custom", "—"
    if att_int > 60:
        return "Custom (>60 guests)", "Custom (>60 guests)", "—"

    bracket_idx = next((i for i, (lo, hi) in enumerate(GUEST_BRACKETS) if lo <= att_int <= hi), None)
    if bracket_idx is None:
        return "Custom", "Custom", "—"

    pizza_counts = [8, 12, 16, 20, 24]
    num_pizzas = pizza_counts[bracket_idx]

    if day_type == "Weekend":
        return f"${WEEKEND_2HR[bracket_idx]:,}", f"${WEEKEND_4HR[bracket_idx]:,}", num_pizzas
    else:
        return f"${WEEKNIGHT_2HR[bracket_idx]:,}", f"${WEEKNIGHT_4HR[bracket_idx]:,}", num_pizzas

# ── Sheet helpers ─────────────────────────────────────────────────────────────

def col_letter(col_num):
    result = ""
    while col_num:
        col_num, rem = divmod(col_num - 1, 26)
        result = chr(65 + rem) + result
    return result

def read_row(sheets, row_num):
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{CRM_TAB}!A{row_num}:{col_letter(COL_MSG_ID)}{row_num}",
    ).execute()
    rows = result.get("values", [])
    return rows[0] if rows else []

def update_row(sheets, row_num, start_time, end_time, duration, attendance, price_2hr, price_4hr, num_pizzas):
    """Update cols L through R for a single sheet row."""
    range_str = f"{CRM_TAB}!{col_letter(COL_START)}{row_num}:{col_letter(COL_PIZZAS)}{row_num}"
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=range_str,
        valueInputOption="RAW",
        body={"values": [[start_time, end_time, str(duration), attendance, price_2hr, price_4hr, str(num_pizzas)]]},
    ).execute()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not SHEET_ID:
        print("ERROR: GOOGLE_SHEET_ID not set in .env")
        sys.exit(1)

    creds  = get_google_credentials()
    gmail  = build("gmail", "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    print(f"Repairing sheet rows: {SHEET_ROWS_TO_REPAIR}\n")

    for srow in SHEET_ROWS_TO_REPAIR:
        row_data = read_row(sheets, srow)

        if not row_data or len(row_data) < COL_MSG_ID:
            print(f"  Sheet row {srow}: could not read (got {len(row_data)} cols), skipping.")
            continue

        msg_id   = row_data[COL_MSG_ID - 1]
        day_type = row_data[COL_DAY_TYPE - 1] if len(row_data) >= COL_DAY_TYPE else ""

        if not msg_id:
            print(f"  Sheet row {srow}: no Gmail Message ID in col V, skipping.")
            continue

        try:
            message = gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()
        except HttpError as e:
            print(f"  Sheet row {srow}: Gmail fetch failed for {msg_id}: {e}")
            continue

        body = get_email_body(message)

        start_time_raw = parse_field(body, "Field 6")
        end_time_raw   = parse_field(body, "Field 7")
        start_time     = to_12hr(start_time_raw)
        end_time       = to_12hr(end_time_raw)
        duration       = calculate_duration(start_time_raw, end_time_raw)
        attendance     = parse_field(body, "Estimated Attendance")
        price_2hr, price_4hr, num_pizzas = calculate_pricing(attendance, day_type)

        # Print before → after
        old = {
            "start":  row_data[COL_START - 1]      if len(row_data) >= COL_START      else "",
            "end":    row_data[COL_END - 1]         if len(row_data) >= COL_END        else "",
            "dur":    row_data[COL_DURATION - 1]    if len(row_data) >= COL_DURATION   else "",
            "att":    row_data[COL_ATTENDANCE - 1]  if len(row_data) >= COL_ATTENDANCE else "",
            "p2":     row_data[COL_PRICE_2HR - 1]   if len(row_data) >= COL_PRICE_2HR  else "",
            "p4":     row_data[COL_PRICE_4HR - 1]   if len(row_data) >= COL_PRICE_4HR  else "",
            "pizza":  row_data[COL_PIZZAS - 1]      if len(row_data) >= COL_PIZZAS     else "",
        }
        lead_id = row_data[0] if row_data else "?"
        print(f"  Sheet row {srow} (Lead {lead_id}):")
        print(f"    L Start Time:       {old['start']!r:40} → {start_time!r}")
        print(f"    M End Time:         {old['end']!r:40} → {end_time!r}")
        print(f"    N Duration:         {old['dur']!r:40} → {duration!r}")
        print(f"    O Attendance:       {old['att']!r:40} → {attendance!r}")
        print(f"    P Est. Price (2hr): {old['p2']!r:40} → {price_2hr!r}")
        print(f"    Q Est. Price (4hr): {old['p4']!r:40} → {price_4hr!r}")
        print(f"    R # of Pizzas:      {old['pizza']!r:40} → {num_pizzas!r}")

        update_row(sheets, srow, start_time, end_time, duration, attendance, price_2hr, price_4hr, num_pizzas)
        print(f"    ✓ Updated\n")

    print("Done.")

if __name__ == "__main__":
    main()
