"""Load matching meeting/extraction JSON into FalkorDB without duplicating nodes."""

import argparse
import json
from pathlib import Path

from falkordb import Graph

from backend.db import get_graph
from backend.roster import roster_for
from backend.seed import TOPICS, seed
from redis.exceptions import ResponseError

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def setup_schema(graph: Graph) -> None:
    # Upgrade already-loaded items before switching to the clip-scoped MERGE key.
    graph.query(
        "MATCH (m:Meeting)-[:HAS_ITEM]->(i:Item) WHERE i.clip_id IS NULL SET i.clip_id = m.clip_id"
    )
    for label, prop in (
        ("Legislation", "file_no"), ("SubItem", "key"), ("Theme", "name"),
    ):
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except ResponseError as exc:
            message = str(exc).lower()
            if not ("index" in message and ("already exists" in message or "already indexed" in message)):
                raise


def load_meeting(graph: Graph, meeting: dict, extract: dict) -> None:
    if meeting["clip_id"] != extract["clip_id"]:
        raise ValueError("Meeting and extraction clip_id must match")
    roster = roster_for(meeting["date"])
    supervisor_names = {member["name"] for member in roster}
    graph.query(
        "MERGE (m:Meeting {clip_id: $clip_id}) "
        "SET m.date = $date, m.body = $body, m.uuid = $uuid, m.duration_sec = $duration_sec "
        "WITH m UNWIND $supervisors AS s "
        "MERGE (n:Supervisor {name: s.name}) SET n.district = s.district",
        {
            **{key: meeting[key] for key in ("clip_id", "date", "body", "uuid", "duration_sec")},
            "supervisors": roster,
        },
    )
    extracted_items = {item["meta_id"]: item for item in extract["items"]}
    items = []
    sub_items = []
    details = []
    topics = []
    themes = []
    speakers = []
    votes = []
    for item in meeting["items"]:
        meta_id = item["meta_id"]
        items.append({key: item.get(key) for key in (
            "meta_id", "title", "file_no", "section", "start_sec", "end_sec",
        )})
        sub_items.extend(
            {
                "meta_id": meta_id,
                "key": f"{meeting['clip_id']}:{meta_id}:{sub.get('file_no') or idx}",
                "title": sub["title"], "file_no": sub.get("file_no"),
            }
            for idx, sub in enumerate(item.get("sub_items", []))
        )
        detail = extracted_items.get(meta_id)
        if detail is None:
            continue
        details.append({
            "meta_id": meta_id, "summary": detail["summary"], "outcome": detail["outcome"],
        })
        topics.extend(
            {"meta_id": meta_id, "name": name}
            for name in detail["topics"] if name in TOPICS
        )
        themes.extend(
            {"meta_id": meta_id, "name": name.strip()}
            for name in detail.get("public_comment_themes", [])
            if isinstance(name, str) and name.strip()
        )
        for speaker in detail["speakers"]:
            speakers.append({
                "meta_id": meta_id, "name": speaker["name"], "stance": speaker["stance"],
                # Classify supervisors using the roster at the meeting date.
                "supervisor": speaker["name"] in supervisor_names,
                "text": speaker.get("quote"), "t0": speaker.get("t0"),
                "verified": speaker.get("verified"),
            })
        votes.extend({"meta_id": meta_id, **vote} for vote in detail["votes"])

    def batch(query: str, rows: list[dict]) -> None:
        if rows:
            graph.query(
                "UNWIND $rows AS r " + query,
                {"clip_id": meeting["clip_id"], "rows": rows},
            )

    batch(
        "MATCH (m:Meeting {clip_id: $clip_id}) "
        "MERGE (i:Item {clip_id: $clip_id, meta_id: r.meta_id}) "
        "SET i.title = r.title, i.file_no = r.file_no, i.section = r.section, "
        "i.start_sec = r.start_sec, i.end_sec = r.end_sec "
        "MERGE (m)-[:HAS_ITEM]->(i)",
        items,
    )
    batch(
        "MATCH (i:Item {clip_id: $clip_id, meta_id: r.meta_id}) "
        "MERGE (c:SubItem {key: r.key}) "
        "SET c.title = r.title, c.file_no = r.file_no "
        "MERGE (c)-[:PART_OF]->(i)",
        sub_items,
    )
    batch(
        "MATCH (i:Item {clip_id: $clip_id, meta_id: r.meta_id}) "
        "SET i.summary = r.summary, i.outcome = r.outcome",
        details,
    )
    batch(
        "MATCH (i:Item {clip_id: $clip_id, meta_id: r.meta_id}), (t:Topic {name: r.name}) "
        "MERGE (i)-[:ABOUT]->(t)",
        topics,
    )
    batch(
        "MATCH (i:Item {clip_id: $clip_id, meta_id: r.meta_id}) "
        "MERGE (t:Theme {name: r.name}) MERGE (i)-[:HEARD_THEME]->(t)",
        themes,
    )
    # Replace quotes only for extracted items, retaining quotes shared by other meetings.
    batch(
        "MATCH (q:Quote)-[edge:FROM]->(i:Item {clip_id: $clip_id, meta_id: r.meta_id}) "
        "DELETE edge WITH DISTINCT q WHERE NOT (q)-[:FROM]->() DETACH DELETE q",
        details,
    )
    batch(
        "MERGE (s:Speaker {name: r.name})",
        [speaker for speaker in speakers if not speaker["supervisor"]],
    )
    batch(
        "MATCH (i:Item {clip_id: $clip_id, meta_id: r.meta_id}), (s {name: r.name}) "
        "WHERE (r.supervisor AND s:Supervisor) OR (NOT r.supervisor AND s:Speaker) "
        "MERGE (s)-[edge:SPOKE_ON]->(i) SET edge.stance = r.stance "
        "FOREACH (quote IN CASE WHEN r.text IS NOT NULL AND r.text <> '' THEN [r] ELSE [] END | "
        "MERGE (q:Quote {text: quote.text, t0: quote.t0}) SET q.verified = quote.verified "
        "MERGE (q)-[:FROM]->(i) MERGE (q)-[:SAID_BY]->(s))",
        speakers,
    )
    batch(
        "MATCH (i:Item {clip_id: $clip_id, meta_id: r.meta_id}), (s:Supervisor {name: r.name}) "
        "MERGE (s)-[v:VOTED]->(i) SET v.vote = r.vote, v.inferred = r.inferred",
        votes,
    )
    graph.query(
        "MATCH (m:Meeting {clip_id: $clip_id})-[:HAS_ITEM]->(i:Item) "
        "OPTIONAL MATCH (c:SubItem)-[:PART_OF]->(i) "
        "WITH m, i, collect(c) + [i] AS entries "
        "UNWIND entries AS entry "
        "WITH m, entry WHERE entry.file_no IS NOT NULL AND entry.file_no <> '' "
        "MERGE (l:Legislation {file_no: entry.file_no}) "
        "SET l.title = CASE WHEN l.latest_date IS NULL OR m.date >= l.latest_date "
        "THEN entry.title ELSE l.title END, "
        "l.latest_date = CASE WHEN l.latest_date IS NULL OR m.date >= l.latest_date "
        "THEN m.date ELSE l.latest_date END "
        "MERGE (entry)-[:CONCERNS]->(l)",
        {"clip_id": meeting["clip_id"]},
    )


def print_counts(graph: Graph) -> None:
    print("Nodes:", graph.query(
        "MATCH (n) RETURN labels(n)[0] AS l, count(*) AS n ORDER BY l"
    ).result_set)
    print("Relationships:", graph.query(
        "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS n ORDER BY t"
    ).result_set)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clip", type=int, help="Load a single clip")
    parser.add_argument("--wipe", action="store_true", help="Delete all graph nodes and re-seed first")
    parser.add_argument("--meetings-file", type=Path, help="Use a specific meeting JSON (for offline fixtures)")
    args = parser.parse_args()
    if args.meetings_file:
        paths = [args.meetings_file]
    elif args.clip is not None:
        path = DATA_DIR / "meetings" / f"{args.clip}.json"
        if not path.exists():
            parser.error(f"Meeting file not found: {path}")
        paths = [path]
    else:
        paths = sorted(path for path in (DATA_DIR / "meetings").glob("*.json") if path.stem.isdecimal())
    graph = get_graph()
    if args.wipe:
        graph.query("MATCH (n) DETACH DELETE n")
        seed(graph)
    setup_schema(graph)
    for path in paths:
        meeting = json.loads(path.read_text())
        if args.clip is not None and meeting["clip_id"] != args.clip:
            continue
        extraction_path = DATA_DIR / "extract" / f"{meeting['clip_id']}.json"
        if not extraction_path.exists():
            print(f"Skipping {meeting['clip_id']}: no matching extraction")
            continue
        extract = json.loads(extraction_path.read_text())
        if extract.get("_stub"):
            print(f"WARNING: loading stub extraction for {meeting['clip_id']}; replace with real extraction before demo")
        load_meeting(graph, meeting, extract)
        print(f"Loaded {meeting['clip_id']}: items={len(meeting['items'])} extracted={len(extract['items'])}")
    print_counts(graph)


if __name__ == "__main__":
    main()
