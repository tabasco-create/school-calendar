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
