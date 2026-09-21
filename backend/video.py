"""Bounded-memory MP4 streaming and browser-access metadata for meetings."""

import json
from contextlib import AbstractContextManager
from pathlib import Path
from threading import Lock

import anyio
import httpx
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, Response, StreamingResponse

MEETINGS_DIR = Path(__file__).resolve().parent.parent / "data" / "meetings"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
GRANICUS_REFERER = "https://sanfrancisco.granicus.com/"
CHUNK_SIZE = 256 * 1024
FORWARDED_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges")
_direct_probe_cache: dict[str, bool] = {}
_direct_probe_lock = Lock()


def captions(clip_id: int) -> FileResponse:
    path = MEETINGS_DIR.parent / "raw" / str(int(clip_id)) / "captions.vtt"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Meeting captions not found")
    return FileResponse(path, media_type="text/vtt")


def resolve_uuid(clip_id: int | str) -> str:
    clip = str(clip_id)
    if not clip.isascii() or not clip.isdecimal():
        raise HTTPException(status_code=404, detail="Meeting not found")
    try:
        meeting = json.loads((MEETINGS_DIR / f"{clip}.json").read_text())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Meeting not found") from None
    uuid = meeting.get("uuid")
    if not uuid:
        raise HTTPException(status_code=404, detail="Meeting video not found")
    return uuid


def _direct_url(uuid: str) -> str:
    return f"https://archive-video.granicus.com/sanfrancisco/sanfrancisco_{uuid}.mp4"


def meta(clip_id: int | str) -> dict:
    uuid = resolve_uuid(clip_id)
    url = _direct_url(uuid)
    # Serialize first probes so simultaneous metadata requests probe each URL once.
    with _direct_probe_lock:
        if uuid not in _direct_probe_cache:
            try:
                with httpx.stream(
                    "GET", url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Referer": "http://localhost:5173/",
                        "Range": "bytes=0-0",
                    },
                    follow_redirects=True,
                    timeout=30,
                ) as upstream:
                    direct_ok = (
                        upstream.status_code in (200, 206)
                        and upstream.headers.get("content-type", "").split(";", 1)[0] == "video/mp4"
                    )
            except httpx.HTTPError:
                direct_ok = False
            _direct_probe_cache[uuid] = direct_ok
        direct_ok = _direct_probe_cache[uuid]
    return {
        "clip_id": int(clip_id),
        "uuid": uuid,
        "direct_url": url,
        "proxied_url": f"/api/video/{clip_id}",
        "direct_ok": direct_ok,
    }


class _VideoResponse(StreamingResponse):
    """Keep the upstream open only for the lifetime of the ASGI response."""

    def __init__(self, upstream: httpx.Response, context: AbstractContextManager, headers: dict):
        super().__init__(
            upstream.iter_raw(chunk_size=CHUNK_SIZE),
            status_code=upstream.status_code,
            headers=headers,
        )
        self._context = context

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also runs if sending raises ClientDisconnect, or streaming is cancelled.
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self._context.__exit__, None, None, None)


def stream(
    clip_id: int | str, range_header: str | None, *, head_only: bool = False,
) -> StreamingResponse | Response:
    url = _direct_url(resolve_uuid(clip_id))
    context = httpx.stream(
        "GET", url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": GRANICUS_REFERER,
            "Range": range_header or "bytes=0-",
            "Accept-Encoding": "identity",
        },
        follow_redirects=True,
        timeout=30,
    )
    try:
        upstream = context.__enter__()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Video upstream unavailable") from exc
    headers = {name: upstream.headers[name] for name in FORWARDED_HEADERS if name in upstream.headers}
    if upstream.status_code == 416:
        context.__exit__(None, None, None)
        return Response(status_code=416, headers={
            name: value for name, value in headers.items() if name in ("content-range", "accept-ranges")
        })
    if upstream.status_code not in (200, 206):
        context.__exit__(None, None, None)
        raise HTTPException(status_code=502, detail=f"Video upstream returned {upstream.status_code}")
    if upstream.status_code == 206:
        headers.setdefault("accept-ranges", "bytes")
    if head_only:
        # Granicus HEAD ignores Range; GET headers give the correct range without reading the body.
        context.__exit__(None, None, None)
        return Response(status_code=upstream.status_code, headers=headers)
    return _VideoResponse(upstream, context, headers)
