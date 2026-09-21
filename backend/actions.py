"""Upcoming Board agendas and constituent contacts, grounded in official SF sources."""

import argparse
import json
import logging
import math
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from backend.http import get_cached

BASE_URL = "https://www.sf.gov"
INDEX_URL = f"{BASE_URL}/departments--board-supervisors/events/upcoming"
CACHE_DIR = Path("data/raw/upcoming")
SFGOV_CACHE_DIR = Path("data/raw/sfgov")
DISTRICT_URL = (
    "https://services.arcgis.com/Zs2aNLFN00jrS4gG/arcgis/rest/services/"
    "Current_Supervisor_Districts_from_DataSF_pulled_nightly_/FeatureServer/0/query"
)
SUPERVISORS = {
    1: ("Connie Chan", "profile--connie-chan"),
    2: ("Stephen Sherrill", "profile--stephen-sherrill"),
    3: ("Danny Sauter", "profile--danny-sauter"),
    4: ("Alan Wong", "alan-wong"),
    5: ("Bilal Mahmood", "profile--bilal-mahmood"),
    6: ("Matt Dorsey", "profile--matt-dorsey"),
    7: ("Myrna Melgar", "profile--myrna-melgar"),
    8: ("Rafael Mandelman", "profile--rafael-mandelman"),
    9: ("Jackie Fielder", "profile--jackie-fielder"),
    10: ("Shamann Walton", "profile--shamann-walton"),
    11: ("Chyanne Chen", "profile--chyanne-chen"),
}
logger = logging.getLogger(__name__)
_upcoming: list[dict] | None = None
_upcoming_lock = Lock()


class _HTML(HTMLParser):
    """Read visible text and links without treating scripts as page content."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.hidden = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in {"p", "li", "br", "div", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden -= 1
        if tag in {"p", "li", "div", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    @property
    def text(self):
        return " ".join("".join(self.parts).split())


def _page_props(html: str) -> dict:
    # SF.gov embeds the same event data used to render its HTML in Next.js JSON.
    match = re.search(r'<script\b[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise ValueError("SF.gov page has no structured event data")
    return json.loads(match.group(1))["props"]["pageProps"]


@lru_cache(maxsize=32)
def _official_page(slug: str) -> dict:
    return _page_props(get_cached(f"{BASE_URL}/{slug}", SFGOV_CACHE_DIR / f"{slug}.html"))["page"]


def _content_text(value) -> str:
    """Read authored rich text, not navigation, media metadata or JSON identifiers."""
    if isinstance(value, str):
        return _HTML(value).text if "<" in value else ""
    if isinstance(value, list):
        return " ".join(filter(None, (_content_text(item) for item in value)))
    if isinstance(value, dict):
        return " ".join(filter(None, (_content_text(item) for key, item in value.items()
                                     if key not in {"meta", "image", "primary_agency", "partner_agencies"})))
    return ""


def _today() -> str:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def _schedule(page: dict) -> tuple[str, str]:
    value = page["date_time"][0]["value"]
    date = datetime.strptime(value["start_date"], "%Y-%m-%d").date().isoformat()
    time = datetime.strptime(value["start_time"], "%H:%M:%S").strftime("%I:%M %p").lstrip("0")
    return date, time


def _body(title: str) -> str:
    if title == "Full Board Meeting":
        return "Board of Supervisors"
    return re.sub(r"\s+Meeting$", "", title)


def _participation(page: dict, location: str, source_url: str, agenda_text: str = "") -> dict:
    notices = {n["value"]["title"]: _HTML(n["value"].get("text", "")).text
               for n in page.get("notices", []) if n.get("type") == "title_and_text"}
    procedures = notices.get("Meeting Procedures", "")
    call_in = notices.get("Public Comment Call-in Information", "")
    text = " ".join([_HTML(page.get("overview", "")).text, procedures, call_in])
    remote = None
    if not re.search(r"remote comment is not provided", text, re.I):
        phone = re.search(r"1-415-655-0001|\(?415\)?[ -]?\d{3}[ -]?\d{4}", call_in or text)
        meeting_id = re.search(r"(?:Meeting ID|Access code)\s*:?\s*([\d][\d #]+)", text, re.I)
        queue = re.search(r"[^.]*\*\s*3[^.]*\.", call_in)
        if phone and meeting_id and queue:
            remote = (f"Call {phone.group()}; Meeting ID: {meeting_id.group(1).strip()}. "
                      + re.sub(r"\*\s+3", "*3", queue.group().strip()))
    written = re.search(
        r"Persons unable to attend.*?Written communications should be submitted.*?"
        r"(?= Communications not received| COPYRIGHT:|$)", procedures, re.I,
    )
    # Keep the Board's limitation: a later vote need not allow another oral hearing.
    limitation = re.search(r"The full Board does not hold a second public hearing[^.]*\.", procedures, re.I)
    if not limitation:
        limitation = re.search(r"The public is encouraged to testify at Committee meetings, and the Board will not hold a second hearing\.", procedures, re.I)
    in_person = location
    if limitation:
        in_person += ". " + limitation.group()
    written_text = written.group().strip() if written else None
    deadline = re.search(r"by the time the proceedings begin", written_text or "", re.I)
    address = re.search(r"(?:submitted to the )(.+?94102)\b", written_text or "", re.I)
    # Only comment instructions establish a written-comment email, not an ADA notice.
    comment_text = text + " " + " ".join(agenda_text.split())
    email = re.search(r"comments?\s+by\s+(?:email|e-mail)\s*(?:to)?\s*:\s*([\w.+-]+@[\w.-]+\.\w+)",
                      comment_text, re.I)
    no_remote = re.search(r"remote comment is not provided at committees", text, re.I)
    return {"in_person": in_person, "remote": remote, "written": written_text,
            "remote_comment_available": remote is not None,
            "remote_reason": no_remote.group() if no_remote and remote is None else None,
            "written_comment": {"deadline": deadline.group() if deadline else None,
                                "email": email[1] if email else None,
                                "mailing_address": address[1] if address else None,
                                "committee_clerk_email": None},
            "source_url": source_url}


def _accessibility_action(events: list[dict]) -> dict | None:
    try:
        ada = _content_text(_official_page("ada-services"))
        interpretation = _content_text(_official_page("interpretation-and-translation-services"))
        _official_page("board-of-supervisors-meeting-information")
        email = re.search(r"please email\s+([\w.+-]+@[\w.-]+\.\w+)", ada, re.I)
        phone = re.search(r"(?:or call|at)\s+(\(?415\)?[ -]\d{3}-\d{4})", ada, re.I)
        tty = re.search(r"(\(?415\)?[ -]\d{3}-\d{4})\s*\(TTY\)", ada, re.I)
        notice = re.search(r"Requests made at least two \(2\) business days[^.]*\.", ada, re.I)
        language_notice = re.search(r"Two \(2\) business days[^.]*\.", interpretation, re.I)
        languages = []
        for event in events:
            match = re.search(r"Language services are available in (.+?) for requests",
                              " ".join(event["agenda_text"].split()), re.I)
            if match:
                languages.extend(name.strip() for name in re.split(r",|\band\b", match[1]) if name.strip())
        return {"type": "interpretation_or_ada_request",
                "deadline": " ".join(m.group() for m in [language_notice, notice] if m) or None,
                "email": email[1] if email else None, "phone": phone[1] if phone else None,
                "tty": tty[1] if tty else None, "languages": list(dict.fromkeys(languages)),
                "source_url": f"{BASE_URL}/ada-services",
                "interpretation_source_url": f"{BASE_URL}/interpretation-and-translation-services",
                "language_source_urls": list(dict.fromkeys(a["agenda_url"] for e in events for a in e["agendas"]))}
    except Exception as exc:
        logger.warning("Unable to load accessibility instructions: %s", exc)
        return None


def _fetch_event(event: dict, refresh: bool, *, include_past: bool = False) -> dict | None:
    slug = event["meta"]["slug"]
    url = f"{BASE_URL}/{slug}"
    html = get_cached(url, CACHE_DIR / f"{slug}.html", refresh)
    page = _page_props(html)["page"]
    if page.get("cancelled"):
        logger.warning("Skipping cancelled meeting: %s", url)
        return None
    date, time = _schedule(page)
    if date < _today() and not include_past:
        return None
    addresses = [a["value"] for a in page.get("meeting_location", []) if a["type"] == "address"]
    location = "; ".join(
        ", ".join(a[k] for k in ("line1", "line2", "city", "state", "zip") if a.get(k))
        for a in addresses
    )
    if not location:
        raise ValueError("No meeting location published")
    agenda_urls = list(dict.fromkeys(
        urljoin(BASE_URL, link) for link in _HTML(html).links
        if urlparse(link).path.lower().endswith(".pdf") and "agenda" in link.lower()
    ))
    agendas = []
    for i, agenda_url in enumerate(agenda_urls):
        suffix = "" if i == 0 else f"_{i + 1}"
        try:
            pdf = get_cached(agenda_url, CACHE_DIR / f"{slug}_agenda{suffix}.pdf", refresh, binary=True)
            text = "\n".join(p.extract_text(extraction_mode="layout") or "" for p in PdfReader(BytesIO(pdf)).pages)
            if not text.strip():
                raise ValueError("PDF has no extractable text")
            agendas.append({"agenda_url": agenda_url, "agenda_text": text})
        except Exception as exc:
            logger.warning("Skipping agenda %s: %s", agenda_url, exc)
    if not agenda_urls:
        logger.warning("No agenda published: %s", url)
    return {"title": page["title"], "body": _body(page["title"]), "url": url,
            "date": date, "time": time, "location": location, "agenda_urls": agenda_urls,
            "agendas": agendas, "agenda_text": "\n".join(a["agenda_text"] for a in agendas),
            "participation": _participation(page, location, url, "\n".join(a["agenda_text"] for a in agendas))}


def fetch_upcoming(refresh: bool = False) -> list[dict]:
    """Load Board/committee agendas once per process; refresh replaces disk caches too."""
    global _upcoming
    with _upcoming_lock:
        if _upcoming is not None and not refresh:
            return _upcoming
        events = []
        try:
            props = _page_props(get_cached(INDEX_URL, CACHE_DIR / "index.html", refresh))
            rows = list(props["events"])
            committees = {a["title"] for group in props.get("child_agency_groups", [])
                          if group["title"] == "Committees" for a in group["agencies"]}
            page_size = len(rows)
            if page_size:
                for page_no in range(2, math.ceil(props["total"] / page_size) + 1):
                    try:
                        more = _page_props(get_cached(f"{INDEX_URL}?page={page_no}",
                                                     CACHE_DIR / f"index_{page_no}.html", refresh))
                        rows.extend(more["events"])
                    except Exception as exc:
                        logger.warning("Skipping index page %s: %s", page_no, exc)
            seen = set()
            for row in rows:
                body = _body(row["title"])
                if body != "Board of Supervisors" and body not in committees:
                    continue
                slug = row["meta"]["slug"]
                if slug in seen:
                    continue
                seen.add(slug)
                try:
                    if row.get("cancelled") or _schedule(row)[0] < _today():
                        continue
                    event = _fetch_event(row, refresh)
                    if event:
                        events.append(event)
                except Exception as exc:
                    logger.warning("Skipping meeting %s: %s", slug, exc)
        except Exception as exc:
            logger.warning("Unable to load upcoming Board meetings: %s", exc)
        _upcoming = sorted(events, key=lambda e: (e["date"], e["body"]))
        return _upcoming


@lru_cache(maxsize=1)
def _agenda_events() -> list[dict]:
    """Include agendas still on disk after they have fallen off the upcoming index."""
    events = list(fetch_upcoming())
    seen = {event["url"] for event in events}
    for path in sorted(CACHE_DIR.glob("*_agenda.pdf")):
        slug = path.stem.removesuffix("_agenda")
        url = f"{BASE_URL}/{slug}"
        if url in seen or not (CACHE_DIR / f"{slug}.html").exists():
            continue
        try:
            event = _fetch_event({"meta": {"slug": slug}}, False, include_past=True)
            if event:
                events.append(event)
                seen.add(url)
        except Exception as exc:
            logger.warning("Unable to read cached agenda %s: %s", path, exc)
    return sorted(events, key=lambda event: event["date"])


_ITEM_HEADING = re.compile(r"(?m)^[ \t]*\d+\.[ \t]+(\d{6})\b[ \t]+\[")
_HISTORY_LINE = re.compile(
    r"(?m)^[ \t]*(\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}));[ \t]*"
    r"([^\n]+(?:\n(?!\d{1,2}/\d{1,2}/|Question:)[^\n]+)*)"
)


def _committee_name(text: str) -> str | None:
    match = re.search(
        r"(?:\bto (?:the )?|\bRecommendations? of the )"
        r"([A-Za-z &-]+? Committee)\b", " ".join(text.split()), re.I,
    )
    return " ".join(match[1].split()).title() if match else None


def parse_agenda_item_block(agenda_text: str, file_no: str) -> dict | None:
    """Only numbered legislative headings establish an item, never a passing mention."""
    headings = list(_ITEM_HEADING.finditer(agenda_text))
    for index, heading in enumerate(headings):
        if heading[1] != file_no:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(agenda_text)
        block = agenda_text[heading.start():end]
        boundary = re.search(r"(?m)^[ \t]*(?:\d{6}[ \t]+\[|ADJOURNMENT\b|"
                             r"PENDING LEGISLATION\b|The Levine Act\b|Agenda Item Information\b)", block)
        if boundary:
            block = block[:boundary.start()]
        # Page footers and the next section heading aren't facts about this item.
        lines = [line.strip() for line in block.splitlines()
                 if not re.search(r"City and County of San Francisco|Meeting Agenda|Printed at", line)]
        clean = "\n".join(lines)
        question = re.search(r"(?m)^Question:.*", clean)
        if question:
            clean = clean[:question.end()]
        title = re.search(r"\[([\s\S]*?)\]", clean)
        sponsors = re.search(r"(?m)^Sponsors?:[ \t]*(.+)", clean)
        roster = {name.rsplit(" ", 1)[-1].casefold(): name for name, _ in SUPERVISORS.values()}
        names = []
        for name in re.split(r"[;,]", sponsors[1]) if sponsors else []:
            name = name.strip()
            name = "Mayor" if name.casefold() == "mayor" else roster.get(name.casefold(), name)
            if name and name not in names:
                names.append(name)
        history = []
        for match in _HISTORY_LINE.finditer(clean):
            value = match[1]
            try:
                day = datetime.strptime(value, "%m/%d/%Y" if len(value.rsplit("/", 1)[1]) == 4 else "%m/%d/%y")
            except ValueError:
                continue
            history.append({"date": day.date().isoformat(), "action": " ".join(match[2].split()).rstrip(".")})
        # A recommendation heading before this item supplies its referring committee.
        preceding = agenda_text[:heading.start()]
        committees = list(re.finditer(r"Recommendations? of the ([A-Za-z &-]+ Committee)", preceding, re.I))
        committee = _committee_name(clean)
        if not committee and committees:
            committee = committees[-1][1].strip()
        return {
            "file_no": file_no, "title": " ".join(title[1].split()) if title else None,
            "sponsors": names, "fiscal_impact": True if re.search(r"\(Fiscal Impact\)", clean, re.I) else None,
            "history": history, "committee_name": committee,
            "documents": [{"label": "Document", "url": url} for url in dict.fromkeys(
                u.rstrip(".,;)") for u in re.findall(r"https?://[^\s<>]+", clean))],
            "agenda_item": " ".join(clean[:title.end()].split()) if title else None,
            "final_passage_scheduled": bool(re.search(r"Question:.*(?:FINALLY PASSED|FINAL PASSAGE)", clean, re.I)),
            "text": clean,
        }
    return None


@lru_cache(maxsize=1)
def _historical_items() -> dict[str, list[dict]]:
    """Index local meeting metadata and its cached HTML, preserving item-level provenance."""
    records = {}
    for path in sorted(Path("data/meetings").glob("*.json")):
        try:
            meeting = json.loads(path.read_text())
            clip_id = meeting["clip_id"]
            extract_path = Path(f"data/extract/{clip_id}.json")
            extracted = json.loads(extract_path.read_text())["items"] if extract_path.exists() else []
            outcomes = {str(item["meta_id"]): item for item in extracted}
            agenda_path = Path(f"data/raw/{clip_id}/agenda.html")
            html = agenda_path.read_text() if agenda_path.exists() else ""
            anchors = {}
            for match in re.finditer(r'<a\b([^>]*)>(.*?)</a>', html, re.S | re.I):
                text = _HTML(match[2]).text
                file = re.match(r"(\d{6})\s+(.+)", text)
                if file:
                    href = re.search(r'href="([^"]+)"', match[1])
                    anchors[file[1]] = {"title": file[2], "url": href[1] if href else None}
            for item in meeting.get("items", []):
                file_no = str(item.get("file_no") or "")
                if not re.fullmatch(r"\d{6}", file_no):
                    continue
                anchor = anchors.get(file_no, {})
                url = anchor.get("url") or (
                    f"https://sanfrancisco.granicus.com/player/clip/{clip_id}"
                    f"?view_id={meeting.get('view_id', 10)}&meta_id={item['meta_id']}&redirect=true"
                )
                outcome = outcomes.get(str(item["meta_id"]), {})
                summary = outcome.get("summary") or ""
                action = outcome.get("outcome") or "Scheduled"
                if action == "passed":
                    if re.search(r"\bfinally passed\b|\bfinal passage\b|\bsecond reading\b", summary, re.I):
                        action = "FINALLY PASSED"
                    elif re.search(r"\bfirst reading\b", summary, re.I):
                        action = "PASSED, ON FIRST READING"
                records.setdefault(file_no, []).append({
                    "title": item.get("title") or anchor.get("title"),
                    "committee_name": _committee_name(item.get("section") or ""),
                    "history": {"date": meeting["date"], "action": action,
                                "body": "Board of Supervisors", "source_url": url},
                    "source_url": url,
                })
        except (ValueError, KeyError, OSError) as exc:
            logger.warning("Unable to read historical agenda %s: %s", path, exc)
    return records

@lru_cache(maxsize=1)
def committees() -> list[dict]:
    """Current rosters only; never imply these members cast a historical vote."""
    directory = []
    try:
        html = get_cached(f"{BASE_URL}/board-of-supervisors-meeting-information",
                          SFGOV_CACHE_DIR / "board-of-supervisors-meeting-information.html")
        slugs = dict.fromkeys(urlparse(urljoin(BASE_URL, link)).path.strip("/")
                             for link in _HTML(html).links
                             if re.search(r"/department-[\w-]*committee/?$", link))
        for slug in slugs:
            try:
                page = _official_page(slug)
                source = f"{BASE_URL}/{slug}"
                members = [{"name": profile["value"]["profile_page"]["title"],
                            "role": profile["value"].get("role") or None}
                           for group in page.get("people", [])
                           for profile in group["value"].get("profiles", [])
                           if profile["type"] == "profile_page"]
                clerk = {"name": None, "email": None, "phone": None}
                for contact in page.get("contact", []):
                    value = contact["value"]
                    for field, label, target, key in [
                        ("phone", "owner", "phone", "phone_number"),
                        ("email", "title", "email", "email"),
                    ]:
                        for entry in value.get(field, []):
                            details = entry["value"]
                            name = re.search(r"Clerk:\s*(.+)", details.get(label, ""), re.I)
                            if name:
                                clerk["name"] = name[1].strip()
                                clerk[target] = details.get(key)
                directory.append({"name": page["title"], "current_members": members,
                                  "clerk": clerk, "source_url": source})
            except Exception as exc:
                logger.warning("Unable to parse committee %s: %s", slug, exc)
    except Exception as exc:
        logger.warning("Unable to load committee directory: %s", exc)
    return directory


def committee_for(name: str | None) -> dict | None:
    def normalized(value):
        return re.sub(r"\s+", " ", value.replace("&", "and").casefold()).strip()
    return next((committee for committee in committees()
                 if name and normalized(committee["name"]) == normalized(name)), None) if name else None



def _phase(history: list[dict]) -> str:
    phase = "unknown"
    for row in sorted(history, key=lambda row: row["date"]):
        action = row["action"].upper()
        if "SCHEDULED" in action and "FINAL PASSAGE" in action:
            phase = "final_passage_scheduled"
        elif re.search(r"\bFINALLY PASSED\b", action) and row["date"] <= _today():
            phase = "passed"
        elif "FIRST READING" in action and "PASSED" in action:
            phase = "first_reading"
        elif re.search(r"ASSIGNED|REFERRED|RECOMMENDED|CONTINUED|COMMITTEE HEARING", action):
            phase = "in_committee"
        elif re.search(r"RECEIVED FROM DEPARTMENT|INTRODUCED", action):
            phase = "introduced"
    return phase


def find_legislation(file_no: str) -> dict | None:
    """Combine exact-file agenda facts with dated outcomes from our existing meeting data."""
    if not isinstance(file_no, str) or not re.fullmatch(r"\d{6}", file_no):
        return None
    result = {"file_no": file_no, "title": None, "sponsors": [], "fiscal_impact": None,
              "history": [], "phase": "unknown", "committee": None,
              "your_supervisor_on_committee": None, "documents": [], "source_url": None}
    committee_name = None
    for item in _historical_items().get(file_no, []):
        result["title"], result["source_url"] = item["title"], item["source_url"]
        result["history"].append(dict(item["history"]))
        committee_name = item["committee_name"] or committee_name
    for event in _agenda_events():
        for agenda in event["agendas"]:
            item = parse_agenda_item_block(agenda["agenda_text"], file_no)
            if not item:
                continue
            source = agenda["agenda_url"]
            result.update(title=item["title"], source_url=source)
            result["sponsors"] = list(dict.fromkeys(result["sponsors"] + item["sponsors"]))
            if item["fiscal_impact"] is not None:
                result["fiscal_impact"] = item["fiscal_impact"]
            result["history"].extend({**row, "source_url": source} for row in item["history"])
            result["documents"].extend({**doc, "source_url": source} for doc in item["documents"])
            committee_name = event["body"] if event["body"].endswith("Committee") else item["committee_name"] or committee_name
            if event["date"] >= _today() and item["final_passage_scheduled"]:
                result["history"].append({"date": event["date"], "action": "FINAL PASSAGE SCHEDULED",
                                          "body": event["body"], "source_url": source})
            elif event["date"] >= _today() and event["body"].endswith("Committee"):
                result["history"].append({"date": event["date"], "action": "COMMITTEE HEARING SCHEDULED",
                                          "body": event["body"], "source_url": source})
    if not result["source_url"]:
        return None
    # Prefer the observed meeting outcome when both it and an agenda repeat the same history.
    unique = {}
    for row in result["history"]:
        key = (row["date"], row["action"].casefold().rstrip("."))
        unique.setdefault(key, row)
    result["history"] = sorted(unique.values(), key=lambda row: row["date"])
    result["phase"] = _phase(result["history"])
    result["committee"] = committee_for(committee_name)
    return result

def _mayor_review(legislation: dict) -> dict | None:
    final = [row for row in legislation["history"]
             if row["action"].upper() == "FINALLY PASSED" and row["date"] <= _today()]
    if not final:
        return None
    try:
        page = _official_page("legislation-passed")
        rule = None
        for section in page.get("additional_content", []):
            for item in section["value"].get("accordion_items", []):
                text = _content_text(item["value"].get("body", []))
                if "shall transmit to the Mayor" in text and "10 calendar days" in text:
                    rule = text
                    break
        if not rule:
            return None
        final_row = max(final, key=lambda row: row["date"])
        return {"type": "mayor_review", "rule": rule,
                "final_passage_date": final_row["date"],
                "deadline_date": (date.fromisoformat(final_row["date"]) + timedelta(days=10)).isoformat(),
                "deadline_basis": "Planning date: final passage + 10 calendar days. The legal review period starts on Mayor receipt; receipt date is not established by these sources.",
                "final_passage_source_url": final_row["source_url"],
                "source_url": f"{BASE_URL}/legislation-passed"}
    except Exception as exc:
        logger.warning("Unable to load Mayor review rule: %s", exc)
        return None



def _election_measures(file_no: str) -> list[dict]:
    """A letter alone is reused every election; require the packet's election date too."""
    packets = [(item["title"] or "", item["source_url"])
               for item in _historical_items().get(file_no, [])]
    for event in _agenda_events():
        for agenda in event["agendas"]:
            item = parse_agenda_item_block(agenda["agenda_text"], file_no)
            if item:
                packets.append((item["text"], agenda["agenda_url"]))
    candidates = [(text, source) for text, source in packets
                  if re.search(r"\b(?:Proposition|(?:ballot )?measure)\s+[A-Z]\b", text)]
    if not candidates:
        return []
    try:
        page = _official_page("qualified-ballot-measures")
        election = re.search(r"\b([A-Z][a-z]+ \d{1,2}, \d{4}) election\b", page.get("facts_title", ""))
        if not election:
            return []
        election_date = datetime.strptime(election[1], "%B %d, %Y").date().isoformat()
        source = f"{BASE_URL}/qualified-ballot-measures"
        measures = {}
        for section in page.get("additional_content", []):
            for entry in section["value"].get("accordion_items", []):
                value = entry["value"]
                heading = re.fullmatch(r"([A-Z])\s*-\s*(.+)", value.get("title", ""))
                if not heading:
                    continue
                documents = []
                for part in value.get("body", []):
                    if part["type"] != "text":
                        continue
                    for link in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', part["value"], re.S):
                        documents.append({"label": _HTML(link[2]).text,
                                          "url": urljoin(BASE_URL, link[1]), "source_url": source})
                measures[heading[1]] = {"type": "election_measure", "election_date": election_date,
                                       "letter": heading[1], "title": heading[2],
                                       "documents": documents, "source_url": source}
        matched = {}
        for text, packet_source in candidates:
            # Don't map references to old propositions to the current election's reused letter.
            if election[1] not in " ".join(text.split()):
                continue
            for letter in re.findall(r"\b(?:Proposition|(?:ballot )?measure)\s+([A-Z])\b", text):
                if letter in measures:
                    matched.setdefault(letter, {**measures[letter], "association_source_url": packet_source})
        return list(matched.values())
    except Exception as exc:
        logger.warning("Unable to load qualified ballot measures: %s", exc)
        return []


def find_actions(file_no: str, legislation: dict | None = None) -> list[dict]:
    """Return future scheduled items, excluding correspondence and pending-file lists."""
    if not isinstance(file_no, str) or not re.fullmatch(r"\d{6}", file_no):
        return []
    actions = []
    for event in fetch_upcoming():
        if event["date"] < _today():
            continue
        for agenda in event["agendas"]:
            item = parse_agenda_item_block(agenda["agenda_text"], file_no)
            if not item:
                continue
            participation = {**event["participation"],
                             "written_comment": dict(event["participation"]["written_comment"]),
                             "source_url": agenda["agenda_url"]}
            committee = committee_for(event["body"])
            if committee:
                participation["written_comment"]["committee_clerk_email"] = committee["clerk"]["email"]
                participation["written_comment"]["committee_source_url"] = committee["source_url"]
            actions.append({"type": "upcoming_agenda", "file_no": file_no,
                            **{k: event[k] for k in ("body", "date", "time", "location")},
                            "participation": participation,
                            "agenda_item": item["agenda_item"], "source_url": event["url"],
                            "agenda_url": agenda["agenda_url"]})
    actions.sort(key=lambda action: action["date"])
    if actions:
        event_urls = {action["source_url"] for action in actions}
        accessibility = _accessibility_action([event for event in fetch_upcoming() if event["url"] in event_urls])
        if accessibility:
            actions.append(accessibility)
    legislation = legislation if legislation is not None else find_legislation(file_no)
    if legislation:
        for district, (name, _) in SUPERVISORS.items():
            if name in legislation["sponsors"]:
                contact = _supervisor_contact(district)
                actions.append({"type": "contact_sponsor", **contact,
                                "source_url": contact["profile_url"]})
        committee = legislation["committee"]
        if committee and legislation["phase"] == "in_committee":
            actions.append({"type": "contact_committee_clerk", "committee": committee["name"],
                            **committee["clerk"], "source_url": committee["source_url"]})
        mayor = _mayor_review(legislation)
        if mayor:
            actions.append(mayor)
        actions.extend(_election_measures(file_no))
    return actions


def enrich_bullet(bullet: dict, supervisor: dict | None = None) -> dict:
    """Attach source-backed facts/actions; missing file evidence leaves an empty action list."""
    file_no = (bullet.get("evidence") or {}).get("file_no")
    if not file_no:
        match = re.search(r"\b\d{6}\b", bullet.get("item_title") or "")
        file_no = match.group() if match else None
    if not file_no:
        bullet.setdefault("actions", [])
        return bullet
    try:
        if "legislation" not in bullet:
            bullet["legislation"] = find_legislation(str(file_no))
        legislation = bullet["legislation"]
        if "actions" not in bullet:
            bullet["actions"] = find_actions(str(file_no), legislation)
        if supervisor and legislation and legislation["committee"]:
            legislation["your_supervisor_on_committee"] = any(
                member["name"] == supervisor["name"]
                for member in legislation["committee"]["current_members"]
            )
    except Exception as exc:
        logger.warning("Unable to enrich file %s: %s", file_no, exc)
        bullet.setdefault("actions", [])
    return bullet


@lru_cache(maxsize=11)
def _supervisor_contact(district: int) -> dict:
    name, slug = SUPERVISORS[district]
    result = {"district": district, "name": name, "profile_url": f"{BASE_URL}/{slug}"}
    try:
        page = _page_props(get_cached(result["profile_url"], CACHE_DIR / f"{slug}.html"))["page"]
        for contact in page.get("contact", []):
            value = contact["value"]
            for phone in value.get("phone", []):
                if phone["value"].get("owner", "").lower() == "voice":
                    result["phone"] = phone["value"]["phone_number"]
            for email in value.get("email", []):
                if email["value"].get("email"):
                    result["email"] = email["value"]["email"]
                    break
    except Exception as exc:
        logger.warning("Unable to parse supervisor contact %s: %s", result["profile_url"], exc)
    return result


def supervisor_for(lat: float, lon: float) -> dict | None:
    """Use the City's district polygon lookup, not a nearest-supervisor guess."""
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    lat, lon = round(lat, 6), round(lon, 6)
    query = urlencode({"geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
                       "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
                       "outFields": "sup_dist,sup_name", "returnGeometry": "false", "f": "json"})
    try:
        data = json.loads(get_cached(f"{DISTRICT_URL}?{query}", CACHE_DIR / f"district_{lat}_{lon}.json"))
        features = data.get("features", [])
        if len(features) != 1:
            return None
        district = int(features[0]["attributes"]["sup_dist"])
        if district not in SUPERVISORS:
            return None
        return dict(_supervisor_contact(district))
    except Exception as exc:
        logger.warning("Unable to look up supervisor: %s", exc)
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--file", dest="file_no")
    parser.add_argument("--legislation", metavar="FILE_NO")
    parser.add_argument("--committees", action="store_true")
    parser.add_argument("--at", help="latitude,longitude")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    if args.refresh:
        _official_page.cache_clear()
        committees.cache_clear()
        _historical_items.cache_clear()
        _agenda_events.cache_clear()
        _supervisor_contact.cache_clear()
        for path in SFGOV_CACHE_DIR.glob("*.html"):
            get_cached(f"{BASE_URL}/{path.stem}", path, refresh=True)
    if args.committees:
        print(json.dumps(committees(), indent=2))
        return
    events = fetch_upcoming(refresh=args.refresh)
    if args.legislation:
        legislation = find_legislation(args.legislation)
        print(json.dumps({"legislation": legislation,
                          "actions": find_actions(args.legislation, legislation)}, indent=2))
        return
    print(f"Upcoming Board events: {len(events)}; PDFs extracted: {sum(len(e['agendas']) for e in events)}")
    for event in events:
        parsed = {a["agenda_url"] for a in event["agendas"]}
        print(f"{event['body']} | {event['date']} {event['time']}")
        for url in event["agenda_urls"]:
            print(f"  {url} | PDF parsed: {url in parsed}")
        if not event["agenda_urls"]:
            print("  No agenda published")
    if args.file_no:
        print(json.dumps({"actions": find_actions(args.file_no)}, indent=2))
    if args.at:
        try:
            lat, lon = map(float, args.at.split(","))
        except ValueError:
            parser.error("--at requires latitude,longitude")
        print(json.dumps({"supervisor": supervisor_for(lat, lon)}, indent=2))


if __name__ == "__main__":
    main()
