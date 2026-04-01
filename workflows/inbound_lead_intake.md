# Workflow: Inbound Event Lead Intake

## Objective
Automatically detect new event inquiry emails from Webflow forms submitted to `info@diamondbackbeer.com`, parse them into structured lead records, append them to the Google Sheets CRM, check Google Calendar for conflicts on the requested date, and send a template reply email with the location pricing PDF attached.

---

## Trigger
Run on a schedule — recommended: **every hour between 8am and 8pm**.

Cron expression (edit path as needed):
```
0 8-20 * * * cd "/Users/tfost/Agentic Workflows/Event CRM" && python tools/run_lead_intake.py >> .tmp/lead_intake.log 2>&1
```

---

## Required Inputs

| Variable | Where | Description |
|---|---|---|
| `GOOGLE_SHEET_ID` | `.env` | ID of the Google Sheet to write leads into |
| `CRM_TAB_NAME` | `.env` | Tab name (default: `CRM`) |
| `GMAIL_CREDENTIALS_FILE` | `.env` | Path to OAuth credentials JSON (default: `credentials.json`) |
| `LEAD_INTAKE_START_DATE` | `.env` | Only import emails on/after this date (format: YYYY/MM/DD) |
| `ENABLE_REPLY_SEND` | `.env` | Set to `true` to activate automated reply sending (default: `false`) |
| `REPLY_FROM` | `.env` | Email address replies are sent from (default: `reservations@diamondbackbeer.com`) |
| `CUSTOMER_PRICING_LP` | `.env` | Path to Locust Point customer-facing pricing PDF |
| `CUSTOMER_PRICING_TIM` | `.env` | Path to Timonium customer-facing pricing PDF |
| `LP_CALENDAR_ID` | `.env` | Google Calendar ID for Locust Point events |
| `TIM_CALENDAR_ID` | `.env` | Google Calendar ID for Timonium events |
| `credentials.json` | Project root | Downloaded from Google Cloud Console |
| `token.json` | Project root | Auto-generated on first run (OAuth flow) |

---

## Monitored Email Subjects

| Subject Line | Location |
|---|---|
| `New form submission for Timonium Form 2` | Timonium |
| `New form submission for Locust Point Form 2` | Locust Point |

Sender: `no-reply-forms@webflow.com`

---

## Email Field Mapping

| Email Field | CRM Column |
|---|---|
| First Name | First Name |
| Last Name | Last Name |
| Email Address | Email |
| Phone Number | Phone |
| Field 5 | Event Date (YYYY-MM-DD) |
| Field 6 | Start Time (HH:MM) |
| Field 7 | End Time (HH:MM) |
| Estimated Attendance | Estimated Attendance |
| Event Details | Event Details |

---

## CRM Output Columns

| Column | Description |
|---|---|
| Lead ID | Sequential integer |
| Date Received | When the email arrived (mm/dd/yyyy) |
| Time Received | When the email arrived (h:MM AM/PM) |
| Location | Timonium or Locust Point |
| First Name | — |
| Last Name | — |
| Email | — |
| Phone | — |
| Event Date | Requested event date (mm/dd/yyyy) |
| Day of Week | e.g. Saturday |
| Day Type | Weeknight / Weekend / Monday / Unknown |
| Start Time | — |
| End Time | — |
| Duration (hrs) | Auto-calculated from start/end |
| Estimated Attendance | From form |
| Est. Price (2hr) | Auto-calculated from pricing table |
| Est. Price (4hr) | Auto-calculated from pricing table |
| # of Pizzas | From pricing table |
| Event Details | Free text from customer |
| Status | Defaults to "New Lead" |
| Notes | Blank — for manual staff use |
| Gmail Message ID | For deduplication (reference) |
| Reply Sent | Timestamp when auto-reply was sent; blank if not yet sent |

---

## Pricing Logic
See `docs/pricing_reference.md` for full tables and formulas.

- **Weeknight** (Tue–Thu): $15 × median guest bracket × 2 hrs
- **Weekend** (Fri–Sun): $20 × median guest bracket × 2 hrs
- **Monday / >60 guests**: Flagged as "Custom" — manual pricing required
- **4-hour rate**: Weeknight = 2hr × 2 − $100; Weekend adds the Weeknight/Weekend premium

---

## Location-Specific Rules to Note During Follow-Up

### Timonium
- Parties **>30 guests cannot start before 4:00 PM** (Aveley Farms Coffee shares the space)
- Patio is small: 40 capacity (20 seats) — confirm patio vs. taproom early
- No pizza buffet (individual pizza service only)

### Locust Point
- Large patio: 250 capacity (150 seats) — primary outdoor option
- Pizza buffet for parties >30 (5 pies: 4 classic, 1 seasonal)
- No Aveley Farms time restriction

---

## Automated Reply Sending

### Activation
Reply sending is **off by default**. Set `ENABLE_REPLY_SEND=true` in `.env` when ready to go live.

### What the reply includes
- Personalized greeting with customer's first name
- Event date, guest count, duration, and estimated 2-hour price
- Custom pricing note if the event falls on Monday or has >60 guests
- Location-specific PDF attached (`docs/Customer Pricing - Locust Point.pdf` or `docs/Customer Pricing - TIMONIUM.pdf`)
- Sent from `reservations@diamondbackbeer.com`

### Calendar conflict check
Before composing the reply, the script checks the location's Google Calendar for any existing events on the requested date:
- **No conflict** → sends standard pricing reply
- **Conflict found** → sends a variant that politely flags the date may be taken and asks for alternate dates, while still including pricing

The calendar check **fails open** — if the calendar API is unavailable or calendar IDs aren't configured, the standard reply is sent without a conflict flag.

### Deduplication
The `Reply Sent` column (W) is written with a timestamp when a reply is sent successfully. This provides visibility into which leads have been auto-replied to.

---

## Deduplication Strategy
- The script writes processed Gmail Message IDs to `.tmp/processed_emails.json`
- The CRM sheet's `Gmail Message ID` column (V) is also checked before inserting
- `processed_emails.json` is the primary guard — a lead email is only ever processed once

---

## Setup Instructions (One-Time)

### 1. Enable APIs (Google Cloud Console)
1. Go to [Google Cloud Console](https://console.cloud.google.com/) — sign in as your Workspace admin
2. Create a new project (e.g. "Diamondback CRM")
3. Enable **Gmail API**, **Google Sheets API**, and **Google Calendar API**
4. Go to **APIs & Services → Credentials**
5. Configure the **OAuth consent screen** (Internal type — limits access to your Workspace org only)
6. Create an **OAuth 2.0 Client ID** (type: Desktop app)
7. Download the JSON → save as `credentials.json` in the project root

> **Workspace admin note:** If your org restricts third-party app access, go to `admin.google.com` → Security → API Controls → App Access Control and mark your OAuth app as Trusted.

### 2. Configure `.env`
Fill in all required values — especially `LP_CALENDAR_ID` and `TIM_CALENDAR_ID` before enabling replies.

**Finding a Google Calendar ID:**
Google Calendar → Settings gear → click the calendar name in the left sidebar → scroll to "Calendar ID" (format: `abc123@group.calendar.google.com` or `email@domain.com` for primary calendars).

### 3. Configure sending alias
To send FROM `reservations@diamondbackbeer.com`:
- **Option A (recommended):** Re-authorize OAuth logged in as `reservations@diamondbackbeer.com`
- **Option B:** In Gmail settings for the authorized account, add `reservations@diamondbackbeer.com` as a "Send mail as" alias

### 4. Install dependencies
```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client python-dotenv
```

### 5. Delete token.json and re-authorize
The OAuth scopes have been expanded (added `gmail.send` and `calendar.readonly`). Delete the old token and re-run to re-authorize:
```bash
rm token.json
python tools/run_lead_intake.py
```
A browser window will open — authorize all requested permissions.

### 6. Set up the cron job
```bash
crontab -e
```
Add:
```
0 8-20 * * * cd "/Users/tfost/Agentic Workflows/Event CRM" && python tools/run_lead_intake.py >> .tmp/lead_intake.log 2>&1
```

---

## Error Handling
| Issue | Resolution |
|---|---|
| `token.json` expired or scope mismatch | Delete `token.json` and re-run to re-authorize |
| `GOOGLE_SHEET_ID not set` | Add to `.env` |
| Email body parsing fails | Check that Webflow form field names haven't changed |
| New subject line format | Add to `SUBJECT_MAP` dict in `run_lead_intake.py` |
| Calendar IDs not set | Script logs a warning and sends standard reply (no conflict check) |
| Pricing PDF not found | Script logs an error and skips sending the reply |
| Reply send fails | Error is logged; lead row stays in sheet with blank Reply Sent column |
| Rate limit on Gmail/Sheets API | Script is lightweight; hourly runs are well within free quota |

---

## Blank Field Handling
If a submitter leaves a form field blank, the corresponding CRM column is written as empty — never populated with data from another field. This is enforced by two rules in the parser:
- The HTML stripper adds newlines before `<td>` and `<li>` tags (not just `<br>`, `<p>`, `<div>`, `<tr>`) to prevent adjacent table cells from running together
- `parse_field` uses `[ \t]*` (horizontal whitespace only) after the colon — not `\s*` — so it never crosses a newline to grab the next field's label when a value is absent

Downstream effects of a blank field:
- Blank Start or End Time → Duration is also blank (can't calculate)
- Blank or non-numeric Attendance → pricing defaults to "Custom" / "—"
- Blank customer Email → reply is skipped with a log warning

---

## Known Constraints
- Webflow field names `Field 5`, `Field 6`, `Field 7` are fragile — if the form is updated, update the parser
- Monday pricing is not defined in the rate sheet; those leads are flagged Custom
- Parties >60 require manual custom pricing
- Calendar conflict check uses Eastern time (UTC-5) bounds — events will be correctly captured in ET, but DST offset is fixed at -05:00

---

## Files
| File | Purpose |
|---|---|
| `tools/run_lead_intake.py` | Full pipeline script |
| `docs/pricing_reference.md` | Internal pricing tables and logic |
| `docs/Reservation Pricing - Locust Point.pdf` | Internal source PDF (Locust Point) |
| `docs/Reservation Pricing - TIMONIUM.pdf` | Internal source PDF (Timonium) |
| `docs/Customer Pricing - Locust Point.pdf` | Customer-facing PDF attached to replies |
| `docs/Customer Pricing - TIMONIUM.pdf` | Customer-facing PDF attached to replies |
| `.tmp/processed_emails.json` | State file — processed Gmail message IDs |
| `.tmp/lead_intake.log` | Cron log output |
| `templates/lp_standard.txt` | Reply template — Locust Point, standard inquiry |
| `templates/tim_standard.txt` | Reply template — Timonium, standard inquiry |
| `templates/lp_custom_pricing.txt` | Reply template — Locust Point, custom pricing needed |
| `templates/tim_custom_pricing.txt` | Reply template — Timonium, custom pricing needed |
| `templates/lp_date_booked.txt` | Reply template — Locust Point, date unavailable |
| `templates/tim_date_booked.txt` | Reply template — Timonium, date unavailable |
| `templates/lp_private_buyout.txt` | Reply template — Locust Point, private/buyout inquiry |
| `templates/tim_private_buyout.txt` | Reply template — Timonium, private/buyout inquiry |
