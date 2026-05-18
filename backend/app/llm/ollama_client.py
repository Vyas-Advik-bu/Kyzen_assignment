"""Gemini (Google Generative AI) client via REST + httpx."""
import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings
from app.observability.logging import get_logger
from app.resilience.timeout import with_timeout, TimeoutError

log = get_logger(__name__)

_inference_semaphore = asyncio.Semaphore(1)

_CHAT_TIMEOUT = 120.0
_IDLE_TIMEOUT = 45.0
_GENERATE_TIMEOUT = 60.0
_INTER_CALL_DELAY = 2.0  # minimum gap between API calls (free tier: 15 RPM)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiError(Exception):
    pass


class ToolCall:
    __slots__ = ("id", "name", "arguments")

    def __init__(self, id: str, name: str, arguments: dict[str, Any]) -> None:
        self.id = id
        self.name = name
        self.arguments = arguments


class ChatResponse:
    __slots__ = ("content", "tool_calls", "model", "done")

    def __init__(self, content: str, tool_calls: list[ToolCall],
                 model: str, done: bool) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.model = model
        self.done = done


# ── Message format conversion ─────────────────────────────────────────────────

def _to_gemini_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict]]:
    """Convert OpenAI-style message list → (system_instruction, gemini_contents)."""
    system = ""
    contents: list[dict] = []
    i = 0

    while i < len(messages):
        msg = messages[i]
        role = msg["role"]

        if role == "system":
            system = msg.get("content") or ""
            i += 1
            continue

        if role == "user":
            contents.append({"role": "user",
                              "parts": [{"text": msg.get("content") or ""}]})
            i += 1
            continue

        if role == "assistant":
            parts: list[dict] = []
            if msg.get("content"):
                parts.append({"text": msg["content"]})

            # Map tool_call_id → function_name for matching tool results below
            call_id_to_name: dict[str, str] = {}
            for tc in (msg.get("tool_calls") or []):
                fn = tc["function"]
                call_id_to_name[tc["id"]] = fn["name"]
                parts.append({"functionCall": {
                    "name": fn["name"],
                    "args": fn.get("arguments", {}),
                }})

            if not parts:
                parts.append({"text": ""})
            contents.append({"role": "model", "parts": parts})
            i += 1

            # Collect all consecutive tool-result messages into one user turn
            tool_parts: list[dict] = []
            while i < len(messages) and messages[i]["role"] == "tool":
                tmsg = messages[i]
                fn_name = call_id_to_name.get(tmsg.get("tool_call_id", ""), "unknown")
                try:
                    result = json.loads(tmsg["content"])
                except (json.JSONDecodeError, TypeError):
                    result = {"result": tmsg.get("content", "")}
                if not isinstance(result, dict):
                    result = {"result": result}
                tool_parts.append({"functionResponse": {
                    "name": fn_name, "response": result,
                }})
                i += 1

            if tool_parts:
                contents.append({"role": "user", "parts": tool_parts})
            continue

        i += 1

    return system, contents


def _to_gemini_tools(tools: list[dict[str, Any]]) -> list[dict]:
    """Convert OpenAI tool schema list → Gemini function_declarations."""
    declarations = []
    for tool in tools:
        fn = tool.get("function", {})
        decl: dict[str, Any] = {
            "name": fn["name"],
            "description": fn.get("description", ""),
        }
        if fn.get("parameters"):
            decl["parameters"] = fn["parameters"]
        declarations.append(decl)
    return [{"function_declarations": declarations}]


# ── Client ────────────────────────────────────────────────────────────────────

class GeminiClient:
    def __init__(
        self,
        api_key: str | None = None,
        primary_model: str | None = None,
        fallback_model: str | None = None,
    ) -> None:
        self._api_key = api_key or settings.gemini_api_key
        self.primary_model = primary_model or settings.primary_model
        self.fallback_model = fallback_model or settings.fallback_model
        self._client = httpx.AsyncClient(base_url=_GEMINI_BASE, timeout=None)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.1,
    ) -> ChatResponse:
        model = model or self.primary_model
        async with _inference_semaphore:
            try:
                return await with_timeout(
                    lambda: self._chat_once(messages, tools, model, temperature),
                    seconds=_CHAT_TIMEOUT,
                    label=f"gemini.chat/{model}",
                )
            except (GeminiError, TimeoutError) as exc:
                if model == self.primary_model and self.fallback_model:
                    log.warning("llm_fallback", primary=model,
                                fallback=self.fallback_model, reason=str(exc))
                    return await with_timeout(
                        lambda: self._chat_once(messages, tools, self.fallback_model, temperature),
                        seconds=_CHAT_TIMEOUT,
                        label=f"gemini.chat/{self.fallback_model}",
                    )
                raise

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.1,
    ) -> AsyncIterator[str | ToolCall]:
        model = model or self.primary_model
        async with _inference_semaphore:
            try:
                async for item in self._stream_once(messages, tools, model, temperature):
                    yield item
            except (GeminiError, TimeoutError) as exc:
                if model == self.primary_model and self.fallback_model:
                    log.warning("llm_stream_fallback", primary=model,
                                fallback=self.fallback_model, reason=str(exc))
                    async for item in self._stream_once(
                        messages, tools, self.fallback_model, temperature
                    ):
                        yield item
                else:
                    raise

    async def generate_short(self, prompt: str, *, model: str | None = None) -> str:
        model = model or self.primary_model
        async with _inference_semaphore:
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 700},
            }
            try:
                data = await with_timeout(
                    lambda: self._post(f"/models/{model}:generateContent", payload),
                    seconds=_GENERATE_TIMEOUT,
                    label=f"gemini.generate/{model}",
                )
                return _extract_text(data)
            except (GeminiError, TimeoutError) as exc:
                if model == self.primary_model and self.fallback_model:
                    log.warning("llm_generate_fallback", reason=str(exc))
                    payload_fb = {**payload}
                    data = await with_timeout(
                        lambda: self._post(f"/models/{self.fallback_model}:generateContent",
                                           payload_fb),
                        seconds=_GENERATE_TIMEOUT,
                        label=f"gemini.generate/{self.fallback_model}",
                    )
                    return _extract_text(data)
                raise

    # ── internals ─────────────────────────────────────────────────────────────

    async def _chat_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        temperature: float,
    ) -> ChatResponse:
        system, contents = _to_gemini_messages(messages)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        if system:
            payload["system_instruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = _to_gemini_tools(tools)

        data = await self._post(f"/models/{model}:generateContent", payload)

        candidates = data.get("candidates", [])
        if not candidates:
            return ChatResponse(content="", tool_calls=[], model=model, done=True)

        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for idx, part in enumerate(parts):
            if "text" in part and part["text"]:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append(ToolCall(
                    id=str(idx), name=fc["name"], arguments=fc.get("args", {})
                ))

        return ChatResponse(content="".join(text_parts), tool_calls=tool_calls,
                            model=model, done=True)

    async def _stream_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        temperature: float,
    ) -> AsyncIterator[str | ToolCall]:
        system, contents = _to_gemini_messages(messages)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        if system:
            payload["system_instruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = _to_gemini_tools(tools)

        await asyncio.sleep(_INTER_CALL_DELAY)
        url = f"/models/{model}:streamGenerateContent?alt=sse"
        headers = {"x-goog-api-key": self._api_key}
        t0 = time.monotonic()
        first_chunk = True

        try:
            async with self._client.stream("POST", url, json=payload,
                                            headers=headers) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise GeminiError(f"Gemini {resp.status_code}: {body.decode()[:200]}")

                line_iter = resp.aiter_lines()
                while True:
                    try:
                        raw = await asyncio.wait_for(
                            line_iter.__anext__(), timeout=_IDLE_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        raise TimeoutError(
                            f"gemini.stream/{model} idle >{_IDLE_TIMEOUT}s"
                        )
                    except StopAsyncIteration:
                        break

                    if not raw or not raw.startswith("data: "):
                        continue
                    payload_str = raw[6:]
                    if payload_str == "[DONE]":
                        break
                    if time.monotonic() - t0 > _CHAT_TIMEOUT:
                        raise TimeoutError(f"gemini.stream/{model} exceeded {_CHAT_TIMEOUT}s")

                    chunk = json.loads(payload_str)
                    first_chunk = False
                    candidates = chunk.get("candidates", [])
                    if not candidates:
                        continue

                    for part in candidates[0].get("content", {}).get("parts", []):
                        if "text" in part and part["text"]:
                            yield part["text"]
                        elif "functionCall" in part:
                            fc = part["functionCall"]
                            yield ToolCall(
                                id=f"call_{fc['name']}",
                                name=fc["name"],
                                arguments=fc.get("args", {}),
                            )
        except (httpx.RequestError, json.JSONDecodeError) as exc:
            if first_chunk:
                raise GeminiError(str(exc)) from exc
            raise

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"x-goog-api-key": self._api_key}
        await asyncio.sleep(_INTER_CALL_DELAY)
        # Retry 429 rate-limit responses with backoff before failing.
        for attempt in range(3):
            try:
                resp = await self._client.post(path, json=payload, headers=headers)
                if resp.status_code == 429:
                    wait = 12 * (attempt + 1)  # 12s, 24s, 36s — enough to clear the RPM window
                    log.warning("gemini_rate_limited", attempt=attempt, wait=wait)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                raise GeminiError(f"HTTP {exc.response.status_code}: "
                                   f"{exc.response.text[:200]}") from exc
            except httpx.RequestError as exc:
                raise GeminiError(str(exc)) from exc
        raise GeminiError("Rate limited (429) after 3 retries")


def _extract_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if "text" in p).strip()


# Alias so existing imports continue to work
OllamaClient = GeminiClient


# ═══════════════════════════════════════════════════════════════════════════════
# Ollama (local LLM) implementation
# ═══════════════════════════════════════════════════════════════════════════════
#
# import asyncio, json, time
# from collections.abc import AsyncIterator
# from typing import Any
# import httpx
# from app.config import settings
# from app.resilience.timeout import with_timeout, TimeoutError
#
# _inference_semaphore = asyncio.Semaphore(1)
# _CHAT_TIMEOUT = 120.0
# _IDLE_TIMEOUT = 45.0
# _GENERATE_TIMEOUT = 60.0
#
# class OllamaError(Exception): pass
#
# class OllamaClient:
#     def __init__(self, base_url=None, primary_model=None, fallback_model=None):
#         self._base_url = (base_url or settings.ollama_base_url).rstrip("/")
#         self.primary_model = primary_model or settings.primary_model
#         self.fallback_model = fallback_model or settings.fallback_model
#         self._client = httpx.AsyncClient(base_url=self._base_url, timeout=None)
#
#     @staticmethod
#     def _model_options(model, temperature):
#         opts = {"temperature": temperature}
#         if "qwen3" in model.lower():
#             opts["think"] = False   # disable thinking tokens
#         return opts
#
#     # ... (chat / stream_chat / generate_short / _chat_once / _stream_once / _post)
