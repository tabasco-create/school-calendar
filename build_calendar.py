#!/usr/bin/env python3
"""
Build a curated school calendar feed from the AEA, PBMS and SDUSD district
Google Calendar feeds.

Outputs (in docs/, served by GitHub Pages):
  school.ics   - the feed to subscribe to
  events.json  - snapshot used to detect changes between runs
  index.html   - status page: last updated, what changed, full event list

Standard library only, so the GitHub Actions run needs no installs.
"""
import base64
import datetime as dt
import html
import json
import os
import re
import string
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
KEEP_PAST_DAYS = 30  # drop events that ended more than this many days ago

FEEDS = {
    "AEA": "https://calendar.google.com/calendar/ical/einstein%40aeacs.org/public/basic.ics",
    "PBMS": "https://calendar.google.com/calendar/ical/c_386a9e1f1ff8a4ce9684d32fd9ad1708e82b7bae338a7e19080a7cdf33152a2c%40group.calendar.google.com/public/basic.ics",
    "DISTRICT": "https://calendar.google.com/calendar/ical/c_j6casa2h7m0c9gkg17l7eqjkm4%40group.calendar.google.com/public/basic.ics",
}

# Days off the source feeds leave out. Add a line per missing day:
#   ("SCHOOL", "YYYY-MM-DD", "YYYY-MM-DD last day", "Title")
# SCHOOL is "AEA" or "PBMS". These are treated exactly like a no-school day
# from the feed (so they can combine into [BOTH OFF]).
MANUAL_DAYS_OFF = [
    # Not on the SDUSD district calendar feed, but PBMS is closed.
    ("PBMS", "2026-10-12", "2026-10-12", "Indigenous Peoples' Day - NO SCHOOL"),
    # PBMS's last day is May 26; AEA runs to June 4. Marks the gap.
    ("PBMS", "2027-05-27", "2027-06-04", "Summer break - PBMS done for the year"),
]

# ---------------------------------------------------------------- filters

def rx(*words):
    return re.compile("|".join(words), re.I)

# Means students are home.
NO_SCHOOL = rx(r"no school", r"vacation", r"\bbreak\b", r"non-?instructional",
               r"school closed", r"schools closed", r"furlough")
# Shortened day or schedule landmark worth seeing.
SCHEDULE = rx(r"minimum day", r"early release", r"early dismissal",
              r"late start", r"conferences?", r"^first day of school$",
              r"last day of school", r"reopens", r"semester (begins|ends|starts)",
              r"quarter ends", r"trimester ends", r"grades? (due|close)",
              r"report cards?")
# District holidays that usually close schools but aren't always labelled
# "no school" in the district feed. Kept and marked "confirm".
NAMED_HOLIDAY = rx(r"^veterans'? day$", r"^labor day$", r"^memorial day$",
                   r"^juneteenth", r"^presidents'? day", r"^lincoln",
                   r"^cesar chavez", r"^indigenous", r"^columbus day$",
                   r"^martin luther king")

# Admin and adult meetings: never wanted.
MEETINGS = rx(r"board", r"trustees", r"committee", r"\belac\b", r"\bssc\b",
              r"\bdelac\b", r"\bmeeting\b", r"\bmtg\b", r"enrollment",
              r"staff development", r"professional development")
# AEA week banners.
AB_WEEK = re.compile(r"^\s*[AB]\s*week\s*$", re.I)
# AEA middle-school-only events (our AEA student is in elementary).
AEA_MIDDLE_ONLY = rx(r"\bMS\b", r"middle school", r"\bAEACMS\b",
                     r"baccalaureate", r"\bIB\b", r"6th", r"7th", r"8th grade")
# Staff/awareness observances that aren't family events.
OBSERVANCE = rx(r"\(annually", r"appreciation", r"hero day", r"principals'? day",
                r"teachers'? day", r"nurses'? day", r"custodi")
# Things that look like holidays but aren't days off.
NOT_OFF = rx(r"\((hindu|jewish|buddhist|muslim|islamic|christian|sikh|jain|"
             r"bahai|baha'i|religious)( holiday)?\)", r"^halloween$")


BACK_IN_SCHOOL = rx(r"reopens", r"school resumes", r"classes resume",
                    r"back to school", r"returns? from", r"^first day of school$")


def classify(source, summary):
    """Return (keep, kind) where kind is 'off', 'confirm', 'schedule' or 'event'."""
    s = summary.strip()
    if BACK_IN_SCHOOL.search(s):  # "School Reopens from Winter Break" isn't a day off
        return True, "schedule"
    if source == "AEA":
        if AB_WEEK.match(s) or MEETINGS.search(s) or OBSERVANCE.search(s):
            return False, None
        if AEA_MIDDLE_ONLY.search(s) and not NO_SCHOOL.search(s):
            return False, None
        if NO_SCHOOL.search(s):
            return True, "off"
        if SCHEDULE.search(s):
            return True, "schedule"
        return True, "event"  # family events, picture day, book fair...

    # PBMS school feed and SDUSD district feed
    if NOT_OFF.search(s):
        return False, None
    if NO_SCHOOL.search(s):
        return True, "off"
    if MEETINGS.search(s):
        return False, None
    if SCHEDULE.search(s):
        return True, "schedule"
    if NAMED_HOLIDAY.search(s):
        return True, "confirm"
    if source == "PBMS":
        return True, "event"  # anything school-specific that isn't a meeting
    return False, None  # district awareness days/months, testing windows, etc.


# ---------------------------------------------------------------- ics parsing

def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def parse_ics(text):
    lines = unfold(text)
    events, tz_blocks = [], []
    cur, tz, depth = None, None, 0
    for line in lines:
        if line == "BEGIN:VTIMEZONE":
            tz = [line]
            continue
        if tz is not None:
            tz.append(line)
            if line == "END:VTIMEZONE":
                tz_blocks.append("\r\n".join(tz))
                tz = None
            continue
        if line == "BEGIN:VEVENT":
            cur, depth = [], 0
            continue
        if cur is not None:
            if line.startswith("BEGIN:"):
                depth += 1
            elif line.startswith("END:") and depth:
                depth -= 1
            elif line == "END:VEVENT":
                events.append(event_from_lines(cur))
                cur = None
                continue
            if depth == 0 and not line.startswith(("BEGIN:", "END:")):
                cur.append(line)
    return events, tz_blocks


def event_from_lines(lines):
    props = {}
    for line in lines:
        m = re.match(r"([A-Z0-9-]+)((?:;[^:]*)?):(.*)", line)
        if not m:
            continue
        name, params, value = m.groups()
        props.setdefault(name, (params, value, line))
    return props


def unescape(v):
    return (v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\"))


def escape(v):
    return (v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
             .replace("\n", "\\n"))


def parse_when(prop):
    """Return (date_or_datetime, is_all_day)."""
    params, value, _ = prop
    if "VALUE=DATE" in params or re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value[:8], "%Y%m%d").date(), True
    tzm = re.search(r"TZID=([^;:]+)", params)
    if value.endswith("Z"):
        d = dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
    else:
        d = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
        d = d.replace(tzinfo=ZoneInfo(tzm.group(1)) if tzm else TZ)
    return d.astimezone(TZ), False


# ---------------------------------------------------------------- building

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "school-calendar-builder"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def load_events(texts):
    today = dt.datetime.now(TZ).date()
    cutoff = today - dt.timedelta(days=KEEP_PAST_DAYS)
    out, tz_blocks = [], []
    for source, text in texts.items():
        evs, tzs = parse_ics(text)
        tz_blocks.extend(tzs)
        for p in evs:
            if "DTSTART" not in p or p.get("STATUS", ("", "", ""))[1] == "CANCELLED":
                continue
            summary = unescape(p.get("SUMMARY", ("", "", ""))[1]).strip()
            keep, kind = classify(source, summary)
            if not keep:
                continue
            start, all_day = parse_when(p["DTSTART"])
            if "DTEND" in p:
                end, _ = parse_when(p["DTEND"])
            else:
                end = start + dt.timedelta(days=1) if all_day else start
            end_day = (end - dt.timedelta(days=1)) if all_day else end.date()
            if end_day < cutoff and "RRULE" not in p:
                continue
            out.append({
                "source": source,
                "school": "AEA" if source == "AEA" else "PBMS",
                "kind": kind,
                "summary": summary,
                "start": start, "end": end, "all_day": all_day,
                "uid": p.get("UID", ("", summary + str(start), ""))[1],
                "raw": p,
            })
    for school, first, last, title in MANUAL_DAYS_OFF:
        start = dt.date.fromisoformat(first)
        end = dt.date.fromisoformat(last) + dt.timedelta(days=1)
        if end - dt.timedelta(days=1) < cutoff:
            continue
        out.append({
            "source": "AEA" if school == "AEA" else "PBMS", "school": school,
            "kind": "off", "summary": title, "start": start, "end": end,
            "all_day": True, "uid": f"manual-{school}-{first}", "raw": {},
            "desc": "Added manually: missing from the school/district calendar feed.",
        })
    return out, tz_blocks


def weekdays(ev):
    """School days (Mon-Fri) covered by an all-day event."""
    if not ev["all_day"]:
        return set()
    d, days = ev["start"], set()
    while d < ev["end"]:
        if d.weekday() < 5:
            days.add(d)
        d += dt.timedelta(days=1)
    return days


def fmt_range(days):
    days = sorted(days)
    a, b = days[0], days[-1]
    f = lambda d: d.strftime("%b %-d")
    return f(a) if a == b else f"{f(a)}–{f(b)}"


def runs(days):
    """Split a set of weekdays into contiguous school-week runs."""
    days, out, cur = sorted(days), [], []
    for d in days:
        if cur and (d - cur[-1]).days > 3:
            out.append(cur)
            cur = []
        cur.append(d)
    if cur:
        out.append(cur)
    return out


def curate(events):
    off = [e for e in events if e["kind"] in ("off", "confirm") and e["all_day"]]

    # 1. Drop off-events covered by another off-event from the same school
    #    (e.g. Christmas Day inside Winter Break, Thanksgiving inside Fall Break).
    #    Longest first; on a tie a definite "off" beats a "confirm".
    off.sort(key=lambda e: (-(e["end"] - e["start"]).days, e["kind"] != "off"))
    covered, kept = set(), []
    for e in off:
        if any(k["school"] == e["school"] and k["start"] <= e["start"]
               and e["end"] <= k["end"] for k in kept):
            covered.add(id(e))
        else:
            kept.append(e)
    events = [e for e in events if id(e) not in covered]
    off = [e for e in off if id(e) not in covered]

    off_days = {"AEA": set(), "PBMS": set()}
    for e in off:
        off_days[e["school"]] |= weekdays(e)

    result, merged = [], set()
    other = {"AEA": "PBMS", "PBMS": "AEA"}

    for e in off:
        if id(e) in merged:
            continue
        mine, theirs = weekdays(e), off_days[other[e["school"]]]
        # 2. Identical day-off at both schools -> one [BOTH OFF] event.
        twin = next((o for o in off if o["school"] != e["school"] and id(o) not in merged
                     and weekdays(o) == mine and mine), None)
        if twin:
            merged.update({id(e), id(twin)})
            aea, pb = (e, twin) if e["school"] == "AEA" else (twin, e)
            note = ""
            if "confirm" in (aea["kind"], pb["kind"]):
                who = "PBMS" if pb["kind"] == "confirm" else "AEA"
                note = f" ({who}: confirm)"
            result.append(dict(aea, summary=f"[BOTH OFF] {aea['summary']}{note}",
                               uid="both-" + aea["uid"],
                               desc=f"AEA: {aea['summary']}\nPBMS: {pb['summary']}"))
            continue
        merged.add(id(e))
        tag = e["school"]
        title = e["summary"] + (" (confirm)" if e["kind"] == "confirm" else "")
        if mine and mine <= theirs:
            # 3. All of these days are also off at the other school,
            #    just described by a different-shaped event.
            result.append(dict(e, summary=f"[BOTH OFF] [{tag}] {title}"))
        elif mine and not (mine & theirs):
            # 4. Only this school is off.
            result.append(dict(e, summary=f"[{tag}] {title} — {other[tag]} not marked off"))
        else:
            # 5. Partial overlap: keep it, and add an alert for the gap days.
            result.append(dict(e, summary=f"[{tag}] {title}"))
            for run in runs(mine - theirs):
                start, end = run[0], run[-1] + dt.timedelta(days=1)
                result.append({
                    "source": e["source"], "school": tag, "kind": "alert",
                    "summary": f"[{tag} ONLY OFF] {other[tag]} is in school {fmt_range(run)}",
                    "start": start, "end": end, "all_day": True,
                    "uid": f"only-{tag}-{start.isoformat()}", "raw": {},
                    "desc": f"From: {e['summary']}",
                })

    for e in events:
        if e["kind"] in ("off", "confirm") and e["all_day"]:
            continue
        result.append(dict(e, summary=f"[{e['school']}] {e['summary']}"))

    def sort_key(e):
        s = e["start"]
        return (s if isinstance(s, dt.datetime) else
                dt.datetime.combine(s, dt.time.min, TZ)), e["summary"]
    return sorted(result, key=sort_key)


# ---------------------------------------------------------------- output

def fold(line):
    out, b = [], line.encode("utf-8")
    while len(b) > 75:
        cut = 75 if not out else 74
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode())
        b = b[cut:]
    out.append(b.decode())
    return "\r\n ".join(out)


def ics_date(d, all_day):
    if all_day:
        return f";VALUE=DATE:{d.strftime('%Y%m%d')}"
    return f":{d.astimezone(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def write_ics(events, path):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//school-calendar//curated AEA + PBMS//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:School (AEA + PBMS)",
             "X-WR-TIMEZONE:America/Los_Angeles",
             "X-WR-CALDESC:Curated: days off\\, minimum days\\, conferences and family events.",
             "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
             "X-PUBLISHED-TTL:PT12H"]
    for e in events:
        raw = e.get("raw", {})
        lines += ["BEGIN:VEVENT",
                  f"UID:{e['uid']}@school-calendar",
                  f"DTSTAMP:{stamp}",
                  "DTSTART" + ics_date(e["start"], e["all_day"]),
                  "DTEND" + ics_date(e["end"], e["all_day"]),
                  "SUMMARY:" + escape(e["summary"])]
        if "RRULE" in raw:
            lines.append(raw["RRULE"][2])
        if "RECURRENCE-ID" in raw:
            lines.append(raw["RECURRENCE-ID"][2])
        if "LOCATION" in raw and raw["LOCATION"][1]:
            lines.append(raw["LOCATION"][2])
        desc = e.get("desc")
        if desc is None and "DESCRIPTION" in raw:
            lines.append(raw["DESCRIPTION"][2])
        elif desc:
            lines.append("DESCRIPTION:" + escape(desc))
        lines += ["TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(fold(l) for l in lines) + "\r\n")


def when_text(e):
    s, en = e["start"], e["end"]
    if e["all_day"]:
        last = en - dt.timedelta(days=1)
        a = s.strftime("%a %b %-d")
        return a if last == s else f"{a} – {last.strftime('%a %b %-d')}"
    return f"{s.strftime('%a %b %-d')}, {s.strftime('%-I:%M %p').lower()}"


def snapshot(events):
    return {f"{e['uid']}|{e['start']}": {"summary": e["summary"], "when": when_text(e)}
            for e in events}


def diff(old, new):
    changes = []
    for k, v in new.items():
        if k not in old:
            changes.append(("Added", v["summary"], v["when"]))
        elif old[k] != v:
            changes.append(("Changed", v["summary"], v["when"]))
    for k, v in old.items():
        if k not in new:
            changes.append(("Removed", v["summary"], v["when"]))
    return changes


PAGE = string.Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>School Calendar</title>
<meta name="description" content="Curated AEA + PBMS school calendar: days off, minimum days, conferences and family events.">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🍎</text></svg>">
<style>
:root{--bg:#f7f4ee;--card:#fffdf9;--fg:#22211e;--muted:#6e6a61;--line:#e3ddd1;
--both:#2e6b4c;--both-bg:#e3f0e8;--aea:#2a5aa0;--aea-bg:#e4ecf8;--pbms:#9b4a12;--pbms-bg:#f8e9dd;
--alert:#b02a20;--alert-bg:#fbe5e2;--accent:#22211e;--accent-fg:#fffdf9}
@media (prefers-color-scheme:dark){:root{--bg:#151513;--card:#1e1d1a;--fg:#ecebe6;--muted:#9d998f;--line:#302e2a;
--both:#7fcfa2;--both-bg:#1d2e24;--aea:#8fb4f2;--aea-bg:#1c2536;--pbms:#f2a96f;--pbms-bg:#33241a;
--alert:#ff8f84;--alert-bg:#3a1e1b;--accent:#ecebe6;--accent-fg:#151513}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
main{max-width:780px;margin:0 auto;padding:28px 16px 64px}
header h1{font-size:1.9rem;line-height:1.15;margin:0;letter-spacing:-.01em}
header p{margin:4px 0 0;color:var(--muted)}
.status{display:flex;flex-wrap:wrap;gap:6px 18px;margin:14px 0 22px;font-size:.88rem;color:var(--muted)}
.status b{color:var(--fg);font-weight:600}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--both);margin-right:6px;vertical-align:1px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px}
h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:30px 0 10px}
.card h2{margin-top:0}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 12px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 14px;border-radius:8px;border:1px solid var(--line);
background:var(--card);color:var(--fg);font:inherit;font-size:.92rem;font-weight:600;text-decoration:none;cursor:pointer}
.btn.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
.btn:hover{filter:brightness(.96)}
.url{display:flex;gap:8px;align-items:stretch}
.url code{flex:1;min-width:0;padding:8px 10px;border-radius:8px;background:var(--bg);border:1px solid var(--line);
font-size:.82rem;overflow-x:auto;white-space:nowrap}
.hint{font-size:.84rem;color:var(--muted);margin:10px 0 0}
.next{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
.tile{border-radius:12px;padding:12px 14px}
.tile .lbl{font-size:.74rem;text-transform:uppercase;letter-spacing:.07em;font-weight:700}
.tile .d{font-size:1.15rem;font-weight:700;margin-top:2px}
.tile .n{font-size:.84rem;margin-top:2px;opacity:.85}
.t-both{background:var(--both-bg);color:var(--both)} .t-aea{background:var(--aea-bg);color:var(--aea)} .t-pbms{background:var(--pbms-bg);color:var(--pbms)}
@media (max-width:560px){.next{grid-template-columns:1fr}}
.legend{display:flex;flex-wrap:wrap;gap:8px;font-size:.84rem}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:.76rem;font-weight:700;white-space:nowrap}
.tag.both{background:var(--both-bg);color:var(--both)} .tag.aea{background:var(--aea-bg);color:var(--aea)}
.tag.pbms{background:var(--pbms-bg);color:var(--pbms)} .tag.alert{background:var(--alert-bg);color:var(--alert)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 6px}
.chip{padding:5px 12px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--fg);font:inherit;font-size:.85rem;cursor:pointer}
.chip[aria-pressed=true]{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
.month{font-weight:700;margin:18px 0 4px;font-size:1rem}
.row{display:grid;grid-template-columns:150px 1fr;gap:12px;padding:9px 0;border-bottom:1px solid var(--line)}
.row .w{color:var(--muted);font-size:.9rem;font-variant-numeric:tabular-nums}
.row .t{display:flex;flex-wrap:wrap;gap:6px;align-items:baseline}
.row.off .t span.s{font-weight:600}
@media (max-width:560px){.row{grid-template-columns:1fr;gap:2px}}
.chg{display:grid;grid-template-columns:80px 1fr;gap:10px;padding:6px 0;border-bottom:1px solid var(--line);font-size:.92rem}
.chg .a{font-weight:700} .chg .a.Added{color:var(--both)} .chg .a.Removed{color:var(--alert)} .chg .a.Changed{color:var(--pbms)}
.muted{color:var(--muted)}
footer{margin-top:36px;font-size:.82rem;color:var(--muted)}
footer a{color:inherit}
.lunch-head{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.lunch-head h2{margin:0;flex:1}
.lunch-head .btn{padding:4px 10px;font-size:.85rem}
#lunch-range{font-size:.88rem;font-weight:600;min-width:118px;text-align:center}
.lg{display:grid;grid-template-columns:96px 1fr 1fr;font-size:.9rem}
.lg>div{padding:8px 10px;border-top:1px solid var(--line)}
.lg .lh{border-top:none;font-size:.74rem;text-transform:uppercase;letter-spacing:.07em;font-weight:700;padding-top:0}
.lg .lh.aea{color:var(--aea)} .lg .lh.pbms{color:var(--pbms)}
.lg .ld{font-weight:600} .lg .ld small{display:block;font-weight:400;color:var(--muted)}
.lg .now{background:var(--bg)}
.lg ul{margin:0;padding-left:16px} .lg li+li{margin-top:1px}
.lunch-note{font-size:.84rem;margin:10px 0 0;padding:8px 10px;border-radius:8px;background:var(--alert-bg);color:var(--alert)}
.lunch-note a,#lunch-src a{color:inherit}
@media (max-width:560px){.lg{grid-template-columns:1fr}.lg .lh{display:none}
.lg .ld{border-top-width:2px}.lg .aea:not(.lh)::before,.lg .pbms:not(.lh)::before{display:block;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.lg .aea:not(.lh)::before{content:"AEA";color:var(--aea)} .lg .pbms:not(.lh)::before{content:"PBMS";color:var(--pbms)}}
</style></head><body><main>

<header>
  <h1>School Calendar</h1>
  <p>AEA + PBMS &mdash; days off, minimum days, conferences and family events. Everything else filtered out.</p>
</header>

<div class="status">
  <span><span class="dot"></span>Last checked <b>$checked</b></span>
  <span>Last change <b>$changed_at</b></span>
  <span><b>$count</b> upcoming events</span>
</div>

<div class="next">$next_tiles</div>

<section class="card" id="lunch">
  <div class="lunch-head">
    <h2>Lunch</h2>
    <button class="btn" id="lunch-prev" type="button" aria-label="Previous week">&lsaquo;</button>
    <span id="lunch-range"></span>
    <button class="btn" id="lunch-next" type="button" aria-label="Next week">&rsaquo;</button>
  </div>
  <div class="lg" id="lunch-grid"><p class="muted">Loading menus&hellip;</p></div>
  <div id="lunch-notes"></div>
  <p class="hint" id="lunch-src"></p>
</section>

<section class="card">
  <h2>Subscribe</h2>
  <div class="btns">
    <a class="btn primary" href="$google_url" target="_blank" rel="noopener">+ Google Calendar</a>
    <a class="btn" href="$webcal_url">+ Apple / Outlook</a>
    <a class="btn" href="school.ics" download>Download .ics</a>
  </div>
  <div class="url"><code id="feed">$feed_url</code><button class="btn" id="copy" type="button">Copy</button></div>
  <p class="hint">Google Calendar on a computer: <b>Other calendars &rarr; + &rarr; From URL</b>, paste the link.
  Subscribed calendars refresh on Google's schedule, usually within a day of a change here.</p>
</section>

<section class="card">
  <h2>How to read titles</h2>
  <div class="legend">
    <span><span class="tag both">BOTH OFF</span> both kids home</span>
    <span><span class="tag aea">AEA</span> elementary</span>
    <span><span class="tag pbms">PBMS</span> middle</span>
    <span><span class="tag alert">ONLY OFF</span> one kid home, one in school</span>
    <span class="muted">(confirm) = district holiday not explicitly marked no school</span>
  </div>
</section>

<h2>Latest changes <span class="muted" style="text-transform:none;letter-spacing:0">&middot; $changed_at</span></h2>
$changes

<h2>Upcoming</h2>
<div class="chips" role="group" aria-label="Filter">
  <button class="chip" data-f="all" aria-pressed="true">All</button>
  <button class="chip" data-f="off" aria-pressed="false">Days off</button>
  <button class="chip" data-f="aea" aria-pressed="false">AEA</button>
  <button class="chip" data-f="pbms" aria-pressed="false">PBMS</button>
</div>
<div id="list">$rows</div>

<footer>
  Built daily from the AEA, PBMS and SDUSD district public calendars. Source dates are the schools'; check with the school if something looks off.
  $repo_link
</footer>

<script>
(function(){
  var copy=document.getElementById('copy');
  copy.addEventListener('click',function(){
    var t=document.getElementById('feed').textContent;
    (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(function(){
      copy.textContent='Copied';setTimeout(function(){copy.textContent='Copy'},1500);
    },function(){
      var r=document.createRange();r.selectNodeContents(document.getElementById('feed'));
      var s=getSelection();s.removeAllRanges();s.addRange(r);
    });
  });
  var chips=document.querySelectorAll('.chip');
  chips.forEach(function(c){c.addEventListener('click',function(){
    var f=c.dataset.f;
    chips.forEach(function(x){x.setAttribute('aria-pressed',x===c)});
    document.querySelectorAll('#list .row').forEach(function(r){
      r.hidden=!(f==='all'||r.dataset.tags.split(' ').indexOf(f)>=0);
    });
    document.querySelectorAll('#list .month').forEach(function(m){
      var n=m.nextElementSibling,any=false;
      while(n&&!n.classList.contains('month')){if(!n.hidden)any=true;n=n.nextElementSibling}
      m.hidden=!any;
    });
  })});
})();
</script>
<script>
(function(){
  // Lunch menus: docs/lunch/*.json, refreshed by the lunch workflow.
  var DAYS=['Monday','Tuesday','Wednesday','Thursday','Friday'], aea=null, sd=null, off=0;
  function pad(n){return (n<10?'0':'')+n}
  function iso(d){return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())}
  function esc(t){return String(t).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
  function fmt(d){return d.toLocaleDateString(undefined,{month:'short',day:'numeric'})}
  function list(items,empty){return items&&items.length?'<ul>'+items.map(function(i){return '<li>'+esc(i)+'</li>'}).join('')+'</ul>':'<span class="muted">'+empty+'</span>'}
  function monday(o){var d=new Date();d.setHours(0,0,0,0);var w=d.getDay();
    if(w===6)d.setDate(d.getDate()+2);else if(w===0)d.setDate(d.getDate()+1);
    d.setDate(d.getDate()-(d.getDay()-1)+o*7);return d}
  function aeaCell(k){var m=aea&&aea.months[k.slice(0,7)];
    if(!m)return '<span class="muted">Not posted yet</span>';
    if(m.days[k])return list(m.days[k]);
    if(m.status!=='ok')return '<a href="'+esc(m.source)+'">See PDF</a>';
    return '<span class="muted">No lunch listed</span>'}
  function render(){
    var mon=monday(off),today=iso(new Date()),fri=new Date(mon);fri.setDate(mon.getDate()+4);
    document.getElementById('lunch-range').textContent=fmt(mon)+' – '+fmt(fri);
    var h='<div class="lh"></div><div class="lh aea">AEA</div><div class="lh pbms">PBMS</div>';
    DAYS.forEach(function(name,i){var d=new Date(mon);d.setDate(mon.getDate()+i);
      var k=iso(d),c=k===today?' now':'';
      h+='<div class="ld'+c+'">'+name.slice(0,3)+'<small>'+fmt(d)+'</small></div>'
        +'<div class="aea'+c+'">'+aeaCell(k)+'</div>'
        +'<div class="pbms'+c+'">'+list(sd&&sd.weekly[name],'No menu')+'</div>'});
    document.getElementById('lunch-grid').innerHTML=h;
    var notes=[],m=aea&&aea.months[iso(mon).slice(0,7)];
    if(m&&m.status!=='ok')notes.push('AEA menu couldn’t be read automatically — <a href="'+esc(m.source)+'">open the PDF</a>.');
    if(sd&&sd.changed)notes.push('SDUSD posted a new lunch menu — <a href="'+esc(sd.pdf)+'">see the PDF</a>. PBMS items may be out of date.');
    document.getElementById('lunch-notes').innerHTML=notes.map(function(n){return '<p class="lunch-note">'+n+'</p>'}).join('');
    var src=['<a href="'+(m?esc(m.source):'#')+'">AEA menu PDF</a>'];
    if(sd)src.push('<a href="'+esc(sd.pdf)+'">SDUSD menu PDF</a> (same every week, '+esc(sd.term)+')');
    document.getElementById('lunch-src').innerHTML='Sources: '+src.join(' &middot; ')+'. Menus subject to change.'}
  function get(u){return fetch(u,{cache:'no-cache'}).then(function(r){return r.ok?r.json():null}).catch(function(){return null})}
  Promise.all([get('lunch/aea.json'),get('lunch/sdusd.json')]).then(function(r){aea=r[0];sd=r[1];
    if(!aea&&!sd){document.getElementById('lunch').hidden=true;return}render()});
  document.getElementById('lunch-prev').onclick=function(){off--;render()};
  document.getElementById('lunch-next').onclick=function(){off++;render()};
})();
</script>
</main></body></html>
""")


TAG_RE = re.compile(r"^((?:\[[^\]]+\]\s*)+)")


def split_tags(summary):
    """'[BOTH OFF] [PBMS] Winter Break' -> (['BOTH OFF','PBMS'], 'Winter Break')."""
    m = TAG_RE.match(summary)
    if not m:
        return [], summary
    return re.findall(r"\[([^\]]+)\]", m.group(1)), summary[m.end():].strip()


def tag_class(tag):
    if tag == "BOTH OFF":
        return "both"
    if "ONLY OFF" in tag:
        return "alert"
    return "aea" if tag.startswith("AEA") else "pbms"


def row_flags(e):
    """Which filter chips an event belongs to, and which kids are home."""
    tags, _ = split_tags(e["summary"])
    both = "BOTH OFF" in tags
    off = both or e["kind"] in ("off", "confirm", "alert")
    flags = {"aea" if e["school"] == "AEA" else "pbms"}
    if both:
        flags |= {"aea", "pbms"}
    if off:
        flags.add("off")
    home = set()
    if both:
        home = {"AEA", "PBMS"}
    elif off:
        home = {e["school"]}
    return flags, home


def last_day(e):
    return (e["end"] - dt.timedelta(days=1)) if e["all_day"] else e["end"].date()


def write_page(events, changes, changed_at, path, feed_url, repo_url=None):
    today = dt.datetime.now(TZ).date()
    upcoming = [e for e in events if last_day(e) >= today]

    # Upcoming list
    rows, month = [], None
    for e in upcoming:
        d = e["start"] if e["all_day"] else e["start"].date()
        m = d.strftime("%B %Y")
        if m != month:
            rows.append(f'<div class="month">{m}</div>')
            month = m
        flags, _ = row_flags(e)
        tags, title = split_tags(e["summary"])
        pills = "".join(f'<span class="tag {tag_class(t)}">{html.escape(t)}</span>' for t in tags)
        rows.append(f'<div class="row{" off" if "off" in flags else ""}" data-tags="{" ".join(sorted(flags))}">'
                    f'<div class="w">{html.escape(when_text(e))}</div>'
                    f'<div class="t">{pills}<span class="s">{html.escape(title)}</span></div></div>')
    if not rows:
        rows.append('<p class="muted">Nothing upcoming.</p>')

    # "Next day off" tiles
    def first(pred):
        return next((e for e in upcoming if pred(e)), None)

    def tile(cls, label, e):
        if not e:
            return (f'<div class="tile {cls}"><div class="lbl">{label}</div>'
                    f'<div class="d">None listed</div></div>')
        start = max(e["start"] if e["all_day"] else e["start"].date(), today)
        days = (start - today).days
        when = "Today" if days == 0 else "Tomorrow" if days == 1 else f"in {days} days"
        _, title = split_tags(e["summary"])
        title = re.sub(r"\s*—.*$", "", title)
        return (f'<div class="tile {cls}"><div class="lbl">{label}</div>'
                f'<div class="d">{html.escape(when_text(e))}</div>'
                f'<div class="n">{when} &middot; {html.escape(title)}</div></div>')

    tiles = (tile("t-both", "Next day both off", first(lambda e: row_flags(e)[1] == {"AEA", "PBMS"}))
             + tile("t-aea", "Next AEA day off", first(lambda e: "AEA" in row_flags(e)[1]))
             + tile("t-pbms", "Next PBMS day off", first(lambda e: "PBMS" in row_flags(e)[1])))

    # Changes
    if changes:
        ch = "".join(f'<div class="chg"><span class="a {a}">{a}</span>'
                     f'<span>{html.escape(s)} <span class="muted">&middot; {html.escape(w)}</span></span></div>'
                     for a, s, w in changes)
    else:
        ch = '<p class="muted">No changes yet. Edits the schools make will show up here.</p>'

    webcal = re.sub(r"^https?://", "webcal://", feed_url)
    # Google's mobile app expects cid base64-encoded; a plain URL shows up as garbled symbols
    cid = base64.b64encode(feed_url.encode()).decode().rstrip("=")
    google = "https://calendar.google.com/calendar/render?cid=" + urllib.parse.quote(cid, safe="")
    now = dt.datetime.now(TZ).strftime("%a %b %-d, %-I:%M %p")
    repo = f'<a href="{html.escape(repo_url)}">Source on GitHub</a>.' if repo_url else ""
    with open(path, "w", encoding="utf-8") as f:
        f.write(PAGE.substitute(
            checked=now, changed_at=html.escape(changed_at), count=len(upcoming),
            next_tiles=tiles, google_url=html.escape(google), webcal_url=html.escape(webcal),
            feed_url=html.escape(feed_url), changes=ch, rows="".join(rows), repo_link=repo))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    texts = {}
    for name, url in FEEDS.items():
        try:
            texts[name] = fetch(url)
        except Exception as ex:  # one bad feed shouldn't wipe the calendar
            print(f"ERROR fetching {name}: {ex}", file=sys.stderr)
            sys.exit(1)

    events, _ = load_events(texts)
    curated = curate(events)

    snap_path = os.path.join(OUT_DIR, "events.json")
    state = {}
    if os.path.exists(snap_path):
        with open(snap_path) as f:
            state = json.load(f)
    old = state.get("events", {})
    new = snapshot(curated)
    now = dt.datetime.now(TZ).strftime("%a %b %-d, %-I:%M %p")
    if old:
        changes = diff(old, new)
        if changes:  # remember the most recent real change set
            state["last_changes"], state["last_changed"] = changes, now
    else:
        state["last_changes"], state["last_changed"] = [], now
    state["events"] = new

    write_ics(curated, os.path.join(OUT_DIR, "school.ics"))
    feed_url = os.environ.get("FEED_URL", "school.ics")
    write_page(curated, [tuple(c) for c in state["last_changes"]], state["last_changed"],
               os.path.join(OUT_DIR, "index.html"), feed_url,
               os.environ.get("REPO_URL"))
    with open(snap_path, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    print(f"{len(curated)} curated events, {len(state['last_changes'])} changes "
          f"(as of {state['last_changed']})")


if __name__ == "__main__":
    main()
