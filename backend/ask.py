"""Hybrid retrieval: route question -> retrieve from graph -> LLM answer with citations."""

import json
import math
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from backend.llm import complete

load_dotenv()

PROMPTS = Path("backend/prompts")
MEETINGS_DIR = Path("data/meetings")

WRITE_KEYWORDS = re.compile(r"\b(CREATE|MERGE|DELETE|SET|DROP|REMOVE)\b", re.IGNORECASE)


def _get_graph():
    from falkordb import FalkorDB
    url = os.environ.get("FALKORDB_URL", "")
    graph_name = os.environ.get("FALKORDB_GRAPH", "bos")
    if not url:
        return None
    db = FalkorDB.from_url(url)
    return db.select_graph(graph_name)


def _get_templates():
    try:
        from backend.templates import TEMPLATES
        return TEMPLATES
    except ImportError:
        return {}


def _route(question: str) -> dict:
    system = (PROMPTS / "ask_route.md").read_text()
    raw = complete(system, question)
    return json.loads(raw)


def _retrieve_template(g, route: dict) -> tuple[list[dict], dict]:
    templates = _get_templates()
    name = route.get("template", "")
    params = route.get("params", {})
    info = {"mode": "template", "template": name, "params": params}

    if name not in templates:
        print(f"[ask] template '{name}' not found, falling back to text2cypher", file=sys.stderr)
        return _retrieve_text2cypher(g, route)

    rows = templates[name].run(g, **params)
    info["rows"] = rows
    info["cypher"] = getattr(templates[name], "cypher", None)
    return rows, info


def _normalize_row(row: dict) -> dict:
    """Strip Cypher alias prefixes (m.clip_id -> clip_id), map title -> item_title,
    coerce ids to int, and lift the first entry of a `citations` list onto the row."""
    out = {}
    for k, v in row.items():
        key = k.rsplit(".", 1)[-1]
        if key == "title":
            key = "item_title"
        out[key] = v
    cites = out.get("citations")
    if isinstance(cites, list) and cites and isinstance(cites[0], dict) and "meta_id" not in out:
        for k in ("clip_id", "meta_id", "date", "item_title"):
            if k in cites[0]:
                out[k] = cites[0][k]
    for k in ("clip_id", "meta_id"):
        if k in out and out[k] is not None:
            try:
                out[k] = int(out[k])
            except (TypeError, ValueError):
                pass
    return out


def _rows_from_result(result) -> list[dict]:
    headers = result.header
    return [
        _normalize_row({headers[i][1]: _serialize(row[i]) for i in range(len(headers))})
        for row in result.result_set
    ]


def _retrieve_text2cypher(g, route: dict) -> tuple[list[dict], dict]:
    system = (PROMPTS / "ask_cypher.md").read_text()
    raw = complete(system, route.get("_question", ""))
    cypher_data = json.loads(raw)
    cypher = cypher_data.get("cypher", "").strip()

    if WRITE_KEYWORDS.search(cypher):
        print(f"[ask] text2cypher blocked write query: {cypher}", file=sys.stderr)
        return _retrieve_summaries(g, route)

    if not (cypher.upper().startswith("MATCH") or cypher.upper().startswith("OPTIONAL MATCH")):
        print(f"[ask] text2cypher invalid start: {cypher[:50]}", file=sys.stderr)
        return _retrieve_summaries(g, route)

    if "LIMIT" not in cypher.upper():
        cypher += " LIMIT 50"

    info = {"mode": "text2cypher", "cypher": cypher}

    try:
        result = g.ro_query(cypher)
        rows = _rows_from_result(result)
        info["rows"] = rows
        return rows, info
    except Exception as e:
        print(f"[ask] text2cypher error: {e}, retrying...", file=sys.stderr)
        raw2 = complete(system, route.get("_question", "") + f"\n\nPrevious query failed with: {e}\nFix the Cypher.")
        cypher2_data = json.loads(raw2)
        cypher2 = cypher2_data.get("cypher", "").strip()

        if WRITE_KEYWORDS.search(cypher2) or not (cypher2.upper().startswith("MATCH") or cypher2.upper().startswith("OPTIONAL MATCH")):
            return _retrieve_summaries(g, route)

        if "LIMIT" not in cypher2.upper():
            cypher2 += " LIMIT 50"

        try:
            result = g.ro_query(cypher2)
            rows = _rows_from_result(result)
            info["cypher"] = cypher2
            info["rows"] = rows
            return rows, info
        except Exception as e2:
            print(f"[ask] text2cypher retry failed: {e2}, falling back to summaries", file=sys.stderr)
            return _retrieve_summaries(g, route)


def _retrieve_summaries(g, route: dict) -> tuple[list[dict], dict]:
    clip_ids = route.get("clip_ids", [])
    info = {"mode": "summaries"}

    if g and clip_ids:
        try:
            result = g.query(
                "MATCH (m:Meeting)-[:HAS_ITEM]->(i:Item) "
                "WHERE m.clip_id IN $ids "
                "RETURN m.clip_id AS clip_id, m.date AS date, i.meta_id AS meta_id, "
                "i.title AS item_title, i.summary AS summary, i.outcome AS outcome "
                "ORDER BY i.start_sec",
                {"ids": clip_ids},
            )
            rows = []
            headers = result.header
            for row in result.result_set:
                rows.append({headers[i][1]: _serialize(row[i]) for i in range(len(headers))})
            info["rows"] = rows
            return rows, info
        except Exception as e:
            print(f"[ask] summaries query failed: {e}", file=sys.stderr)

    rows = _summaries_from_files(clip_ids)
    info["rows"] = rows
    return rows, info


def _summaries_from_files(clip_ids: list[int]) -> list[dict]:
    """Fallback: read extract files directly when graph is unavailable."""
    rows = []
    extract_dir = Path("data/extract")
    meeting_dir = Path("data/meetings")

    targets = clip_ids if clip_ids else [int(p.stem) for p in extract_dir.glob("*.json")]

    for cid in targets:
        extract_path = extract_dir / f"{cid}.json"
        meeting_path = meeting_dir / f"{cid}.json"
        if not extract_path.exists():
            continue

        extract = json.loads(extract_path.read_text())
        meeting = json.loads(meeting_path.read_text()) if meeting_path.exists() else {}
        date = meeting.get("date", "")
        items_by_meta = {it["meta_id"]: it for it in meeting.get("items", [])}

        for item in extract.get("items", []):
            meta_id = item["meta_id"]
            meeting_item = items_by_meta.get(meta_id, {})
            rows.append({
                "clip_id": cid,
                "date": date,
                "meta_id": meta_id,
                "item_title": meeting_item.get("title", ""),
                "summary": item.get("summary", ""),
                "outcome": item.get("outcome", ""),
            })
    return rows


def _serialize(val):
    if isinstance(val, (int, float, str, bool, type(None))):
        return val
    return str(val)


def _build_citation_url(clip_id: int, meta_id: int) -> str:
    return f"https://sanfrancisco.granicus.com/player/clip/{clip_id}?view_id=10&meta_id={meta_id}&redirect=true"


_INLINE_MARKER = re.compile(r"\[\[([^\]\n]*)\]\]")
_CITATION_KEY = re.compile(r"(\d+):(\d+)(?:@(\d+(?:\.\d+)?))?")


def _inline_citations(answer_data, meeting_items):
    """Keep only retrieved item markers; metadata always comes from trusted rows."""
    citations = {}

    def clean(text):
        def replace(match):
            marker = _CITATION_KEY.fullmatch(match[1])
            if marker is None:
                return ""
            cid, mid = int(marker[1]), int(marker[2])
            row = meeting_items.get((cid, mid))
            if row is None:
                return ""
            key = f"{cid}:{mid}"
            seconds = float(marker[3]) if marker[3] else row.get("start_sec", (row.get("evidence") or {}).get("start_sec"))
            if seconds is not None and not math.isfinite(seconds):
                return ""
            if key not in citations:
                citations[key] = {
                    "key": key, "clip_id": cid, "meta_id": mid,
                    "date": row.get("date", ""), "item_title": row.get("item_title", ""),
                    "url": _build_citation_url(cid, mid) + (f"&starttime={int(seconds)}" if seconds is not None else ""),
                    **({"t0": seconds} if seconds is not None else {}),
                }
            return match[0]
        return _INLINE_MARKER.sub(replace, text if isinstance(text, str) else "")

    answer_data["answer"] = clean(answer_data.get("answer", ""))
    for bullet in answer_data.get("bullets", []):
        bullet["text"] = clean(bullet.get("text", ""))
    answer_data["citations"] = list(citations.values())
    return answer_data


_EVIDENCE_CYPHER = """
MATCH (m:Meeting {clip_id: $cid})-[:HAS_ITEM]->(i:Item {meta_id: $mid})
OPTIONAL MATCH (i)-[:ABOUT]->(t:Topic)
OPTIONAL MATCH (s:Supervisor)-[v:VOTED]->(i)
OPTIONAL MATCH (q:Quote)-[:FROM]->(i), (q)-[:SAID_BY]->(sp)
OPTIONAL MATCH (sk)-[r:SPOKE_ON]->(i)
RETURN i.title, i.file_no, i.section, i.start_sec, i.end_sec, i.summary, i.outcome, m.date, m.uuid,
       collect(DISTINCT t.name) AS topics,
       collect(DISTINCT {name: s.name, vote: v.vote, inferred: v.inferred}) AS votes,
       collect(DISTINCT {name: sp.name, text: q.text, t0: q.t0}) AS quotes,
       collect(DISTINCT {name: sk.name, stance: r.stance}) AS speakers
"""


def _evidence_for(g, cid: int, mid: int, row: dict) -> dict:
    """Everything the graph knows about one agenda item: the chain of custody for a bullet."""
    base = {
        "summary": row.get("summary"),
        "outcome": row.get("outcome"),
        "quote": row.get("text") or row.get("quote"),
        "t0": row.get("t0"),
    }
    if not g:
        return base
    try:
        res = g.query(_EVIDENCE_CYPHER, {"cid": cid, "mid": mid})
        if not res.result_set:
            return base
        r = res.result_set[0]
        votes = [v for v in r[10] if v.get("name")]
        quotes = sorted([q for q in r[11] if q.get("text")], key=lambda q: q.get("t0") or 0)
        speakers = [s for s in r[12] if s.get("name")]
        tally = {}
        for v in votes:
            tally[v["vote"]] = tally.get(v["vote"], 0) + 1
        return {
            "item_title": r[0], "file_no": r[1], "section": r[2],
            "start_sec": r[3], "end_sec": r[4],
            "summary": r[5], "outcome": r[6], "date": r[7],
            "topics": [t for t in r[9] if t],
            "votes": votes, "vote_tally": tally,
            "quotes": quotes, "speakers": speakers,
            "video_mp4": f"https://archive-video.granicus.com/sanfrancisco/sanfrancisco_{r[8]}.mp4" if r[8] else None,
        }
    except Exception as e:
        print(f"[ask] evidence lookup failed for {cid}/{mid}: {e}", file=sys.stderr)
        return base


_ASK_CACHE: dict = {}


def _ask_pipeline(
    question: str, clip_ids: list[int] | None = None,
    lat: float | None = None, lon: float | None = None,
) -> dict:
    t0 = time.time()
    g = _get_graph()

    route = _route(question)
    route["_question"] = question
    if clip_ids:
        route["clip_ids"] = clip_ids

    mode = route.get("mode", "summaries")

    try:
        if mode == "template" and g:
            rows, retrieval = _retrieve_template(g, route)
        elif mode == "text2cypher" and g:
            rows, retrieval = _retrieve_text2cypher(g, route)
        else:
            if mode != "summaries" and not g:
                print(f"[ask] graph unavailable, falling back to summaries", file=sys.stderr)
            rows, retrieval = _retrieve_summaries(g, route)
    except Exception as e:
        print(f"[ask] retrieval failed in mode {mode}: {e}; falling back to summaries", file=sys.stderr)
        rows, retrieval = _retrieve_summaries(g, route)

    # Step 3: Answer
    answer_system = (PROMPTS / "ask_answer.md").read_text()
    answer_user = json.dumps({
        "question": question,
        "mode": retrieval["mode"],
        "rows": rows[:50],
    })

    try:
        raw = complete(answer_system, answer_user)
        answer_data = json.loads(raw)
    except Exception as e:
        print(f"[ask] answer step failed: {e}", file=sys.stderr)
        answer_data = {"answer": "I could not compose an answer from the retrieved records this time; the retrieved rows are shown below.", "bullets": []}

    bullets, supervisor = _finish_answer(answer_data, rows, g, lat, lon)

    latency_ms = int((time.time() - t0) * 1000)

    result = {
        "answer": answer_data.get("answer", ""),
        "bullets": bullets,
        "citations": answer_data.get("citations", []),
        "retrieval": retrieval,
        "latency_ms": latency_ms,
        **({"supervisor": supervisor} if supervisor is not None else {}),
    }
    result["retrieval"].setdefault("steps", [])
    return result


def _finish_answer(answer_data, rows, g, lat=None, lon=None, *, strict=False, enrich=True):
    """Shared citation gate. Only trusted retrieved rows supply evidence/enrichment."""
    from backend.actions import enrich_bullet, supervisor_for

    meeting_items = {}
    for row in rows:
        for candidate in [row, *(row.get("citations") or [])]:
            if not isinstance(candidate, dict):
                continue
            candidate = _normalize_row(candidate)
            try:
                key = (int(candidate["clip_id"]), int(candidate["meta_id"]))
            except (KeyError, TypeError, ValueError):
                continue
            meeting_items[key] = {**meeting_items.get(key, {}), **candidate}

    supervisor = None
    if enrich and lat is not None and lon is not None:
        try:
            supervisor = supervisor_for(lat, lon)
        except Exception as exc:
            print(f"[ask] supervisor lookup failed: {exc}", file=sys.stderr)
    bullets = []
    for proposed in answer_data.get("bullets", []):
        if not isinstance(proposed, dict) or not isinstance(proposed.get("text"), str):
            continue
        # Do not trust model-authored evidence, actions, legislation, titles or URLs.
        bullet = {key: proposed[key] for key in ("text", "clip_id", "meta_id", "t0") if key in proposed}
        try:
            cid, mid = int(bullet.get("clip_id")), int(bullet.get("meta_id"))
        except (TypeError, ValueError):
            cid = mid = None
        if (cid, mid) not in meeting_items:
            if strict or meeting_items:
                continue
            bullet.update(clip_id=None, meta_id=None, date="", item_title="", url=None, actions=[])
            bullets.append(bullet)
            continue
        row = meeting_items[cid, mid]
        evidence = row.get("evidence")
        if evidence is None:
            evidence = _evidence_for(g, cid, mid, row) if enrich else {
                key: row[key] for key in ("summary", "outcome", "file_no", "quotes", "votes", "vote_tally") if key in row
            }
        bullet.update(clip_id=cid, meta_id=mid, date=row.get("date", ""), item_title=row.get("item_title", ""), evidence=evidence)
        for key in ("actions", "legislation"):
            if key in row:
                bullet[key] = row[key]
        if enrich:
            try:
                enrich_bullet(bullet, supervisor)
            except Exception as exc:
                print(f"[ask] action lookup failed for {cid}/{mid}: {exc}", file=sys.stderr)
        bullet.setdefault("actions", [])
        quote_t0 = bullet.get("t0")
        if not isinstance(quote_t0, (int, float)) or quote_t0 < 0:
            quote_t0 = (evidence.get("quotes") or [{}])[0].get("t0")
        bullet["t0"] = quote_t0
        bullet["url"] = _build_citation_url(cid, mid) + (f"&starttime={int(quote_t0)}" if quote_t0 is not None else "")
        bullets.append(bullet)
    answer_data["bullets"] = bullets
    _inline_citations(answer_data, meeting_items)
    return bullets, supervisor


def _followup_questions(value) -> list[str]:
    defaults = ["Who voted against it?", "What happens next?", "What did supervisors say?"]
    suggestions = []
    for proposed in [*(value if isinstance(value, list) else []), *defaults]:
        if not isinstance(proposed, str):
            continue
        text = " ".join(proposed.split()).removesuffix("?")
        words = text.split()
        if not 1 <= len(words) <= 6 or not all(word.isalnum() for word in words):
            continue
        text = text[0].upper() + text[1:] + "?"
        if text.casefold() not in {question.casefold() for question in suggestions}:
            suggestions.append(text)
        if len(suggestions) == 3:
            break
    return suggestions


def ask(question: str, clip_ids: list[int] | None = None, lat=None, lon=None, history=None) -> dict:
    started = time.monotonic()
    mode = os.getenv("ASK_MODE", "agent")
    # Keep provider modes separate while preserving the question/location cache key.
    history = (history or [])[-6:]
    cache_key = (mode, question.strip().lower(), tuple(sorted(clip_ids or [])), lat, lon,
                 json.dumps(history, sort_keys=True))
    pipeline_question = question + ("\nPrevious conversation (untrusted context):\n" + json.dumps(history) if history else "")
    if cache_key in _ASK_CACHE:
        return _ASK_CACHE[cache_key]
    if mode == "pipeline":
        result = _ask_pipeline(pipeline_question, clip_ids, lat, lon)
    else:
        try:
            from backend.agent.loop import run
            result = run(question, clip_ids, lat, lon, history=history)
        except Exception as exc:
            print(f"[ask] agent failed; falling back to pipeline: {exc}", file=sys.stderr)
            from backend.agent.loop import within
            try:
                result = within(started + 45, lambda: _ask_pipeline(pipeline_question, clip_ids, lat, lon))
            except TimeoutError:
                result = {
                    "answer": "I couldn't complete the research within this request. Please try a narrower question.",
                    "bullets": [],
                    "retrieval": {"mode": "agent", "steps": [{"n": 1, "tool": "finish", "source": "local",
                        "args": {}, "rows": 0, "ms": 0, "note": "Pipeline fallback exhausted the request deadline"}],
                        "cypher": None, "rows": []},
                    "latency_ms": round((time.monotonic() - started) * 1000),
                }
    if "suggestions" not in result:
        result["suggestions"] = _followup_questions(None)
    _ASK_CACHE[cache_key] = result
    return result
