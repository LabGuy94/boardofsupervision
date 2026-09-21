"""Citation-ready graph queries for the question router.

Optional parameters default to None. ``settlements.run`` returns ordinary row
dictionaries, each with the same ``total``: the sum of dollar amounts in all
matching item and consent sub-item titles, not a sum restricted to approved
outcomes. An empty result is []; use ``sum_dollars(rows)`` to obtain 0.0 then.

Example observed from ``meeting_overview.run(g, clip_id=53160)`` on the
development graph (agenda loaded, extraction still a two-item synthetic stub):
{'clip_id': 53160, 'meta_id': 1262825, 'date': '2026-09-15',
 'item_title': '1 ROLL CALL AND PLEDGE OF ALLEGIANCE', 'start_sec': 33,
 'summary': None, 'outcome': None}
"""

from dataclasses import dataclass
import json
from pathlib import Path
import re


@dataclass
class Template:
    name: str
    description: str
    params: dict[str, str]
    cypher: str

    def run(self, g, **params) -> list[dict]:
        """Execute with optional parameters filled in; settlements adds total."""
        params = {
            **{name: None for name, kind in self.params.items() if "None" in kind},
            **params,
        }
        result = g.query(self.cypher, params)
        columns = [column[1] for column in result.header]
        rows = [dict(zip(columns, row)) for row in result.result_set]
        if self.name == "settlements":
            subitems_by_clip = {}
            expanded_rows = []
            for row in rows:
                if not row["has_subitems_note"]:
                    expanded_rows.append(row)
                    continue
                clip_id = row["clip_id"]
                if clip_id not in subitems_by_clip:
                    subitems_by_clip[clip_id] = consent_subitems(clip_id)
                subitems = [
                    item for item in subitems_by_clip[clip_id]
                    if item["meta_id"] == row["meta_id"]
                ]
                if not subitems:
                    expanded_rows.append(row)
                    continue
                # Replace the container, rather than counting it as a settlement.
                expanded_rows.extend(
                    {
                        **row,
                        "item_title": item["title"],
                        "file_no": item["file_no"],
                        "amount": item["amount"],
                        "from_consent": True,
                    }
                    for item in subitems
                )
            rows = expanded_rows
            total = sum_dollars(rows)
            for row in rows:
                row["total"] = total
        return rows


def sum_dollars(rows) -> float:
    """Sum every dollar amount in the citation-ready rows' item_title values."""
    return sum(
        (
            float(amount.replace(",", ""))
            for row in rows
            for amount in re.findall(r"\$([\d,\.]+)", row["item_title"])
        ),
        0.0,
    )


def consent_subitems(clip_id: int) -> list[dict]:
    """Read settlement details under consent items, retaining the parent citation."""
    path = Path(__file__).resolve().parents[1] / "data" / "meetings" / f"{int(clip_id)}.json"
    if not path.exists():
        return []
    meeting = json.loads(path.read_text())
    return [
        {
            "meta_id": parent["meta_id"],
            "file_no": item.get("file_no"),
            "title": item["title"],
            "amount": sum_dollars([{"item_title": item["title"]}]),
        }
        for parent in meeting["items"]
        if parent.get("section", "").upper() == "CONSENT AGENDA"
        for item in parent.get("sub_items", [])
        if "settlement" in item["title"].lower()
    ]


TEMPLATES: dict[str, Template] = {
    "items_by_topic": Template(
        name="items_by_topic",
        description="Items about a topic, optionally within inclusive ISO date bounds; newest first.",
        params={"topic": "str", "date_from": "str | None", "date_to": "str | None"},
        cypher="""
MATCH (m:Meeting)-[:HAS_ITEM]->(i:Item)-[:ABOUT]->(:Topic {name: $topic})
WHERE ($date_from IS NULL OR m.date >= $date_from)
  AND ($date_to IS NULL OR m.date <= $date_to)
RETURN m.clip_id AS clip_id, i.meta_id AS meta_id, m.date AS date,
       i.title AS item_title, i.start_sec AS start_sec,
       i.summary AS summary, i.outcome AS outcome
ORDER BY date DESC, start_sec
""".strip(),
    ),
    "supervisors_by_topic": Template(
        name="supervisors_by_topic",
        description="Rank supervisors by distinct items they spoke on about a topic, with up to three citations each.",
        params={"topic": "str"},
        cypher="""
MATCH (s:Supervisor)-[:SPOKE_ON]->(i:Item)-[:ABOUT]->(:Topic {name: $topic})
MATCH (m:Meeting)-[:HAS_ITEM]->(i)
WITH DISTINCT s, m, i
ORDER BY m.date DESC, i.start_sec
WITH s, count(DISTINCT i) AS n,
     collect(DISTINCT {clip_id: m.clip_id, meta_id: i.meta_id,
                       date: m.date, item_title: i.title})[..3] AS citations
RETURN s.name AS name, s.district AS district, n, citations,
       citations[0].clip_id AS clip_id, citations[0].meta_id AS meta_id,
       citations[0].date AS date, citations[0].item_title AS item_title
ORDER BY n DESC, name
""".strip(),
    ),
    "supervisor_on_topic": Template(
        name="supervisor_on_topic",
        description="A supervisor's verbatim quotes and timestamps on items about a topic.",
        params={"name": "str", "topic": "str"},
        cypher="""
MATCH (m:Meeting)-[:HAS_ITEM]->(i:Item)-[:ABOUT]->(:Topic {name: $topic})
MATCH (q:Quote)-[:FROM]->(i)
MATCH (q)-[:SAID_BY]->(s:Supervisor {name: $name})
RETURN m.clip_id AS clip_id, i.meta_id AS meta_id, m.date AS date,
       i.title AS item_title, s.name AS name, q.text AS text, q.t0 AS t0
ORDER BY date DESC, t0
""".strip(),
    ),
    "item_outcome": Template(
        name="item_outcome",
        description="Item summary, outcome, speakers and votes; filter by file number and/or case-insensitive title substring.",
        params={"file_no": "str | None", "title_contains": "str | None"},
        cypher="""
MATCH (m:Meeting)-[:HAS_ITEM]->(i:Item)
WHERE ($file_no IS NULL OR i.file_no = $file_no)
  AND ($title_contains IS NULL OR toLower(i.title) CONTAINS toLower($title_contains))
OPTIONAL MATCH (speaker)-[spoke:SPOKE_ON]->(i)
WITH m, i,
     collect(DISTINCT CASE WHEN speaker IS NULL THEN NULL
             ELSE {name: speaker.name, stance: spoke.stance} END) AS speakers
OPTIONAL MATCH (voter:Supervisor)-[v:VOTED]->(i)
RETURN m.clip_id AS clip_id, i.meta_id AS meta_id, m.date AS date,
       i.title AS item_title, i.file_no AS file_no, i.start_sec AS start_sec,
       i.summary AS summary, i.outcome AS outcome, speakers,
       collect(DISTINCT CASE WHEN voter IS NULL THEN NULL
               ELSE {name: voter.name, vote: v.vote, inferred: v.inferred} END) AS votes
ORDER BY date DESC, start_sec
""".strip(),
    ),
    "settlements": Template(
        name="settlements",
        description="Settlement items and consent-agenda settlement sub-items, optionally in one meeting, with itemized outcomes and a repeated total of all title dollar amounts (not approved-only).",
        params={"clip_id": "int | None"},
        cypher="""
MATCH (m:Meeting)-[:HAS_ITEM]->(i:Item)
OPTIONAL MATCH (i)-[:ABOUT]->(topic:Topic {name:'settlements'})
WITH m, i, topic
WHERE (toLower(i.title) CONTAINS 'settlement' OR topic IS NOT NULL)
  AND ($clip_id IS NULL OR m.clip_id = $clip_id)
RETURN DISTINCT m.clip_id AS clip_id, i.meta_id AS meta_id, m.date AS date,
       i.title AS item_title, i.file_no AS file_no, i.start_sec AS start_sec,
       i.summary AS summary, i.outcome AS outcome,
       NOT (toLower(i.title) CONTAINS 'settlement') AS has_subitems_note
ORDER BY date DESC, start_sec
""".strip(),
    ),
    "meeting_overview": Template(
        name="meeting_overview",
        description="All meeting items in agenda order, optionally filtered by clip ID and/or ISO date.",
        params={"clip_id": "int | None", "date": "str | None"},
        cypher="""
MATCH (m:Meeting)-[:HAS_ITEM]->(i:Item)
WHERE ($clip_id IS NULL OR m.clip_id = $clip_id)
  AND ($date IS NULL OR m.date = $date)
RETURN m.clip_id AS clip_id, i.meta_id AS meta_id, m.date AS date,
       i.title AS item_title, i.start_sec AS start_sec,
       i.summary AS summary, i.outcome AS outcome
ORDER BY start_sec
""".strip(),
    ),
    "bill_timeline": Template(
        name="bill_timeline",
        description="Chronological meeting history for a legislative file, including consent sub-items, outcomes, vote tallies and meeting body. Consent votes/outcomes belong to the cited parent item.",
        params={"file_no": "str"},
        cypher="""
MATCH (entry)-[:CONCERNS]->(:Legislation {file_no: $file_no})
OPTIONAL MATCH (entry)-[:PART_OF]->(parent:Item)
WITH entry, coalesce(parent, entry) AS i
MATCH (m:Meeting)-[:HAS_ITEM]->(i)
OPTIONAL MATCH (:Supervisor)-[v:VOTED]->(i)
RETURN m.clip_id AS clip_id, i.meta_id AS meta_id, m.date AS date,
       i.title AS item_title, entry.title AS legislation_item_title,
       entry.file_no AS file_no, m.body AS body, i.start_sec AS start_sec,
       i.outcome AS outcome,
       {aye: sum(CASE WHEN v.vote = 'aye' THEN 1 ELSE 0 END),
        no: sum(CASE WHEN v.vote = 'no' THEN 1 ELSE 0 END),
        absent: sum(CASE WHEN v.vote = 'absent' THEN 1 ELSE 0 END),
        excused: sum(CASE WHEN v.vote = 'excused' THEN 1 ELSE 0 END)} AS vote_tally
ORDER BY date, start_sec
""".strip(),
    ),
    "dissent_blocs": Template(
        name="dissent_blocs",
        description="Pairs of supervisors who voted no together, ranked by distinct shared items, with every supporting item citation.",
        params={},
        cypher="""
MATCH (a:Supervisor)-[:VOTED {vote: 'no'}]->(i:Item)<-[:VOTED {vote: 'no'}]-(b:Supervisor)
WHERE a.name < b.name
MATCH (m:Meeting)-[:HAS_ITEM]->(i)
WITH DISTINCT a, b, m, i
ORDER BY m.date, i.start_sec
WITH a, b, count(DISTINCT i) AS n,
     collect(DISTINCT {clip_id: m.clip_id, meta_id: i.meta_id,
                       date: m.date, item_title: i.title}) AS items
RETURN a.name AS supervisor_a, b.name AS supervisor_b, n, items,
       items[0].clip_id AS clip_id, items[0].meta_id AS meta_id,
       items[0].date AS date, items[0].item_title AS item_title
ORDER BY n DESC, supervisor_a, supervisor_b
""".strip(),
    ),
    "themes_by_topic": Template(
        name="themes_by_topic",
        description="Public-comment themes heard on items about a topic, with a citation for each item and theme.",
        params={"topic": "str"},
        cypher="""
MATCH (m:Meeting)-[:HAS_ITEM]->(i:Item)-[:ABOUT]->(:Topic {name: $topic})
MATCH (i)-[:HEARD_THEME]->(t:Theme)
RETURN DISTINCT m.clip_id AS clip_id, i.meta_id AS meta_id, m.date AS date,
       i.title AS item_title, t.name AS theme
ORDER BY date DESC, meta_id, theme
""".strip(),
    ),
}


def list_for_llm() -> str:
    """Render one name, description and parameter declaration per line."""
    return "\n".join(
        f"{template.name}: {template.description} Params: "
        + ", ".join(f"{name}: {kind}" for name, kind in template.params.items())
        for template in TEMPLATES.values()
    )


if __name__ == "__main__":
    from backend.db import get_graph

    graph = get_graph()
    samples = {
        "items_by_topic": {"topic": "environment"},
        "supervisors_by_topic": {"topic": "environment"},
        "supervisor_on_topic": {"name": "Connie Chan", "topic": "environment"},
        "item_outcome": {"title_contains": "tree"},
        "settlements": {"clip_id": 53160},
        "meeting_overview": {"clip_id": 53160},
        "bill_timeline": {"file_no": "251211"},
        "dissent_blocs": {},
        "themes_by_topic": {"topic": "housing"},
    }
    for template_name, sample_params in samples.items():
        result_rows = TEMPLATES[template_name].run(graph, **sample_params)
        print(f"{template_name}: rows={len(result_rows)} first={result_rows[0] if result_rows else None}")
