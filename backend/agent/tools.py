"""Read-only retrieval tools and the request-local citation/trace ledger."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import time

from backend.ask import _normalize_row, _evidence_for
from backend.templates import TEMPLATES

_ITEM_COLUMNS = "m.clip_id AS clip_id, i.meta_id AS meta_id, m.date AS date, i.title AS item_title, i.file_no AS file_no, i.outcome AS outcome, i.summary AS summary"
_FORBIDDEN = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|DROP|REMOVE|FOREACH|LOAD|UNION)\b", re.I)


def bounded_query(query: str, *, fulltext: bool = False) -> str:
    query = query.strip()
    # Mask literals, but reject comments/multiple statements rather than trying to parse them.
    code = re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", lambda m: " " * len(m.group()), query)
    if ';' in code or '//' in code or '/*' in code or _FORBIDDEN.search(code):
        raise ValueError("Only a single read-only MATCH query is allowed")
    if not re.match(r"^(?:OPTIONAL\s+)?MATCH\b", code, re.I):
        if not (fulltext and query.startswith("CALL db.idx.fulltext.queryNodes(")):
            raise ValueError("Queries must start with MATCH")
    if re.search(r"\bCALL\b", code, re.I) and not fulltext:
        raise ValueError("Procedures are not available through run_cypher")
    limits = list(re.finditer(r"\bLIMIT\s+(\d+)\b", code, re.I))
    if len(limits) != len(re.findall(r"\bLIMIT\b", code, re.I)):
        raise ValueError("LIMIT must be a literal integer at most 100")
    for match in reversed(limits):
        start, end = match.span(1)
        query = query[:start] + str(min(int(match.group(1)), 100)) + query[end:]
    if not re.search(r"\bLIMIT\s+\d+\s*$", code, re.I):
        query += " LIMIT 100"
    return query


class ReadOnlyGraph:
    """Adapter lets existing templates/evidence use query(), always dispatched as RO."""
    def __init__(self, graph, clip_ids=None):
        self.graph = graph
        self.clip_ids = clip_ids or []
        self.last_cypher = None

    def query(self, query, params=None, *, fulltext=False):
        if self.graph is None:
            raise RuntimeError("Meeting graph is unavailable")
        query = bounded_query(query, fulltext=fulltext)
        self.last_cypher = query
        return self.graph.ro_query(query, params or {}, timeout=3000)


def result_rows(result):
    return [_normalize_row(dict(zip([h[1] for h in result.header], row))) for row in result.result_set]

def item_rows(value):
    """Walk nested aggregate citation maps as well as ordinary item rows."""
    if isinstance(value, dict):
        if value.get("clip_id") is not None and value.get("meta_id") is not None:
            yield _normalize_row(value)
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from item_rows(child)
    elif isinstance(value, list):
        for child in value:
            yield from item_rows(child)



@dataclass
class Trace:
    steps: list[dict] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    valid_ids: set[tuple[int, int]] = field(default_factory=set)
    items: dict[tuple[int, int], dict] = field(default_factory=dict)

    def record(self, name, args, result, started, note=None):
        rows = result if isinstance(result, list) else [result]
        source = "sfgov" if name in {"legislation", "next_steps"} else "falkordb"
        step = {"n": len(self.steps) + 1, "tool": name, "source": source, "args": args, "rows": len(rows), "ms": round((time.monotonic() - started) * 1000)}
        if note:
            step["note"] = note
        self.steps.append(step)
        if not note:
            self.rows.extend(rows)
        if not note and source == "falkordb":
            for candidate in item_rows(rows):
                try:
                    key = (int(candidate["clip_id"]), int(candidate["meta_id"]))
                except (KeyError, TypeError, ValueError):
                    continue
                self.valid_ids.add(key)
                self.items[key] = {**self.items.get(key, {}), **candidate}


class Tools:
    def __init__(self, graph, clip_ids=None):
        self.g = ReadOnlyGraph(graph, clip_ids)
        self.clip_ids = clip_ids or []
        self.legislation_by_file = {}
        self.actions_by_file = {}
        self.last_retrieval_cypher = None

    def execute(self, name, args):
        if name not in {"search_items", "run_template", "run_cypher", "get_item", "meeting_overview", "legislation", "next_steps"}:
            raise ValueError(f"Unknown tool: {name}")
        rows = getattr(self, name)(**args)
        if isinstance(rows, list):
            return [_normalize_row(r) for r in rows if not self.clip_ids or r.get("clip_id") is None or int(r["clip_id"]) in self.clip_ids][:100]
        return rows

    def search_items(self, text: str, limit: int = 20):
        limit = max(1, min(int(limit), 100))
        params = {"q": text, "ids": self.clip_ids}
        scope = "WHERE (size($ids) = 0 OR m.clip_id IN $ids) "
        try:
            result = self.g.query(
                "CALL db.idx.fulltext.queryNodes('Item', $q) YIELD node AS i, score "
                "MATCH (m:Meeting)-[:HAS_ITEM]->(i) " + scope +
                f"RETURN {_ITEM_COLUMNS}, score ORDER BY score DESC LIMIT {limit}", params, fulltext=True)
            rows = result_rows(result)
        except Exception:
            rows = []
        if not rows:
            # Also covers Lucene terms whose punctuation/inflection yielded no match.
            terms = re.findall(r"[\w]+", text.lower())
            rows = result_rows(self.g.query(
                "MATCH (m:Meeting)-[:HAS_ITEM]->(i) " + scope +
                "AND any(term IN $terms WHERE toLower(i.title) CONTAINS term OR toLower(i.summary) CONTAINS term) "
                f"RETURN {_ITEM_COLUMNS} ORDER BY date DESC LIMIT {limit}", {**params, "terms": terms}))
        return [{**r, "summary": (r.get("summary") or "")[:300]} for r in rows]

    def run_template(self, name: str, params: dict | None = None):
        template = TEMPLATES[name]
        params = params or {}
        unknown = params.keys() - template.params.keys()
        if unknown:
            raise ValueError(f"Unsupported parameters: {sorted(unknown)}. Expected {template.params}")
        rows = template.run(self.g, **params)
        self.last_retrieval_cypher = self.g.last_cypher
        return rows

    def run_cypher(self, query: str):
        rows = result_rows(self.g.query(query))
        self.last_retrieval_cypher = self.g.last_cypher
        return rows

    def get_item(self, clip_id: int, meta_id: int):
        clip_id, meta_id = int(clip_id), int(meta_id)
        if self.clip_ids and clip_id not in self.clip_ids:
            raise ValueError("Item is outside the requested meetings")
        rows = result_rows(self.g.query(
            f"MATCH (m:Meeting {{clip_id: $cid}})-[:HAS_ITEM]->(i:Item {{meta_id: $mid}}) RETURN {_ITEM_COLUMNS}",
            {"cid": clip_id, "mid": meta_id}))
        if not rows:
            return {"error": "Item not found"}
        evidence = _evidence_for(self.g, clip_id, meta_id, rows[0])
        path = Path("data/meetings") / f"{clip_id}.json"
        sub_items = []
        if path.exists():
            meeting = json.loads(path.read_text())
            sub_items = next((i.get("sub_items", []) for i in meeting.get("items", []) if i["meta_id"] == meta_id), [])
        return {**rows[0], **evidence, "sub_items": sub_items, "evidence": {**evidence, "sub_items": sub_items}}

    def meeting_overview(self, clip_id: int):
        return self.run_template("meeting_overview", {"clip_id": int(clip_id)})

    def legislation(self, file_no: str):
        from backend.actions import find_legislation
        value = find_legislation(str(file_no))
        self.legislation_by_file[str(file_no)] = value
        return [value] if value else []

    def next_steps(self, file_no: str):
        from backend.actions import find_actions, find_legislation
        file_no = str(file_no)
        if file_no not in self.legislation_by_file:
            self.legislation_by_file[file_no] = find_legislation(file_no)
        value = find_actions(file_no, self.legislation_by_file[file_no])
        self.actions_by_file[file_no] = value
        return value


def tool_schema(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required}}}


STR = {"type": "string"}
INT = {"type": "integer"}
TOOL_SCHEMAS = [
    tool_schema("search_items", "Search agenda item titles and summaries. Short specific search terms work best.", {"text": STR, "limit": INT}, ["text"]),
    tool_schema("run_template", "Run one named retrieval template with its documented parameters.", {"name": {"type": "string", "enum": list(TEMPLATES)}, "params": {"type": "object"}}, ["name", "params"]),
    tool_schema("run_cypher", "Read-only FalkorDB MATCH query, LIMIT <=100. Include citation IDs in rows or citations.", {"query": STR}, ["query"]),
    tool_schema("get_item", "Get complete evidence: votes, tally, quotes and times, speakers, topics and sub-items.", {"clip_id": INT, "meta_id": INT}, ["clip_id", "meta_id"]),
    tool_schema("meeting_overview", "Get items in agenda order for a meeting.", {"clip_id": INT}, ["clip_id"]),
    tool_schema("legislation", "Look up the official legislative status and committee for a file number.", {"file_no": STR}, ["file_no"]),
    tool_schema("next_steps", "Find published upcoming agendas and public participation details for a legislative file.", {"file_no": STR}, ["file_no"]),
    tool_schema("finish", "Finish with an answer, retrieved-item citations, and three short follow-up questions. Append [[clip_id:meta_id]] immediately after factual claims in answer and bullet text, copying IDs from retrieved rows. For exact quotes use [[clip_id:meta_id@t0]] with the retrieved quote timestamp. Never invent IDs. Use a GFM table for 3+ items sharing amounts, votes, counts or dates, and a final Total row when amounts are summed.", {"answer": STR, "bullets": {"type": "array", "items": {"type": "object", "properties": {"text": STR, "clip_id": INT, "meta_id": INT, "t0": {"type": "number"}}, "required": ["text", "clip_id", "meta_id"]}}, "suggestions": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "string", "description": "Sentence case question, at most six words, no punctuation except trailing question mark.", "pattern": "^[A-Z][A-Za-z0-9]*(?: [A-Za-z0-9]+){0,5}\\?$"}}}, ["answer", "bullets", "suggestions"]),
]
