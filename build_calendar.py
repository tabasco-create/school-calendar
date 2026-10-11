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
import datetime as dt
import html
import json
import os
import re
import sys
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

# ---------------------------------------------------------------- filters

def rx(*words):
    return re.compile("|".join(words), re.I)

# Means students are home.
NO_SCHOOL = rx(r"no school", r"vacation", r"\bbreak\b", r"non-?instructional",
               r"school closed", r"schools closed", r"furlough")
# Shortened day or schedule landmark worth seeing.
SCHEDULE = rx(r"minimum day", r"early release", r"early dismissal",
              r"late start", r"conferences?", r"first day of school",
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
# Things that look like holidays but aren't days off.
NOT_OFF = rx(r"\((hindu|jewish|buddhist|muslim|islamic|christian|sikh|jain|"
             r"bahai|baha'i|religious)( holiday)?\)", r"^halloween$")


BACK_IN_SCHOOL = rx(r"reopens", r"school resumes", r"classes resume",
                    r"back to school", r"returns? from", r"first day of school")


def classify(source, summary):
    """Return (keep, kind) where kind is 'off', 'confirm', 'schedule' or 'event'."""
    s = summary.strip()
    if BACK_IN_SCHOOL.search(s):  # "School Reopens from Winter Break" isn't a day off
        return True, "schedule"
    if source == "AEA":
        if AB_WEEK.match(s) or MEETINGS.search(s):
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


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>School Calendar</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e2dc;--both:#2f6f4f;--aea:#2b5ea7;--pbms:#9a4d16;--alert:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#ecebe6;--muted:#9c9b95;--line:#2c2b29;--both:#6fc49a;--aea:#86aef0;--pbms:#f0a46a;--alert:#ff8a80}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,system-ui,Segoe UI,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px 64px}}
h1{{font-size:1.6rem;margin:0 0 4px}} h2{{font-size:1.05rem;margin:32px 0 8px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}}
.meta{{color:var(--muted);font-size:.9rem}}
.sub{{margin:16px 0;padding:12px 14px;border:1px solid var(--line);border-radius:8px;font-size:.9rem;word-break:break-all}}
table{{width:100%;border-collapse:collapse}} td{{padding:7px 8px 7px 0;border-bottom:1px solid var(--line);vertical-align:top}}
td.w{{white-space:nowrap;color:var(--muted);width:1%;padding-right:16px;font-variant-numeric:tabular-nums}}
.both{{color:var(--both);font-weight:600}} .aea{{color:var(--aea)}} .pbms{{color:var(--pbms)}} .alert{{color:var(--alert);font-weight:600}}
.month td{{border:0;padding-top:20px;font-weight:600}}
</style></head><body><main>
<h1>School Calendar</h1>
<div class="meta">AEA + PBMS, curated &middot; Last updated {updated} &middot; {count} upcoming events</div>
<div class="sub">Subscribe in Google Calendar (Other calendars &rarr; From URL):<br><code>{feed_url}</code></div>
<h2>Latest changes <span style="text-transform:none;letter-spacing:0">({changed_at})</span></h2>
{changes}
<h2>Upcoming</h2>
<table>{rows}</table>
</main></body></html>
"""


def css_class(summary):
    if summary.startswith("[BOTH OFF]"):
        return "both"
    if "ONLY OFF]" in summary:
        return "alert"
    return "aea" if summary.startswith("[AEA]") else "pbms"


def write_page(events, changes, changed_at, path, feed_url):
    today = dt.datetime.now(TZ).date()
    upcoming = [e for e in events
                if ((e["end"] - dt.timedelta(days=1)) if e["all_day"] else e["end"].date()) >= today]
    rows, month = [], None
    for e in upcoming:
        d = e["start"] if e["all_day"] else e["start"].date()
        m = d.strftime("%B %Y")
        if m != month:
            rows.append(f'<tr class="month"><td colspan="2">{m}</td></tr>')
            month = m
        rows.append(f'<tr><td class="w">{html.escape(when_text(e))}</td>'
                    f'<td class="{css_class(e["summary"])}">{html.escape(e["summary"])}</td></tr>')
    if changes:
        ch = "<table>" + "".join(
            f'<tr><td class="w">{a}</td><td>{html.escape(s)} <span class="meta">({html.escape(w)})</span></td></tr>'
            for a, s, w in changes) + "</table>"
    else:
        ch = '<p class="meta">No changes yet.</p>'
    now = dt.datetime.now(TZ).strftime("%a %b %-d, %Y at %-I:%M %p %Z")
    with open(path, "w", encoding="utf-8") as f:
        f.write(PAGE.format(updated=now, changed_at=html.escape(changed_at),
                            count=len(upcoming), feed_url=html.escape(feed_url),
                            changes=ch, rows="".join(rows)))


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
    now = dt.datetime.now(TZ).strftime("%a %b %-d, %Y at %-I:%M %p %Z")
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
               os.path.join(OUT_DIR, "index.html"), feed_url)
    with open(snap_path, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    print(f"{len(curated)} curated events, {len(state['last_changes'])} changes "
          f"(as of {state['last_changed']})")


if __name__ == "__main__":
    main()
