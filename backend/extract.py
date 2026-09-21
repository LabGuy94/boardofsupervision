"""Extract structured data from meeting transcripts using an LLM.

CLI: python -m backend.extract [--clip 53160] [--all] [--force]
"""

import argparse
import json
import sys
import re
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, field_validator

from backend.llm import complete
from backend.roster import check_roll_call, roster_for

MEETINGS_DIR = Path("data/meetings")
EXTRACT_DIR = Path("data/extract")
PROMPT_PATH = Path("backend/prompts/extract.md")

VALID_TOPICS = {
    "housing", "homelessness", "public_safety", "transit", "budget",
    "settlements", "parks", "small_business", "environment", "health",
    "labor", "planning_land_use", "elections_governance", "other",
}

VALID_OUTCOMES = {"passed", "failed", "continued", "referred", "no_action", "unknown"}

MAX_DOCUMENT_CHARS = 180_000 * 4


class Speaker(BaseModel):
    name: str
    stance: str
    quote: str
    t0: float
    verified: bool = False


class Vote(BaseModel):
    name: str
    vote: str
    inferred: bool = True


class ExtractedItem(BaseModel):
    meta_id: int
    summary: str
    outcome: str
    topics: list[str]
    speakers: list[Speaker] = []
    votes: list[Vote] = []
    public_comment_themes: list[str] = []

    @field_validator("topics")
    @classmethod
    def validate_topics(cls, v):
        return [t for t in v if t in VALID_TOPICS]

    @field_validator("outcome")
    @classmethod
    def validate_outcome(cls, v):
        return v if v in VALID_OUTCOMES else "unknown"


class ExtractResult(BaseModel):
    clip_id: int
    items: list[ExtractedItem]


def item_cues(meeting: dict) -> tuple[dict[int, list], dict[int, int]]:
    """Share a following discussion window with short called-together entries."""
    cues: dict[int, list] = {}
    for cue in meeting["cues"]:
        cues.setdefault(cue["meta_id"], []).append(cue)
    grouped = {}
    following = None
    for item in reversed(meeting["items"]):
        meta_id = item["meta_id"]
        if item["end_sec"] - item["start_sec"] < 5 and following is not None:
            grouped[meta_id] = following
            cues[meta_id] = cues.get(meta_id, []) + cues.get(following, [])
        else:
            following = meta_id
    return cues, grouped


def document_parts(meeting: dict) -> tuple[str, list[str]]:
    roster = roster_for(meeting["date"])
    header = (
        f"MEETING {meeting['clip_id']} — {meeting['body']} — {meeting['date']}\n"
        "SUPERVISORS: " + ", ".join(f"{s['name']} (District {s['district']})" for s in roster) + "\n\n"
    )
    cues_by_meta, grouped = item_cues(meeting)
    sections = []
    for item in meeting["items"]:
        meta_id = item["meta_id"]
        file_part = f" file={item['file_no']}" if item.get("file_no") else ""
        section_part = f" section={item['section']}" if item.get("section") else ""
        lines = [
            f"### ITEM meta_id={meta_id}{file_part}{section_part} start={item['start_sec']}s",
            f"TITLE: {item['title']}",
        ]
        if meta_id in grouped:
            lines.append(f"CALLED TOGETHER WITH meta_id={grouped[meta_id]}; shared captions follow. Attribute outcomes/votes only when supported for this item.")
        for sub in item.get("sub_items", []):
            prefix = f"{sub['file_no']} " if sub.get("file_no") else ""
            lines.append(f"  SUB-ITEM: {prefix}{sub['title']}")
        cues = cues_by_meta.get(meta_id, [])
        lines.extend(f"[{cue['t0']}] {cue['text']}" for cue in cues)
        if not cues:
            lines.append("NO CAPTIONS FOR THIS ITEM: do not infer a discussion, outcome, speakers, or votes.")
        sections.append("\n".join(lines) + "\n\n")
    return header, sections


def build_document(meeting: dict) -> str:
    header, sections = document_parts(meeting)
    return header + "".join(sections)


def build_documents(meeting: dict, max_chars: int = MAX_DOCUMENT_CHARS) -> list[str]:
    header, sections = document_parts(meeting)
    documents = []
    current = header
    for section in sections:
        if len(current) + len(section) > max_chars and current != header:
            documents.append(current)
            current = header
        current += section
    if current != header:
        documents.append(current)
    return documents


def verify_quotes(meeting: dict, result: dict) -> dict:
    cues_by_meta, _ = item_cues(meeting)
    counts = {"verified": 0, "partial": 0, "dropped": 0}
    normalize = lambda text: " ".join(text.casefold().split())
    for item in result["items"]:
        speakers = []
        for speaker in item.get("speakers", []):
            quote = normalize(speaker.get("quote") or "")
            t0 = speaker.get("t0")
            window = " ".join(
                cue["text"] for cue in cues_by_meta.get(item["meta_id"], [])
                if t0 is not None and t0 - 2 <= cue["t0"] <= t0 + 60
            )
            window = normalize(window)
            speaker["verified"] = bool(quote and quote in window)
            quote_tokens = Counter(re.findall(r"\w+", quote))
            caption_tokens = Counter(re.findall(r"\w+", window))
            overlap = sum((quote_tokens & caption_tokens).values()) / max(1, sum(quote_tokens.values()))
            if speaker["verified"]:
                counts["verified"] += 1
            elif overlap >= 0.6:
                counts["partial"] += 1
            else:
                counts["dropped"] += 1
                # Keep the supported speaker attribution, but do not publish a bad quote.
                speaker["quote"] = ""
            speakers.append(speaker)
        item["speakers"] = speakers
    print(f"[quotes] {meeting['clip_id']}: " + " ".join(f"{k}={v}" for k, v in counts.items()), file=sys.stderr)
    return counts


def complete_document(system_prompt: str, document: str, json_schema: dict) -> dict:
    for attempt in range(3):
        raw = complete(system_prompt, document, json_schema=json_schema)
        if raw and raw.strip():
            return json.loads(raw)
        print(f"[extract] empty LLM content attempt={attempt + 1}/3", file=sys.stderr)
        if attempt < 2:
            time.sleep(2 ** attempt)
    raise ValueError("LLM returned empty content on all three attempts")


def extract_meeting(clip_id: int, force: bool = False) -> Optional[dict]:
    """Run extraction for a single meeting."""
    meeting_path = MEETINGS_DIR / f"{clip_id}.json"
    extract_path = EXTRACT_DIR / f"{clip_id}.json"

    if not meeting_path.exists():
        print(f"[extract] {clip_id}: no meeting file, skipping", file=sys.stderr)
        return None

    if extract_path.exists() and not force:
        print(f"[extract] {clip_id}: extract exists, skipping (use --force)", file=sys.stderr)
        return None

    meeting = json.loads(meeting_path.read_text())
    if not meeting["cues"]:
        print(f"[extract] {clip_id}: no captions, skipping", file=sys.stderr)
        return None
    check_roll_call(meeting)
    documents = build_documents(meeting)
    print(f"[extract] {clip_id}: ~{sum(len(doc) for doc in documents) // 4} tokens, {len(documents)} chunks", file=sys.stderr)
    roster_text = ", ".join(f"{s['name']} (District {s['district']})" for s in roster_for(meeting["date"]))
    system_prompt = PROMPT_PATH.read_text().replace("{{SUPERVISORS}}", roster_text).replace("{{MEETING_DATE}}", meeting["date"])

    json_schema = {
        "type": "object",
        "properties": {
            "clip_id": {"type": "integer"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "meta_id": {"type": "integer"},
                        "summary": {"type": "string"},
                        "outcome": {"type": "string"},
                        "topics": {"type": "array", "items": {"type": "string"}},
                        "speakers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "stance": {"type": "string"},
                                    "quote": {"type": "string"},
                                    "t0": {"type": "number"},
                                },
                                "required": ["name", "stance", "quote", "t0"],
                            },
                        },
                        "votes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "vote": {"type": "string"},
                                    "inferred": {"type": "boolean"},
                                },
                                "required": ["name", "vote", "inferred"],
                            },
                        },
                        "public_comment_themes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["meta_id", "summary", "outcome", "topics"],
                },
            },
        },
        "required": ["clip_id", "items"],
    }

    valid_items = []
    seen = set()
    for document in documents:
        data = complete_document(system_prompt, document, json_schema)
        expected = {int(value) for value in re.findall(r"^### ITEM meta_id=(\d+)", document, re.MULTILINE)}
        for item_data in data.get("items", []):
            try:
                validated = ExtractedItem(**item_data)
                if validated.meta_id not in expected or validated.meta_id in seen:
                    continue
                valid_items.append(validated.model_dump())
                seen.add(validated.meta_id)
            except Exception as e:
                print(f"[extract] {clip_id}: dropping item {item_data.get('meta_id', '?')}: {e}", file=sys.stderr)
        missing = expected - seen
        if missing:
            raise ValueError(f"LLM omitted {len(missing)} items: {sorted(missing)}; refusing incomplete extract")
    result = {"clip_id": clip_id, "items": valid_items}
    result["quote_verification"] = verify_quotes(meeting, result)

    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    extract_path.write_text(json.dumps(result, indent=2))
    print(f"[extract] {clip_id}: wrote {len(valid_items)} items to {extract_path}", file=sys.stderr)
    return result


def main():
    parser = argparse.ArgumentParser(description="Extract structured data from meeting transcripts")
    parser.add_argument("--clip", type=int, help="Process a single clip")
    parser.add_argument("--all", action="store_true", help="Process all meetings")
    parser.add_argument("--force", action="store_true", help="Overwrite existing extracts")
    args = parser.parse_args()

    if args.clip:
        extract_meeting(args.clip, force=args.force)
    elif args.all:
        for path in sorted(MEETINGS_DIR.glob("*.json")):
            clip_id = int(path.stem)
            extract_meeting(clip_id, force=args.force)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
