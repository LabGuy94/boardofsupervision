import os
import sys

import httpx

from . import LLM_MODEL

_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")


def _complete(system: str, user: str, *, json_schema: dict | None = None, audio_path: str | None = None) -> str:
    body: dict = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_schema is not None:
        body["response_format"] = {"type": "json_object"}

    resp = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {_API_KEY}"},
        json=body,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()

    usage = data.get("usage", {})
    print(
        f"[llm:openrouter] tokens_in={usage.get('prompt_tokens', '?')} tokens_out={usage.get('completion_tokens', '?')} cost={usage.get('cost', '?')}",
        file=sys.stderr,
    )
    return data["choices"][0]["message"]["content"]
