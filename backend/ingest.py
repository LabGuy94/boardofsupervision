"""Granicus agendas and timed captions -> README Contract 1 meeting files."""

import argparse
from bisect import bisect_right
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import sys

import httpx
import webvtt

from backend.http import get_cached


BASE_URL = "https://sanfrancisco.granicus.com"
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
BODIES = {
    10: "Board of Supervisors",
    12: "Land Use",
    7: "Budget & Finance",
    13: "Rules",
    44: "Public Safety",
    11: "GAO",
    21: "Police Commission",
    55: "SFMTA Board",
    22: "PUC",
    91: "Rec & Park",
    92: "Port",
    45: "Small Business",
    47: "SFUSD Board of Ed",
}
UUID_PATTERN = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
SECTION_PATTERN = re.compile(
    r"^(CONSENT AGENDA|REGULAR AGENDA|UNFINISHED BUSINESS|NEW BUSINESS|"
    r"SPECIAL ORDER|PUBLIC COMMENT|ROLL CALL FOR INTRODUCTIONS|ROLL CALL|"
    r"ADJOURNMENT|COMMITTEE REPORTS|FOR ADOPTION WITHOUT COMMITTEE REFERENCE|"
    r"IMPERATIVE AGENDA|RECOMMENDATIONS?\b.*)",
    re.IGNORECASE,
)


def parse_listing(html: str) -> list[dict]:
    rows = {}
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr\s*>", html, re.DOTALL | re.IGNORECASE):
        clip = re.search(r"clip_id=(\d+)", row)
        date = re.search(r"\b\d{10}\s*</span>\s*(\d{2}/\d{2}/\d{2})", row)
        if not clip or not date:
            continue
        duration = re.search(r"(\d+)h\s*(\d+)m", unescape(row))
        uuid = re.search(rf"archive-video\.granicus\.com/sanfrancisco/sanfrancisco_({UUID_PATTERN})\.mp3", row)
        clip_id = int(clip.group(1))
        rows[clip_id] = {
            "clip_id": clip_id,
            "date": datetime.strptime(date.group(1), "%m/%d/%y").date().isoformat(),
            "uuid": uuid.group(1) if uuid else None,
            "duration_sec": int(duration.group(1)) * 3600 + int(duration.group(2)) * 60 if duration else None,
        }
    return sorted(rows.values(), key=lambda row: (row["date"], row["clip_id"]), reverse=True)


class _AgendaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.section = None
        self.divs = []
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.divs.append({
                "parts": [],
                "has_item": False,
                "is_agenda": "agenda" in attrs.get("class", "").split(),
            })
        elif tag == "a":
            match = re.search(r"meta_id=(\d+)", attrs.get("href", ""))
            if match:
                self.anchor = (int(match.group(1)), [])
                if self.divs:
                    self.divs[-1]["has_item"] = True
        elif tag == "br":
            self.handle_data(" ")

    def handle_data(self, data):
        if self.divs:
            self.divs[-1]["parts"].append(data)
        if self.anchor is not None:
            self.anchor[1].append(data)

    def update_section(self, label):
        label = re.sub(r"^\d+\s+", "", label)
        match = SECTION_PATTERN.match(label)
        if match:
            self.section = match.group(1).upper()

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            meta_id, parts = self.anchor
            label = " ".join("".join(parts).split())
            file_match = re.match(r"^(\d{6})\s+(.*)$", label)
            if not file_match:
                self.update_section(label)
            self.items.append({
                "meta_id": meta_id,
                "file_no": file_match.group(1) if file_match else None,
                "title": file_match.group(2) if file_match else label,
                "section": self.section,
            })
            self.anchor = None
        elif tag == "div" and self.divs:
            div = self.divs.pop()
            label = " ".join("".join(div["parts"]).split())
            file_match = re.match(r"^(\d{6})\s+(.*)$", label)
            if file_match and div["is_agenda"] and not div["has_item"]:
                self.items.append({
                    "meta_id": None,
                    "file_no": file_match.group(1),
                    "title": file_match.group(2),
                    "section": self.section,
                })
            elif not file_match:
                self.update_section(label)


def parse_agenda(html: str) -> list[dict]:
    parser = _AgendaParser()
    parser.feed(html)
    parser.close()
    return parser.items


def parse_time_index(html: str) -> dict[int, int]:
    return {
        int(meta_id): int(time)
        for time, meta_id in re.findall(r'\{"time":(\d+),"type":"Agenda","id":"(\d+)"\}', html)
    }


def parse_media(html: str) -> dict:
    urls = re.findall(r'https?://[^\s"\'<>]+', unescape(html).replace(r"\/", "/"))
    media = {
        extension: next((url for url in urls if re.search(rf"\.{extension}(?:\?[^\s]*)?$", url)), None)
        for extension in ("mp3", "mp4", "m3u8")
    }
    uuid = re.search(rf"sanfrancisco_({UUID_PATTERN})", html)
    media["uuid"] = uuid.group(1) if uuid else None
    return media


def _seconds(timestamp: str) -> float:
    hours, minutes, seconds, milliseconds = re.split(r"[:.]", timestamp)
    return (int(hours) * 3600000 + int(minutes) * 60000 + int(seconds) * 1000 + int(milliseconds)) / 1000


def parse_vtt(text: str) -> list[dict]:
    return [
        {"t0": _seconds(cue.start), "t1": _seconds(cue.end), "text": " ".join(cue.text.splitlines())}
        for cue in webvtt.from_string(text)
    ]


def assemble(listing_row: dict, items: list[dict], times: dict[int, int], cues: list[dict]) -> dict:
    timed_items = []
    preceding = None
    for item in items:
        if item["meta_id"] in times:
            preceding = {**item, "start_sec": times[item["meta_id"]], "sub_items": []}
            timed_items.append(preceding)
        elif preceding is not None:
            preceding["sub_items"].append({
                "meta_id": item["meta_id"],
                "file_no": item["file_no"],
                "title": item["title"],
            })
    timed_items.sort(key=lambda item: item["start_sec"])
    if not timed_items:
        raise ValueError("No agenda items with timestamps; cannot assign captions")
    duration = math.ceil(max(listing_row.get("duration_sec") or 0, max((cue["t1"] for cue in cues), default=0)))
    starts = [item["start_sec"] for item in timed_items]
    for index, item in enumerate(timed_items):
        item["end_sec"] = starts[index + 1] if index + 1 < len(starts) else duration
    assigned_cues = [
        {**cue, "meta_id": timed_items[max(0, bisect_right(starts, cue["t0"]) - 1)]["meta_id"]}
        for cue in cues
    ]
    view_id = listing_row.get("view_id", 10)
    return {
        "clip_id": listing_row["clip_id"],
        "view_id": view_id,
        "body": BODIES[view_id],
        "date": listing_row["date"],
        "uuid": listing_row.get("uuid"),
        "duration_sec": duration,
        "items": timed_items,
        "cues": assigned_cues,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--view", type=int, choices=sorted(BODIES), default=10)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--clip", type=int)
    parser.add_argument("--from", dest="from_date", help="First meeting date, YYYY-MM-DD (inclusive)")
    parser.add_argument("--to", dest="to_date", help="Last meeting date, YYYY-MM-DD (inclusive)")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.offline and args.refresh:
        parser.error("--offline and --refresh cannot be used together")

    def fetch(url: str, path: Path) -> str:
        if args.offline:
            return path.read_text(encoding="utf-8")
        return get_cached(url, path, refresh=args.refresh)

    try:
        listing = parse_listing(fetch(
            f"{BASE_URL}/ViewPublisher.php?view_id={args.view}",
            DATA_DIR / "raw" / f"view_{args.view}.html",
        ))
    except FileNotFoundError as exc:
        parser.error(f"Offline cache missing: {exc.filename}")
    if args.clip is not None:
        selected = [row for row in listing if row["clip_id"] == args.clip]
        if not selected:
            parser.error(f"Clip {args.clip} not found in view {args.view}")
    else:
        selected = [
            row for row in listing
            if (not args.from_date or row["date"] >= args.from_date)
            and (not args.to_date or row["date"] <= args.to_date)
        ][:args.limit]
    output_dir = DATA_DIR / "meetings"
    output_dir.mkdir(parents=True, exist_ok=True)
    for row in selected:
        clip_id = row["clip_id"]
        raw_dir = DATA_DIR / "raw" / str(clip_id)
        try:
            agenda = fetch(
                f"{BASE_URL}/GeneratedAgendaViewer.php?view_id={args.view}&clip_id={clip_id}",
                raw_dir / "agenda.html",
            )
            items = parse_agenda(agenda)
            indexed = next((item["meta_id"] for item in items if item.get("meta_id") is not None), None)
            if indexed is None:
                print(f"Skipping {clip_id}: no indexed agenda items", file=sys.stderr)
                continue
            player = fetch(
                f"{BASE_URL}/player/clip/{clip_id}?view_id={args.view}&meta_id={indexed}&redirect=true",
                raw_dir / "player.html",
            )
            try:
                captions = fetch(f"{BASE_URL}/videos/{clip_id}/captions.vtt", raw_dir / "captions.vtt")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                print(f"Skipping {clip_id}: captions.vtt returned HTTP 404", file=sys.stderr)
                continue
        except FileNotFoundError as exc:
            print(f"Skipping {clip_id}: offline cache missing {exc.filename}", file=sys.stderr)
            continue
        meeting = assemble(
            {**row, "view_id": args.view, "uuid": row["uuid"] or parse_media(player)["uuid"]},
            items,
            parse_time_index(player),
            parse_vtt(captions),
        )
        (output_dir / f"{clip_id}.json").write_text(json.dumps(meeting, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{clip_id} {meeting['date']} items={len(meeting['items'])} cues={len(meeting['cues'])}", flush=True)


if __name__ == "__main__":
    main()
