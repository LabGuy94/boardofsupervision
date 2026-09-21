"""Seed the frozen Contract 3 roster and topics: uv run -m backend.seed."""

from falkordb import Graph
from redis.exceptions import ResponseError

from backend.db import get_graph

# Frozen README roster; do not silently replace names when the live Board changes.
SUPERVISORS = {
    "Connie Chan": 1,
    "Stephen Sherrill": 2,
    "Danny Sauter": 3,
    "Alan Wong": 4,
    "Bilal Mahmood": 5,
    "Matt Dorsey": 6,
    "Myrna Melgar": 7,
    "Rafael Mandelman": 8,
    "Jackie Fielder": 9,
    "Shamann Walton": 10,
    "Chyanne Chen": 11,
}
TOPICS = (
    "housing", "homelessness", "public_safety", "transit", "budget",
    "settlements", "parks", "small_business", "environment", "health",
    "labor", "planning_land_use", "elections_governance", "other",
)


def seed(graph: Graph) -> None:
    for label, prop in (
        ("Meeting", "clip_id"), ("Item", "meta_id"),
        ("Supervisor", "name"), ("Topic", "name"),
    ):
        try:
            graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
        except ResponseError as exc:
            message = str(exc).lower()
            if not ("index" in message and ("already exists" in message or "already indexed" in message)):
                raise
    try:
        graph.query("CALL db.idx.fulltext.createNodeIndex('Item','title','summary')")
    except ResponseError as exc:
        message = str(exc).lower()
        if "already" not in message and "exists" not in message:
            # Older FalkorDB deployments may not expose full-text procedures.
            print(f"[seed] full-text index unavailable; search uses CONTAINS: {exc}")
    graph.query(
        "UNWIND $supervisors AS s MERGE (:Supervisor {name: s.name, district: s.district})",
        {"supervisors": [{"name": name, "district": district} for name, district in SUPERVISORS.items()]},
    )
    graph.query("UNWIND $topics AS name MERGE (:Topic {name: name})", {"topics": list(TOPICS)})


def main() -> None:
    graph = get_graph()
    seed(graph)
    print(graph.query("MATCH (n) RETURN labels(n)[0] AS l, count(*) AS n ORDER BY l").result_set)


if __name__ == "__main__":
    main()
