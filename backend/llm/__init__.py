import json
import os
import re
import sys
import time

from dotenv import load_dotenv

load_dotenv()

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openrouter")
LLM_MODEL = os.environ.get("LLM_MODEL", "google/gemini-3.8-flash")


def complete(system: str, user: str, *, json_schema: dict | None = None, audio_path: str | None = None) -> str:
    if LLM_PROVIDER == "gemini":
        from .gemini import _complete
    else:
        from .openrouter import _complete

    t0 = time.time()
    raw = _complete(system, user, json_schema=json_schema, audio_path=audio_path)
    latency = time.time() - t0
    if raw is None or not raw.strip():
        return ""

    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
    raw = re.sub(r"\n?```\s*$", "", raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[llm] JSON parse error, retrying (provider={LLM_PROVIDER} model={LLM_MODEL})", file=sys.stderr)
        raw = _complete(system, user + "\n\nReturn only valid JSON.", json_schema=json_schema, audio_path=audio_path)
        if raw is None or not raw.strip():
            return ""
        raw = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
        raw = re.sub(r"\n?```\s*$", "", raw)
        parsed = json.loads(raw)
        latency = time.time() - t0

    print(f"[llm] provider={LLM_PROVIDER} model={LLM_MODEL} latency={latency:.1f}s", file=sys.stderr)
    return raw
