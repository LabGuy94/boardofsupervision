import os
import sys

from . import LLM_MODEL

_API_KEY = os.environ.get("GEMINI_API_KEY", "")


def _complete(system: str, user: str, *, json_schema: dict | None = None, audio_path: str | None = None) -> str:
    from google import genai

    client = genai.Client(api_key=_API_KEY)

    config: dict = {"system_instruction": system}
    if json_schema is not None:
        config["response_mime_type"] = "application/json"
        config["response_schema"] = json_schema

    contents = []
    if audio_path:
        uploaded = client.files.upload(file=audio_path)
        contents.append(uploaded)
    contents.append(user)

    model = LLM_MODEL
    if not model.startswith("gemini-"):
        model = "gemini-3.8-flash"

    resp = client.models.generate_content(model=model, contents=contents, config=config)

    usage = resp.usage_metadata
    if usage:
        print(
            f"[llm:gemini] tokens_in={usage.prompt_token_count} tokens_out={usage.candidates_token_count}",
            file=sys.stderr,
        )
    return resp.text
