"""Run cached-caption extraction six-wide; optionally load completed records."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from backend.extract import EXTRACT_DIR, MEETINGS_DIR, verify_quotes
from backend.roster import check_roll_call

LOG_DIR = Path("data/raw/backfill")


def extract_one(meeting: dict) -> dict:
    clip = meeting["clip_id"]
    path = EXTRACT_DIR / f"{clip}.json"
    log_path = LOG_DIR / f"{clip}.log"
    if path.exists():
        result = json.loads(path.read_text())
        result["quote_verification"] = verify_quotes(meeting, result)
        path.write_text(json.dumps(result, indent=2) + "\n")
        return {"clip_id": clip, "status": "existing", "cost": 0.0}
    completed = subprocess.run(
        [sys.executable, "-m", "backend.extract", "--clip", str(clip)],
        text=True, capture_output=True,
        env={**os.environ, "LLM_PROVIDER": "openrouter"},
    )
    text = completed.stdout + completed.stderr
    with log_path.open("a") as stream:
        stream.write(text)
    costs = [float(value) for value in re.findall(r"\[llm[^\n]*\bcost=([0-9.eE+-]+)", text)]
    status = "extracted" if completed.returncode == 0 and path.exists() else "failed"
    print(f"[backfill] {clip}: {status} cost=${sum(costs):.6f} log={log_path}", flush=True)
    return {"clip_id": clip, "status": status, "cost": sum(costs), "cost_calls": len(costs)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_date", required=True)
    parser.add_argument("--to", dest="to_date", default="9999-12-31")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--load", action="store_true")
    parser.add_argument("--verify-year", action="store_true", help="Require three roll-call samples before historical extraction")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    meetings = sorted(
        (json.loads(path.read_text()) for path in MEETINGS_DIR.glob("*.json") if path.stem.isdecimal()),
        key=lambda meeting: meeting["date"],
    )
    meetings = [meeting for meeting in meetings if args.from_date <= meeting["date"] <= args.to_date and meeting["cues"]]
    if not meetings:
        parser.error("No captioned meetings in this date range; ingest first")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if args.verify_year:
        for year in sorted({meeting["date"][:4] for meeting in meetings}, reverse=True):
            yearly = [meeting for meeting in meetings if meeting["date"].startswith(year)]
            preferred = sorted({0, len(yearly) // 2, len(yearly) - 1})
            candidates = preferred + [index for index in range(len(yearly)) if index not in preferred]
            reports = []
            for index in candidates:
                meeting = yearly[index]
                report = {"clip_id": meeting["clip_id"], "date": meeting["date"], **check_roll_call(meeting)}
                reports.append(report)
                if report["mismatch_rate"] > 0.1 or sum(row["verified"] for row in reports) >= 3:
                    break
            (LOG_DIR / f"roster-run-{year}.json").write_text(json.dumps(reports, indent=2) + "\n")
            if any(report["mismatch_rate"] > 0.1 for report in reports):
                raise SystemExit(f"{year}: >10% roster mismatch; no extraction started")
            if sum(report["verified"] for report in reports) < 3:
                raise SystemExit(f"{year}: fewer than three verified opening roll calls; no extraction started")
    graph = None
    if args.load:
        from backend.db import get_graph
        from backend.load import load_meeting
        graph = get_graph()
    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(extract_one, meeting): meeting for meeting in meetings}
        for future in as_completed(futures):
            meeting = futures[future]
            row = future.result()
            rows.append(row)
            if graph is not None and row["status"] != "failed":
                load_meeting(graph, meeting, json.loads((EXTRACT_DIR / f"{meeting['clip_id']}.json").read_text()))
                row["loaded"] = True
                print(f"[backfill] loaded {meeting['clip_id']}", flush=True)
    summary = {"from": args.from_date, "to": args.to_date, "meetings": len(meetings), "cost": sum(row["cost"] for row in rows), "results": rows}
    (LOG_DIR / f"run-{args.from_date}-{args.to_date}.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"[backfill] finished meetings={len(meetings)} succeeded={sum(row['status'] != 'failed' for row in rows)} cost=${summary['cost']:.6f}", flush=True)
    if graph is not None:
        from backend.load import print_counts
        print_counts(graph)
    if any(row["status"] == "failed" for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
