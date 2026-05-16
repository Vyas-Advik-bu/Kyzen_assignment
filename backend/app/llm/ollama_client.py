"""
Raw async Ollama client — handles streaming chat, tool calls, and model fallback.
Single inference semaphore prevents GPU/RAM contention under concurrent requests.
"""
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

# One concurrent inference at a time — Ollama queues internally but we want
# controlled backpressure rather than silent queuing that times out SSE streams.
_inference_semaphore = asyncio.Semaphore(1)

_CHAT_TIMEOUT = 120.0   # seconds per LLM call (total wall-clock)
_IDLE_TIMEOUT = 45.0    # seconds to wait for next token before declaring hang
_GENERATE_TIMEOUT = 60.0


class OllamaError(Exception):
    pass


class ToolCall:
    __slots__ = ("id", "name", "arguments")

    def __init__(self, id: str, name: str, arguments: dict[str, Any]) -> None:
        self.id = id
        self.name = name
        self.arguments = arguments


class ChatResponse:
    __slots__ = ("content", "tool_calls", "model", "done")

    def __init__(
        self,
        content: str,
        tool_calls: list[ToolCall],
        model: str,
        done: bool,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.model = model
        self.done = done


class OllamaClient:
    def __init__(
        self,
        base_url: str | None = None,
        primary_model: str | None = None,
        fallback_model: str | None = None,
    ) -> None:
        self._base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.primary_model = primary_model or settings.primary_model
        self.fallback_model = fallback_model or settings.fallback_model
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=None)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.1,
        stream: bool = False,
    ) -> ChatResponse:
        """Single-turn chat, non-streaming variant for structured synthesis calls."""
        model = model or self.primary_model
        async with _inference_semaphore:
            try:
                return await with_timeout(
                    lambda: self._chat_once(messages, tools, model, temperature, stream=False),
                    seconds=_CHAT_TIMEOUT,
                    label=f"chat/{model}",
                )
            except (OllamaError, TimeoutError) as exc:
                if model == self.primary_model and self.fallback_model:
                    log.warning("llm_fallback", primary=model,
                                fallback=self.fallback_model, reason=str(exc))
                    return await with_timeout(
                        lambda: self._chat_once(messages, tools, self.fallback_model,
                                                temperature, stream=False),
                        seconds=_CHAT_TIMEOUT,
                        label=f"chat/{self.fallback_model}",
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
        """
        Streaming chat — yields str tokens or ToolCall objects.
        Falls back to secondary model if primary errors on first chunk.
        """
        model = model or self.primary_model
        async with _inference_semaphore:
            try:
                async for item in self._stream_once(messages, tools, model, temperature):
                    yield item
            except (OllamaError, TimeoutError) as exc:
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
        """Quick single-prompt generate — used for per-page summarization."""
        model = model or self.primary_model
        async with _inference_semaphore:
            opts = self._model_options(model, 0.0)
            opts["num_predict"] = 700
            payload = {"model": model, "prompt": prompt, "stream": False, "options": opts}
            try:
                resp = await with_timeout(
                    lambda: self._post("/api/generate", payload),
                    seconds=_GENERATE_TIMEOUT,
                    label=f"generate/{model}",
                )
                return resp.get("response", "").strip()
            except (OllamaError, TimeoutError) as exc:
                if model == self.primary_model and self.fallback_model:
                    log.warning("llm_generate_fallback", reason=str(exc))
                    payload["model"] = self.fallback_model
                    resp = await with_timeout(
                        lambda: self._post("/api/generate", payload),
                        seconds=_GENERATE_TIMEOUT,
                        label=f"generate/{self.fallback_model}",
                    )
                    return resp.get("response", "").strip()
                raise

    # ── internals ──────────────────────────────────────────────────────────────

    @staticmethod
    def _model_options(model: str, temperature: float) -> dict[str, Any]:
        opts: dict[str, Any] = {"temperature": temperature}
        if "qwen3" in model.lower():
            opts["think"] = False
        return opts

    async def _chat_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        temperature: float,
        stream: bool,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": self._model_options(model, temperature),
        }
        if tools:
            payload["tools"] = tools

        data = await self._post("/api/chat", payload)
        msg = data.get("message", {})
        content = msg.get("content", "") or ""
        raw_calls = msg.get("tool_calls") or []
        tool_calls = [
            ToolCall(
                id=str(i),
                name=tc["function"]["name"],
                arguments=tc["function"].get("arguments", {}),
            )
            for i, tc in enumerate(raw_calls)
        ]
        return ChatResponse(content=content, tool_calls=tool_calls,
                            model=model, done=True)

    async def _stream_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        temperature: float,
    ) -> AsyncIterator[str | ToolCall]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": self._model_options(model, temperature),
        }
        if tools:
            payload["tools"] = tools

        t0 = time.monotonic()
        first_chunk = True
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise OllamaError(f"Ollama {resp.status_code}: {body.decode()[:200]}")
                # Per-line idle timeout — catches Ollama hangs that produce zero bytes
                line_iter = resp.aiter_lines()
                while True:
                    try:
                        raw = await asyncio.wait_for(
                            line_iter.__anext__(), timeout=_IDLE_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        raise TimeoutError(
                            f"stream/{model} idle >{_IDLE_TIMEOUT}s — Ollama may be hung"
                        )
                    except StopAsyncIteration:
                        break
                    if not raw:
                        continue
                    if time.monotonic() - t0 > _CHAT_TIMEOUT:
                        raise TimeoutError(f"stream/{model} exceeded {_CHAT_TIMEOUT}s total")
                    chunk = json.loads(raw)
                    msg = chunk.get("message", {})
                    first_chunk = False
                    # tool calls come in the final chunk for most Ollama models
                    raw_calls = msg.get("tool_calls") or []
                    for i, tc in enumerate(raw_calls):
                        yield ToolCall(
                            id=str(i),
                            name=tc["function"]["name"],
                            arguments=tc["function"].get("arguments", {}),
                        )
                    content = msg.get("content") or ""
                    if content:
                        yield content
        except (httpx.RequestError, json.JSONDecodeError) as exc:
            if first_chunk:
                raise OllamaError(str(exc)) from exc
            raise

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(path, json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise OllamaError(f"HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise OllamaError(str(exc)) from exc
