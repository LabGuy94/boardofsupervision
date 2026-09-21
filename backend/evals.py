"""Read-only live API evaluation: uv run -m backend.evals [--only ID] [--json].

Checks inspect answer/bullet prose, not retrieval rows or attached evidence (which
could contain a fact the answer omitted). Citation checks accept one meta_id, a
list of alternatives, or {"clip_id": N} for any item in a particular meeting.
min_bullets accepts a count or {"count": N, "with_t0": true} for timed citations.
Each run appends a UUID nonce to request questions to bypass response caching.
Question clip_ids are sent to the API; citations_within checks their scope.
P90 uses the nearest-rank percentile of client-observed request latencies.
The injection case additionally compares graph node counts before/after the API
call; run it without concurrent ingestion to avoid attributing external writes.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import time
from uuid import uuid4

import httpx

from backend.db import get_graph


ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "data/evals/questions.json"
LAST_RUN = ROOT / "data/evals/last_run.json"
API_URL = "http://localhost:8000/api/ask"


def prose(response: dict) -> str:
    return "\n".join(
        [str(response.get("answer", ""))]
        + [str(bullet.get("text", "")) for bullet in response.get("bullets", [])]
    )


def check_result(kind: str, value, passed: bool, detail: str) -> dict:
    return {"type": kind, "value": value, "passed": bool(passed), "detail": detail}


def evaluate_check(check: dict, response: dict) -> dict:
    kind, value = check["type"], check["value"]
    text = prose(response)
    bullets = response.get("bullets", [])
    if kind == "contains":
        passed = str(value).casefold() in text.casefold()
        detail = f"Expected prose to contain {value!r}"
    elif kind == "answer_not_contains":
        passed = str(value).casefold() not in text.casefold()
        detail = f"Expected prose not to contain {value!r}"
    elif kind == "regex":
        passed = re.search(value, text, re.IGNORECASE | re.DOTALL) is not None
        detail = f"Expected prose to match {value!r}"
    elif kind == "cites_meta_id":
        if isinstance(value, dict):
            passed = any(
                str(b.get("clip_id")) == str(value["clip_id"])
                and b.get("meta_id") is not None for b in bullets
            )
        else:
            alternatives = value if isinstance(value, list) else [value]
            passed = any(str(b.get("meta_id")) in {str(v) for v in alternatives} for b in bullets)
        detail = f"Expected citation {value!r}; got " + str(
            [(b.get("clip_id"), b.get("meta_id")) for b in bullets]
        )
    elif kind == "min_bullets":
        count = value["count"] if isinstance(value, dict) else value
        timed = isinstance(value, dict) and value.get("with_t0", False)
        actual = sum(
            not timed or (
                isinstance(b.get("t0"), (int, float))
                and not isinstance(b["t0"], bool)
                and math.isfinite(b["t0"]) and b["t0"] >= 0
            ) for b in bullets
        )
        passed = actual >= count
        detail = f"Expected >= {count} {'timed ' if timed else ''}bullets; got {actual}"
    elif kind == "no_bullets":
        passed = not bullets
        detail = f"Expected no bullets; got {len(bullets)}"
    elif kind == "mode_in":
        mode = response.get("retrieval", {}).get("mode")
        passed = mode in value
        detail = f"Expected mode in {value!r}; got {mode!r}"
    elif kind == "citations_within":
        allowed = {int(clip) for clip in value}
        actual = {int(b["clip_id"]) for b in bullets if b.get("clip_id") is not None}
        passed = bool(bullets) and actual <= allowed
        detail = f"Expected citations only in {sorted(allowed)}; got {sorted(actual)}"
    elif kind == "has_action_type":
        actions = response.get("actions", []) + [
            action for b in bullets for action in b.get("actions", [])
        ]
        types = sorted({a.get("type", "") for a in actions})
        passed = value in types
        detail = f"Expected action {value!r}; got {types!r}"
    else:
        raise ValueError(f"Unknown check type: {kind}")
    return check_result(kind, value, passed, detail)


def citation_invariant(graph, response: dict) -> dict:
    """Check every supplied citation pair with exactly one read-only Cypher."""
    citations = []
    malformed = []
    for index, bullet in enumerate(response.get("bullets", [])):
        if bullet.get("clip_id") is None and bullet.get("meta_id") is None:
            continue
        values = (bullet.get("clip_id"), bullet.get("meta_id"))
        if not all(
            isinstance(v, int) and not isinstance(v, bool)
            or isinstance(v, str) and v.isdecimal() for v in values
        ):
            malformed.append(index)
            continue
        citations.append({"clip_id": int(values[0]), "meta_id": int(values[1])})
    rows = graph.ro_query(
        "UNWIND $citations AS citation "
        "OPTIONAL MATCH (m:Meeting)-[:HAS_ITEM]->(i:Item) "
        "WHERE m.clip_id = citation.clip_id AND i.meta_id = citation.meta_id "
        "RETURN citation.clip_id, citation.meta_id, count(i)",
        {"citations": citations},
    ).result_set
    found = {(int(clip), int(meta)) for clip, meta, count in rows if count > 0}
    missing = sorted({(c["clip_id"], c["meta_id"]) for c in citations} - found)
    return check_result(
        "citation_invariant", None, not malformed and not missing,
        f"Checked {len(citations)} citations; missing={missing}; malformed bullet indices={malformed}",
    )


def node_count(graph) -> int:
    return int(graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0])


def run_question(client: httpx.Client, graph, question: dict, nonce: str) -> dict:
    result = {
        "id": question["id"], "question": question["question"],
        "request_question": f"{question['question']} (eval {nonce})",
        "clip_ids": question.get("clip_ids"),
        "latency_s": None, "mode": None, "steps_count": None,
        "passed": False, "checks": [], "answer_snippet": "", "error": None,
    }
    injection = question["id"] == "injection_read_only"
    before = None
    if injection:
        try:
            before = node_count(graph)
        except Exception as exc:
            result["checks"].append(check_result("node_count_unchanged", None, False, f"Before: {exc}"))
    start = time.perf_counter()
    response = None
    try:
        payload = {"question": result["request_question"]}
        if "clip_ids" in question:
            payload["clip_ids"] = question["clip_ids"]
        http_response = client.post(API_URL, json=payload)
        http_response.raise_for_status()
        response = http_response.json()
        if not isinstance(response, dict):
            raise ValueError("API response must be a JSON object")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["latency_s"] = round(time.perf_counter() - start, 3)
        if injection and before is not None:
            try:
                after = node_count(graph)
                result["checks"].append(check_result(
                    "node_count_unchanged", before, before == after,
                    f"Node count before={before}, after={after}",
                ))
            except Exception as exc:
                result["checks"].append(check_result("node_count_unchanged", before, False, f"After: {exc}"))
    if response is not None and not result["error"]:
        result["response"] = response
        try:
            retrieval = response.get("retrieval") or {}
            result["mode"] = retrieval.get("mode")
            steps = retrieval.get("steps")
            result["steps_count"] = len(steps) if isinstance(steps, list) else None
            result["answer_snippet"] = prose(response)[:1200]
        except (TypeError, AttributeError) as exc:
            result["error"] = f"Invalid response shape: {exc}"
        for check in question["checks"]:
            try:
                result["checks"].append(evaluate_check(check, response))
            except Exception as exc:
                result["checks"].append(check_result(check["type"], check["value"], False, str(exc)))
        try:
            result["checks"].append(citation_invariant(graph, response))
        except Exception as exc:
            result["checks"].append(check_result("citation_invariant", None, False, str(exc)))
    else:
        for check in question["checks"]:
            result["checks"].append(check_result(check["type"], check["value"], False, "No usable API response"))
        result["checks"].append(check_result("citation_invariant", None, False, "No usable API response"))
    result["passed"] = not result["error"] and all(c["passed"] for c in result["checks"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", metavar="ID", help="Run one question by its stable ID")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of the table")
    args = parser.parse_args()
    questions = json.loads(QUESTIONS.read_text())
    if args.only:
        questions = [q for q in questions if q["id"] == args.only]
        if not questions:
            parser.error(f"Unknown question ID: {args.only}")
    graph = get_graph()
    run = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "api_url": API_URL, "timeout_s": 90, "results": [],
        "nonce": uuid4().hex,
        "node_count_before": node_count(graph),
    }
    if not args.json:
        print(f"{'Question':32} {'Result':6} {'Seconds':>7} {'Mode':13} {'Steps':>5}  Checks", flush=True)
        print("-" * 88, flush=True)
    with httpx.Client(timeout=90, trust_env=False) as client:
        for question in questions:
            result = run_question(client, graph, question, run["nonce"])
            run["results"].append(result)
            # Keep completed responses even if a later request is interrupted.
            LAST_RUN.parent.mkdir(parents=True, exist_ok=True)
            LAST_RUN.write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n")
            if not args.json:
                n_passed = sum(c["passed"] for c in result["checks"])
                print(
                    f"{result['id']:32} {'PASS' if result['passed'] else 'FAIL':6} "
                    f"{result['latency_s']:7.1f} {result['mode'] or '-':13} "
                    f"{result['steps_count'] if result['steps_count'] is not None else '-':>5}  "
                    f"{n_passed}/{len(result['checks'])}", flush=True,
                )
    run["finished_at"] = datetime.now(timezone.utc).isoformat()
    run["passed"] = sum(r["passed"] for r in run["results"])
    run["total"] = len(run["results"])
    run["median_latency_s"] = round(statistics.median(r["latency_s"] for r in run["results"]), 3)
    latencies = sorted(r["latency_s"] for r in run["results"])
    run["p90_latency_s"] = latencies[math.ceil(0.9 * len(latencies)) - 1]
    run["modes"] = {
        mode: sum((r["mode"] or "unknown") == mode for r in run["results"])
        for mode in sorted({r["mode"] or "unknown" for r in run["results"]})
    }
    run["node_count_after"] = node_count(graph)
    LAST_RUN.write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n")
    if args.json:
        print(json.dumps(run, indent=2, ensure_ascii=False))
    else:
        print(
            f"\n{run['passed']}/{run['total']} passed, "
            f"median {run['median_latency_s']:.1f} s, p90 {run['p90_latency_s']:.1f} s; "
            f"modes={run['modes']}"
        )
        for result in run["results"]:
            if result["passed"]:
                continue
            print(f"\nFAIL {result['id']}: {result['question']}")
            if result["error"]:
                print(f"  Error: {result['error']}")
            for check in result["checks"]:
                if not check["passed"]:
                    print(f"  {check['type']}: {check['detail']}")
            print(f"  Answer: {result['answer_snippet'] or '(no answer)'}")
        print(f"\nSaved {LAST_RUN.relative_to(ROOT)}")
    return 0 if run["passed"] == run["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
