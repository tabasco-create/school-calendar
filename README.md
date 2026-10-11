# School Calendar (AEA + PBMS)

A curated calendar feed built from three public Google Calendars:
AEA's academic calendar, the PBMS school calendar, and the SDUSD district calendar.

**Subscribe:** in Google Calendar, go to *Other calendars → + → From URL* and paste
`https://<your-github-user>.github.io/school-calendar/school.ics`

**Status page:** `https://<your-github-user>.github.io/school-calendar/`
(last updated, latest changes, full upcoming list)

## What's kept

| Source | Kept | Dropped |
|---|---|---|
| AEA | No-school days, minimum days, conferences, semester start/end, picture day, family events (Trunk or Treat, Lantern Fest, Winter Market, book fair…) | A/B week banners, board/committee/ELAC/FoAEA meetings, middle-school-only events, enrollment |
| District (for PBMS) | Breaks, no-school holidays, school reopening, minimum days | Board meetings, awareness days/months, religious observances, testing windows |
| PBMS | Anything that isn't a meeting (the feed is currently empty) | Meetings |

## Title prefixes

- `[BOTH OFF]` both kids are home
- `[AEA]` / `[PBMS]` applies to one school
- `[AEA ONLY OFF]` / `[PBMS ONLY OFF]` days only one kid is home (e.g. the end of winter break)
- `(confirm)` a district holiday the district feed doesn't explicitly mark as no school

## How it updates

`.github/workflows/update.yml` runs `build_calendar.py` daily around 6am Pacific
and commits the refreshed `docs/` folder. Run it manually from the **Actions** tab
(*Update school calendar → Run workflow*). Google Calendar re-reads subscribed
feeds on its own schedule, usually every 12–24 hours.

To change what's kept, edit the keyword lists at the top of `build_calendar.py`.

**A day off is missing?** The source feeds aren't always complete (the district feed
left out Indigenous Peoples' Day 2026). Add a line to `MANUAL_DAYS_OFF` near the top
of `build_calendar.py`, e.g. `("PBMS", "2026-10-12", "2026-10-12", "Indigenous Peoples' Day - NO SCHOOL")`.

## Lunch menus

The index page has a **Lunch** card showing the week's lunch for AEA (elementary) and PBMS (SDUSD middle school).

- `lunch/aea_lunch.py` downloads AEA's monthly elementary menu PDF and reads the Lunch page into `docs/lunch/aea.json`.
- `docs/lunch/sdusd.json` holds the SDUSD Middle/High weekly lunch menu, typed in by hand. The district's PDF is an image, and it is the same menu every week for the semester. `lunch/sdusd_check.py` flags it on the page when the district posts a new PDF, so the list can be updated.
- `.github/workflows/lunch.yml` checks on the 1st and 15th of each month. If the current month's AEA menu isn't posted yet, it retries daily until it is. "Run workflow" forces a fresh download.
