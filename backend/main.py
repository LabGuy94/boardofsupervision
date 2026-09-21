"""FastAPI server: /api/ask, /api/meetings, /api/health"""

import json
import os
import sys
import threading
import time
from queue import Empty, Queue
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, Field

from backend.video import captions as video_captions, meta as video_meta, stream as video_stream

load_dotenv()

app = FastAPI(title="BoardOfSupervision API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MEETINGS_DIR = Path("data/meetings")


@app.api_route("/api/video/{clip_id}", methods=["GET", "HEAD"])
def api_video(clip_id: str, request: Request):
    return video_stream(clip_id, request.headers.get("range"), head_only=request.method == "HEAD")


@app.get("/api/video/{clip_id}/meta")
def api_video_meta(clip_id: str):
    return video_meta(clip_id)


@app.get("/api/captions/{clip_id}.vtt")
def api_captions(clip_id: int):
    return video_captions(clip_id)


class CitedItem(BaseModel):
    clip_id: int
    meta_id: int
    item_title: str = ""


class HistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    cited: list[CitedItem] | None = None


class AskRequest(BaseModel):
    question: str
    clip_ids: list[int] | None = None
    lat: float | None = None
    lon: float | None = None
    history: list[HistoryTurn] | None = Field(default=None, max_length=6)


@app.post("/api/ask")
def api_ask(req: AskRequest):
    from backend.ask import ask
    return ask(req.question, clip_ids=req.clip_ids, lat=req.lat, lon=req.lon,
               history=[turn.model_dump() for turn in req.history] if req.history else None)


@app.post("/api/ask/stream")
def api_ask_stream(req: AskRequest):
    """POST-compatible SSE; disconnects stop delivery, work stays deadline-bounded."""
    from backend.agent.loop import run

    events = Queue()
    connected = threading.Event()
    connected.set()

    def emit(kind, value):
        if connected.is_set():
            events.put((kind, value))

    def work():
        try:
            result = run(
                req.question, clip_ids=req.clip_ids, lat=req.lat, lon=req.lon,
                history=[turn.model_dump() for turn in req.history] if req.history else None,
                on_step=lambda step: emit("step", step),
                on_delta=lambda delta: emit("answer_delta", delta),
            )
            emit("result", result)
        except Exception:
            emit("error", {"message": "We couldn't read the meeting record just now. Please try again."})
        finally:
            emit("done", {})

    def stream():
        threading.Thread(target=work, daemon=True).start()
        try:
            while True:
                try:
                    kind, value = events.get(timeout=10)
                except Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {kind}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"
                if kind == "done":
                    break
        finally:
            connected.clear()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


_meetings_cache = (0.0, [])


@app.get("/api/meetings")
def api_meetings():
    global _meetings_cache
    if time.monotonic() - _meetings_cache[0] < 60:
        return _meetings_cache[1]
    from backend.ask import _get_graph
    try:
        graph = _get_graph()
        result = graph.ro_query("MATCH (m:Meeting) RETURN m.clip_id AS clip_id, m.date AS date ORDER BY date DESC", timeout=3000)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="The loaded meeting list is unavailable") from exc
    meetings = []
    for clip_id, date in result.result_set:
        path = MEETINGS_DIR / f"{int(clip_id)}.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        meetings.append({"clip_id": int(clip_id), "date": date,
                         "n_items": len(data.get("items", [])), "n_cues": len(data.get("cues", [])),
                         "loaded": True})
    _meetings_cache = (time.monotonic(), meetings)
    return meetings


@app.get("/api/health")
def api_health():
    provider = os.environ.get("LLM_PROVIDER", "openrouter")
    model = os.environ.get("LLM_MODEL", "")
    graph_status = "unknown"
    node_count = 0

    try:
        from falkordb import FalkorDB
        url = os.environ.get("FALKORDB_URL", "")
        if url:
            db = FalkorDB.from_url(url)
            g = db.select_graph(os.environ.get("FALKORDB_GRAPH", "bos"))
            result = g.query("MATCH (n) RETURN count(n)")
            node_count = result.result_set[0][0]
            graph_status = "connected"
        else:
            graph_status = "no_url"
    except Exception as e:
        graph_status = f"error: {e}"

    return {
        "llm_provider": provider,
        "llm_model": model,
        "graph_status": graph_status,
        "graph_node_count": node_count,
    }


class FrontendFiles(StaticFiles):
    async def get_response(self, path, scope):
        if path == "api" or path.startswith("api/"):
            raise StarletteHTTPException(status_code=404)
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404:
            return await super().get_response("index.html", scope)
        return response


FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    # Keep this mount after API routes so one same-origin tunnel serves both.
    app.mount("/", FrontendFiles(directory=FRONTEND_DIST, html=True), name="frontend")
