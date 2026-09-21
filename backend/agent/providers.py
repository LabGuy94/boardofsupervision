"""Provider-native message histories for the hand-rolled tool loop."""

import json
import os
import sys
import time
from dataclasses import dataclass

import httpx

from ..llm import LLM_MODEL, LLM_PROVIDER


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Turn:
    calls: list[ToolCall]
    text: str
    raw: object


def _arguments(value: object) -> dict:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            return {"_argument_error": f"Invalid JSON tool arguments: {exc}"}
    if not isinstance(value, dict):
        return {"_argument_error": "Tool arguments must be a JSON object."}
    return value


class Session:
    def __init__(
        self, system: str, tools: list[dict], user: str,
        history: list[dict] | None = None,
    ):
        self.provider = "gemini" if LLM_PROVIDER == "gemini" else "openrouter"
        self.model = LLM_MODEL
        self.system = system
        self.tools = tools
        self._client = None
        self._messages: list = []

        if self.provider == "gemini":
            from google import genai
            from google.genai import types

            self.model = self.model.removeprefix("google/")
            if not self.model.startswith("gemini-"):
                self.model = "gemini-3.8-flash"
            self._native_tools = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=tool["function"]["name"],
                            description=tool["function"].get("description"),
                            parameters_json_schema=tool["function"]["parameters"],
                        )
                        for tool in tools
                    ]
                )
            ]
            self._client = genai.Client(
                api_key=os.environ.get("GEMINI_API_KEY", ""),
                http_options=types.HttpOptions(
                    retry_options=types.HttpRetryOptions(attempts=1)
                ),
            )
            self._messages.extend(
                types.Content(
                    role="model" if message["role"] == "assistant" else "user",
                    parts=[types.Part(text=message["content"])],
                )
                for message in history or []
            )
            self._messages.append(
                types.Content(role="user", parts=[types.Part(text=user)])
            )
        else:
            self._messages.extend([
                {"role": "system", "content": system},
                *(
                    {"role": message["role"], "content": message["content"]}
                    for message in history or []
                ),
                {"role": "user", "content": user},
            ])

    def complete(self, timeout: float, finish_only: bool = False) -> Turn:
        started = time.monotonic()
        try:
            if self.provider == "gemini":
                return self._complete_gemini(timeout, finish_only)
            return self._complete_openrouter(timeout, finish_only)
        finally:
            print(
                f"[llm] provider={self.provider} model={self.model} "
                f"latency={time.monotonic() - started:.1f}s",
                file=sys.stderr,
            )

    def _complete_openrouter(self, timeout: float, finish_only: bool) -> Turn:
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}"
            },
            json={
                "model": self.model,
                "messages": self._messages,
                "tools": self.tools,
                "tool_choice": (
                    {"type": "function", "function": {"name": "finish"}}
                    if finish_only else "auto"
                ),
                "max_tokens": 4000,
                **({"reasoning": {"effort": "low"}} if "flash" in self.model else {}),
                # Gemini thought signatures cannot cross upstream providers.
                **({
                    "provider": {
                        "only": ["google-ai-studio"],
                        "allow_fallbacks": False,
                    }
                } if self.model.startswith("google/gemini-") else {}),
            },
            timeout=timeout,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text
            for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY"):
                secret = os.environ.get(name)
                if secret:
                    detail = detail.replace(secret, "[REDACTED]")
            raise RuntimeError(
                f"OpenRouter HTTP {response.status_code}: {detail[:2000]}"
            ) from exc
        raw = response.json()["choices"][0]["message"]
        calls = [
            ToolCall(
                id=call["id"],
                name=call["function"]["name"],
                args=_arguments(call["function"].get("arguments")),
            )
            for call in raw.get("tool_calls") or []
        ]
        # Keep reasoning metadata and the original argument strings intact.
        self._messages.append(raw)
        return Turn(calls=calls, text=raw.get("content") or "", raw=raw)

    def _complete_gemini(self, timeout: float, finish_only: bool) -> Turn:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self.model,
            contents=self._messages,
            config=types.GenerateContentConfig(
                system_instruction=self.system,
                tools=self._native_tools,
                max_output_tokens=4000,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="ANY" if finish_only else "AUTO",
                        allowed_function_names=["finish"] if finish_only else None,
                    )
                ),
                http_options=types.HttpOptions(timeout=max(1, int(timeout * 1000))),
            ),
        )
        if not response.candidates or response.candidates[0].content is None:
            raise RuntimeError("Gemini returned no model content.")
        raw = response.candidates[0].content
        calls = []
        text = []
        for part in raw.parts or []:
            if part.function_call is not None:
                call = part.function_call
                calls.append(ToolCall(
                    id=call.id or "",
                    name=call.name,
                    args=_arguments(call.args),
                ))
            if part.text and not part.thought:
                text.append(part.text)
        # Reusing Content preserves every Part's opaque thought_signature.
        self._messages.append(raw)
        return Turn(calls=calls, text="".join(text), raw=raw)

    def add_results(self, turn: Turn, results: list[dict]) -> None:
        if len(turn.calls) != len(results):
            raise ValueError("Each tool call must have exactly one result.")
        if not turn.calls:
            return
        if self.provider == "gemini":
            from google.genai import types

            self._messages.append(types.Content(
                role="user",
                parts=[
                    types.Part(function_response=types.FunctionResponse(
                        id=call.id or None,
                        name=call.name,
                        response={"rows": result} if isinstance(result, list) else result,
                    ))
                    for call, result in zip(turn.calls, results)
                ],
            ))
        else:
            self._messages.extend(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
                for call, result in zip(turn.calls, results)
            )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
