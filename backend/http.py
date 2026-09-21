"""Cached Granicus requests with the headers required by its media CDN."""

from pathlib import Path

import httpx


client = httpx.Client(
    headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
        "Referer": "https://sanfrancisco.granicus.com/",
    },
    timeout=30,
)


def get_cached(
    url: str, path: Path, refresh: bool = False, *, binary: bool = False
) -> str | bytes:
    if path.exists() and not refresh:
        return path.read_bytes() if binary else path.read_text(encoding="utf-8")
    response = client.get(url, follow_redirects=True)
    response.raise_for_status()
    if response.status_code != 200:
        raise httpx.HTTPStatusError(
            f"Expected 200, got {response.status_code}",
            request=response.request,
            response=response,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        path.write_bytes(response.content)
        return response.content
    path.write_text(response.text, encoding="utf-8")
    return response.text
