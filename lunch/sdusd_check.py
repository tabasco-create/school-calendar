"""Watch the SDUSD Middle/High School Lunch PDF for changes.

The district publishes one fixed weekly menu per semester as a flat image
(no text to parse), so the menu itself lives hand-entered in
docs/lunch/sdusd.json. This script only detects when the district posts a
new PDF, and sets "changed": true so the page shows a notice + link.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

DATA = Path("docs/lunch/sdusd.json")
UA = "Mozilla/5.0 (family lunch-menu page; personal use)"


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def current_pdf_link(page_url: str) -> str | None:
    """Find the 'Middle/High School Lunch' link on the district menus page."""
    html = get(page_url).decode("utf-8", "ignore")
    for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        label = re.sub(r"<[^>]+>|&nbsp;", " ", text)
        if re.search(r"middle\s*/\s*high", label, re.I) and re.search(r"lunch", label, re.I):
            return href.replace("&amp;", "&")
    return None


def drive_id(url: str) -> str | None:
    m = re.search(r"/d/([\w-]{20,})", url) or re.search(r"[?&]id=([\w-]{20,})", url)
    return m.group(1) if m else None


def main() -> int:
    if "--scheduled" in sys.argv and dt.date.today().day not in (1, 15):
        print("SDUSD check runs on the 1st and 15th only")
        return 0
    data = json.loads(DATA.read_text())
    data["checked"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")

    try:
        link = current_pdf_link(data["source_page"]) or data["pdf"]
    except Exception as e:
        print(f"could not read menus page: {e}", file=sys.stderr)
        link = data["pdf"]

    if drive_id(link) != drive_id(data["pdf"]):
        print(f"new menu PDF posted: {link}")
        data["changed"] = True
        data["pdf"] = link

    fid = drive_id(link)
    try:
        pdf = get(f"https://drive.usercontent.google.com/download?id={fid}&export=download")
        if pdf[:4] == b"%PDF":
            digest = hashlib.sha256(pdf).hexdigest()
            if data.get("pdf_sha256") and digest != data["pdf_sha256"]:
                print("menu PDF contents changed")
                data["changed"] = True
            data["pdf_sha256"] = data.get("pdf_sha256") or digest
            if data["changed"]:
                data["new_sha256"] = digest
        else:
            print("Drive did not return a PDF", file=sys.stderr)
    except Exception as e:
        print(f"download failed: {e}", file=sys.stderr)

    DATA.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print("changed" if data["changed"] else "unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
