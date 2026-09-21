"""Date-aware SF Board membership and conservative caption roll-call checks.

Service intervals are [start, end), clipped to the researched corpus window.
A service roster is not an attendance list. Caption silence never proves absence.
"""

from datetime import date as Date
import re
import sys
import unicodedata


COVERAGE_START = "2019-01-01"
COVERAGE_END = "2026-09-20"  # Exclusive; researched through September 19, 2026.

# Membership and district succession:
# https://en.wikipedia.org/wiki/List_of_members_of_the_San_Francisco_Board_of_Supervisors
# https://en.wikipedia.org/wiki/San_Francisco_Board_of_Supervisors
# Regular elected terms start January 8, not election day. Current district roster:
# https://sfbos.archive.sf.gov/
# https://sfbos.archive.sf.gov/supervisor-chan-district-1
# https://sfbos.archive.sf.gov/supervisor-melgar-district-7
# https://sfbos.archive.sf.gov/supervisor-sauter-district-3
# https://sfbos.archive.sf.gov/supervisor-mahmood-district-5
# https://sfbos.archive.sf.gov/supervisor-fielder-district-9
# https://sfbos.archive.sf.gov/supervisor-chen-district-11
# Jan 1 starts below are coverage clipping, NOT asserted inauguration dates.
# Each row: canonical name, district, inclusive start, exclusive end (None=ongoing).
TERMS = (
    ("Sandra Lee Fewer", 1, COVERAGE_START, "2021-01-08"),
    ("Connie Chan", 1, "2021-01-08", None),
    # Stefani's archived term explicitly ends Dec 2; Sherrill appointed Dec 18.
    # https://sfbos.archive.sf.gov/supervisor-stefani-district-2
    # https://www.sf.gov/news--mayor-breed-appoints-stephen-sherrill-serve-san-francisco-board-supervisors
    ("Catherine Stefani", 2, COVERAGE_START, "2024-12-02"),
    ("Stephen Sherrill", 2, "2024-12-18", None),
    ("Aaron Peskin", 3, COVERAGE_START, "2025-01-08"),
    ("Danny Sauter", 3, "2025-01-08", None),
    ("Katy Tang", 4, COVERAGE_START, "2019-01-08"),
    ("Gordon Mar", 4, "2019-01-08", "2023-01-08"),
    # Recall took effect Oct 18, NOT the Sep 16 election date.
    # https://sfbos.archive.sf.gov/supervisor-engardio-district-4
    # https://en.wikipedia.org/wiki/Beya_Alcaraz (sworn Nov 6; resigned Nov 13)
    # https://www.sf.gov/news-mayor-lurie-appoints-alan-wong-as-district-4-supervisor
    ("Joel Engardio", 4, "2023-01-08", "2025-10-18"),
    ("Beya Alcaraz", 4, "2025-11-06", "2025-11-13"),
    ("Alan Wong", 4, "2025-12-01", None),
    # Archive lists Dec 17 (first Board meeting), but actual swearing-in was
    # Dec 16 at 5 p.m., corroborated by Wikipedia and contemporary reporting:
    # https://sfbos.archive.sf.gov/supervisor-preston-district-5
    # https://en.wikipedia.org/wiki/Dean_Preston
    # https://sfist.com/2019/12/17/dean-prestion-sworn-in-as-supervisor-by-a-blue-haired-tom-ammiano/
    ("Vallie Brown", 5, COVERAGE_START, "2019-12-16"),
    ("Dean Preston", 5, "2019-12-16", "2025-01-08"),
    ("Bilal Mahmood", 5, "2025-01-08", None),
    ("Jane Kim", 6, COVERAGE_START, "2019-01-08"),
    # Haney resigned before Assembly swearing-in May 3; Dorsey appointed May 9.
    # https://en.wikipedia.org/wiki/Matt_Haney
    # https://sfbos.archive.sf.gov/supervisor-dorsey-district-6
    ("Matt Haney", 6, "2019-01-08", "2022-05-03"),
    ("Matt Dorsey", 6, "2022-05-09", None),
    ("Norman Yee", 7, COVERAGE_START, "2021-01-08"),
    ("Myrna Melgar", 7, "2021-01-08", None),
    ("Rafael Mandelman", 8, COVERAGE_START, None),
    ("Hillary Ronen", 9, COVERAGE_START, "2025-01-08"),
    ("Jackie Fielder", 9, "2025-01-08", None),
    # Cohen left for the Board of Equalization Jan 7, one day before Walton.
    # https://en.wikipedia.org/wiki/Malia_Cohen
    ("Malia Cohen", 10, COVERAGE_START, "2019-01-07"),
    ("Shamann Walton", 10, "2019-01-08", None),
    ("Ahsha Safai", 11, COVERAGE_START, "2025-01-08"),
    ("Chyanne Chen", 11, "2025-01-08", None),
)


def roster_for(date: str) -> list[dict]:
    """Return members in district order; omit genuinely vacant seats.

    Reject dates outside researched coverage rather than silently using today's
    Board. Names use the corpus's ASCII Safai spelling; matching accepts Safaí.
    """
    day = Date.fromisoformat(date).isoformat()
    if not COVERAGE_START <= day < COVERAGE_END:
        raise ValueError(f"Roster coverage is {COVERAGE_START} through 2026-09-19: {day}")
    return [
        {"name": name, "district": district}
        for name, district, start, end in TERMS
        if start <= day and (end is None or day < end)
    ]


def _normalize(text: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", " ", plain.upper()).strip()


# Observed CART spellings from README's 2026-09-15 roll call. Role-only
# "MR PRESIDENT" is deliberately not a name: the chair changes over time.
_ALIASES = {
    "Rafael Mandelman": ("MANDOLIN", "MANDELMAN"),
    "Danny Sauter": ("SADR", "SAUTER", "SOTER"),
    "Stephen Sherrill": ("CHERYL", "SHERRILL", "SHERYL"),
    "Alan Wong": ("HUANG WONG", "WONG", "HUANG"),
    "Bilal Mahmood": ("MAHMOOD", "MAHMUD"),
    "Jackie Fielder": ("FIELDER", "FIELDING"),
    "Myrna Melgar": ("MELGAR", "MALGAR"),
}
_NAME_ALIASES = {
    alias: name
    for name, _, _, _ in TERMS
    for alias in (_normalize(name), *_ALIASES.get(name, (name.rsplit(" ", 1)[-1].upper(),)))
}
_ALIAS_PATTERN = "|".join(re.escape(alias) for alias in sorted(_NAME_ALIASES, key=len, reverse=True))
_CALLED_NAME = re.compile(rf"\b(?:SUPERVISOR|PRESIDENT)\s+({_ALIAS_PATTERN})\b")
_ROLL_START = re.compile(r"\b(?:ROLL CALL|CALL (?:THE )?ROLL)\b")
_ROLL_END = re.compile(r"\b(?:YOU HAVE A QUORUM|WE HAVE A QUORUM|A QUORUM IS PRESENT|PLEDGE OF ALLEGIANCE)\b")


def _assess(text: str, expected: set[str]) -> dict:
    start = _ROLL_START.search(text)
    if start:
        text = text[start.end():]
    end = _ROLL_END.search(text)
    if end:
        text = text[:end.start()]
    roll_found = bool(start or (len(_CALLED_NAME.findall(text)) >= 3 and re.search(r"\bPRESENT\b", text)))
    matches = list(_CALLED_NAME.finditer(text)) if roll_found else []
    observed = {_NAME_ALIASES[m.group(1)] for m in matches}
    # CHAN/CHEN can be swapped. Both distinct surname calls establish coverage
    # of the pair, not which one answered; a lone surname cannot verify either.
    ambiguous = []
    pair = {"Connie Chan", "Chyanne Chen"} & expected
    if len(pair) == 2 and observed & pair and not pair <= observed:
        full_names = {name for name in pair if _normalize(name) in text}
        ambiguous = sorted((observed & pair) - full_names)
        observed.difference_update(ambiguous)
    # An unrecognized supervisor call prevents verification. Ignore title-only
    # references without an ensuing word, but do not invent a canonical name.
    unknown = []
    for call in re.finditer(r"\bSUPERVISOR\s+([A-Z]+)\b", text if roll_found else ""):
        if not _CALLED_NAME.match(text, call.start()):
            unknown.append(call.group(1))
    unexpected = observed - expected
    unobserved = expected - observed
    complete = roll_found and not unobserved and not unknown and not ambiguous
    # An unobserved member may be absent, excused, or beyond the cue window.
    # Only a complete replacement roster can support a 'missing' discrepancy.
    missing = unobserved if roll_found and len(observed) >= len(expected) and not unknown and not ambiguous else set()
    mismatch = len(unexpected | missing) / max(len(expected | observed), 1)
    return {
        "observed": sorted(observed),
        "missing": sorted(missing),
        "unexpected": sorted(unexpected),
        "unobserved": sorted(unobserved),
        "unknown": sorted(set(unknown)),
        "ambiguous": ambiguous,
        "mismatch_rate": mismatch,
        "verified": bool(complete and not unexpected),
        "status": "mismatch" if unexpected or missing else "verified" if complete else "inconclusive",
        "roll_call_found": roll_found,
    }


def check_roll_call(meeting: dict) -> dict:
    """Compare the opening roll call, NOT attendance, with the dated roster.

    Numeric mismatch_rate measures explicit contradictory names, not missing
    captions. Zero is NOT verification: callers must require verified=True.
    First inspect 40 cues. If inconclusive, extend only through the opening
    roll-call agenda item (at most 160 cues/10 minutes). Report both assessments.
    Named absences/excuses count as observed membership; no vote is inferred.
    """
    expected = {member["name"] for member in roster_for(meeting["date"])}
    cues = meeting.get("cues", [])
    first = _assess(_normalize(" ".join(cue.get("text", "") for cue in cues[:40])), expected)
    report = dict(first)
    inspected = min(40, len(cues))
    # Committee attendance cannot verify the full Board, even with familiar names.
    full_board = meeting.get("body", "Board of Supervisors").strip().lower() == "board of supervisors"
    if full_board and first["status"] == "inconclusive" and len(cues) > 40:
        opening = next((item for item in meeting.get("items", [])
                        if "ROLL CALL" in item.get("title", "").upper()), None)
        if opening and opening.get("end_sec") is not None:
            limit = min(float(opening["end_sec"]), 600)
            extended = [cue for cue in cues[:160] if float(cue.get("t0", 0)) < limit]
            if len(extended) > inspected:
                report = _assess(_normalize(" ".join(cue.get("text", "") for cue in extended)), expected)
                inspected = len(extended)
    if not full_board:
        report.update(verified=False, status="inconclusive", missing=[])
    report.update(first_40=first, cues_checked=inspected, expected=sorted(expected))
    print(
        f"[roster] {meeting.get('clip_id', '?')} {meeting['date']}: "
        f"{report['status']} verified={report['verified']} "
        f"observed={len(report['observed'])}/{len(expected)} "
        f"mismatch_rate={report['mismatch_rate']:.3f} cues={inspected} "
        f"unexpected={report['unexpected']} unobserved={report['unobserved']} "
        f"unknown={report['unknown']} ambiguous={report['ambiguous']}",
        file=sys.stderr,
    )
    return report
