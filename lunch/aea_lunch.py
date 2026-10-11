"""Download and parse the AEA (Albert Einstein Academies) elementary lunch menu PDF.

The menu is a monthly Numbers-exported calendar grid (Revolution Foods). Page
layout: one page per meal (Breakfast, Lunch, Snack, Extras, A La Carte). We only
read the "Lunch" page.

Usage:
    python lunch/aea_lunch.py              # current + next month, writes docs/lunch/aea.json
    python lunch/aea_lunch.py --pdf file.pdf --month 2026-10   # parse a local file
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import io
import json
import re
import statistics
import sys
import unicodedata
import urllib.request
from pathlib import Path

import pdfplumber

BASE = "https://www.aeacs.org"
MENU_PAGE = f"{BASE}/school-breakfast-lunch-menus"
UA = "Mozilla/5.0 (family lunch-menu page; personal use)"

# A line ending in one of these always continues onto the next line.
CONNECTORS = ("w/", "&", ",", "-")                      # trailing characters
CONNECTOR_WORDS = {"w/", "&", "and", "with", "on", "of", "or", "in"}  # whole last word


# ---------------------------------------------------------------- download ---

def _get(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            return data if data[:4] == b"%PDF" or url == MENU_PAGE else None
    except Exception:
        return None


def candidate_urls(year: int, month: int) -> list[str]:
    name = calendar.month_name[month]
    # Files land in the WordPress upload folder for the month they were
    # uploaded, which is often the month before the menu month.
    prev = dt.date(year, month, 1) - dt.timedelta(days=1)
    folders = [(year, month), (prev.year, prev.month)]
    names = [
        f"{year}-{name}-Elementary-Menu.pdf",
        f"{name}-{year}-Elementary-Menu.pdf",
        f"{year}-{name}-Elementary-Menu-1.pdf",
    ]
    return [f"{BASE}/wp-content/uploads/{y}/{m:02d}/{n}" for y, m in folders for n in names]


def links_from_menu_page(month: int) -> list[str]:
    """Fallback: look for an elementary menu PDF link on the menus page."""
    html = _get(MENU_PAGE)
    if not html:
        return []
    name = calendar.month_name[month].lower()
    links = re.findall(r'href="([^"]+\.pdf)"', html.decode("utf-8", "ignore"))
    return [u for u in links if "elementary" in u.lower() and name in u.lower()]


def fetch_pdf(year: int, month: int) -> tuple[bytes, str] | None:
    for url in candidate_urls(year, month) + links_from_menu_page(month):
        data = _get(url)
        if data:
            return data, url
    return None


# ------------------------------------------------------------------- parse ---

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)  # ﬁ -> fi
    return re.sub(r"\s+", " ", s).strip()


def _find_lunch_page(pdf):
    for page in pdf.pages:
        top = page.crop((0, 0, page.width, page.height * 0.12)).extract_text() or ""
        if re.search(r"\bLunch\b", top):
            return page
    return None


def _merge_lines(lines: list[dict], inner_w: float) -> list[str]:
    """Join wrapped lines back into menu items.

    Line spacing is identical for wraps and new items, so we decide per line:
      1. previous line ends in a connector ("w/", "&", ",", "and"...) -> wrap
      2. this line's first word would have fit on the previous line -> new item
         (the cell had room, so the break was deliberate)
      3. this line is a short tail ("Roll", "Fava Beans", "Baby Carrots") -> wrap
      4. otherwise -> new item
    inner_w is the widest line seen anywhere, which never exceeds the real
    cell width, so rule 2 only fires when the word truly would have fit.
    """
    items: list[list[dict]] = []
    for ln in lines:
        if not items:
            items.append([ln])
            continue
        prev = items[-1][-1]
        prev_text = prev["text"].rstrip()
        text = ln["text"]
        space_w = ln["size"] * 0.28
        fits = (prev["x1"] - prev["x0"]) + space_w + ln["first_word_w"] <= inner_w
        real_words = [w for w in text.split() if w not in ("&", "w/")]
        if (
            prev_text.endswith(CONNECTORS)
            or prev_text.split()[-1].lower() in CONNECTOR_WORDS
            or text[:1].islower()
            or text.startswith(("&", "w/", ","))
        ):
            items[-1].append(ln)
        elif fits:
            items.append([ln])
        elif len(real_words) <= 2 and "w/" not in text:
            items[-1].append(ln)
        else:
            items.append([ln])
    return [_norm(" ".join(l["text"] for l in it)) for it in items]


def parse_lunch(pdf_bytes: bytes, year: int, month: int) -> dict[str, list[str]]:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = _find_lunch_page(pdf)
        if page is None:
            raise ValueError("no Lunch page found")

        words = page.extract_words(extra_attrs=["size"])
        # Footer ("Lunch Includes:" box) bounds the grid from below.
        footer = min(
            (w["top"] for w in words if w["text"].startswith("Includes")),
            default=page.height,
        )
        typical = statistics.median(w["size"] for w in words if w["top"] < footer)
        anchors = [
            w for w in words
            if w["text"].isdigit() and 1 <= int(w["text"]) <= 31
            and w["size"] >= typical * 1.5 and w["top"] < footer
        ]
        if len(anchors) < 5:
            raise ValueError(f"only {len(anchors)} day numbers found")

        centers = sorted({round((a["x0"] + a["x1"]) / 2) for a in anchors})
        col_w = statistics.median(b - a for a, b in zip(centers, centers[1:]) if b - a > 50)
        row_tops = sorted({round(a["top"]) for a in anchors})

        cells = []
        for a in anchors:
            cx = (a["x0"] + a["x1"]) / 2
            nxt = [t for t in row_tops if t > a["top"] + 5]
            bottom = (nxt[0] - 2) if nxt else footer - 2
            box = (cx - col_w / 2 + 2, a["bottom"] + 1, cx + col_w / 2 - 2, bottom)
            lines = []
            for ln in page.crop(box).extract_text_lines(return_chars=True):
                text = ln["text"].strip()
                if not re.search(r"[A-Za-z]", text):
                    continue  # stray punctuation / day numbers
                chars = ln["chars"]
                first = text.split()[0]
                fw = sum(c["x1"] - c["x0"] for c in chars[: len(first)])
                lines.append({
                    "text": text, "x0": ln["x0"], "x1": ln["x1"],
                    "size": round(chars[0]["size"]), "first_word_w": fw,
                })
            cells.append((int(a["text"]), lines))

        # Usable text width per cell: widest line seen anywhere in the grid.
        all_w = [l["x1"] - l["x0"] for _, ls in cells for l in ls]
        inner_w = max(all_w) if all_w else col_w

        menu: dict[str, list[str]] = {}
        for day, lines in cells:
            items = []
            for it in _merge_lines(lines, inner_w):
                if it not in items:
                    items.append(it)
            if items:
                menu[dt.date(year, month, day).isoformat()] = items
        return menu


# --------------------------------------------------------------------- cli ---

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", help="parse a local PDF instead of downloading")
    ap.add_argument("--month", help="YYYY-MM for --pdf")
    ap.add_argument("--out", default="docs/lunch/aea.json")
    ap.add_argument("--scheduled", action="store_true",
                    help="only fetch on the 1st/15th, or daily while this month is missing")
    args = ap.parse_args()

    out = Path(args.out)
    result = json.loads(out.read_text()) if out.exists() else {"months": {}}

    if args.pdf:
        y, m = map(int, args.month.split("-"))
        jobs = [(y, m, Path(args.pdf).read_bytes(), args.pdf)]
    else:
        today = dt.date.today()
        this_key = f"{today.year}-{today.month:02d}"
        have_this = result["months"].get(this_key, {}).get("status") == "ok"
        if args.scheduled and today.day not in (1, 15) and have_this:
            print(f"{this_key} menu already loaded; next check on the 1st or 15th")
            return 0
        nxt = (today.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        jobs = []
        for d in (today, nxt):
            got = fetch_pdf(d.year, d.month)
            key = f"{d.year}-{d.month:02d}"
            if got:
                jobs.append((d.year, d.month, got[0], got[1]))
            elif key not in result["months"]:
                print(f"{key}: no PDF found yet", file=sys.stderr)

    for y, m, data, src in jobs:
        key = f"{y}-{m:02d}"
        entry = {"source": src, "checked": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")}
        try:
            entry["days"] = parse_lunch(data, y, m)
            entry["status"] = "ok"
        except Exception as e:  # keep the link so the page can fall back to it
            entry["status"] = f"parse failed: {e}"
            entry["days"] = result["months"].get(key, {}).get("days", {})
        result["months"][key] = entry
        print(f"{key}: {entry['status']} ({len(entry['days'])} days) from {src}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
