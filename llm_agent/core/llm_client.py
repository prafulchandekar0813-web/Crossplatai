"""Async client for any OpenAI-compatible /chat/completions endpoint (OpenAI, OpenRouter, SGLang, vLLM, Ollama, ...).

Failures are raised as LLMError with a message that is safe to show to the user. There is deliberately no
"mock" fallback: if the model is unreachable the user is told so instead of receiving a fake answer.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from llm_agent.config import OpenAIConfig

log = logging.getLogger("llm_agent.llm")
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class LLMNotConfigured(LLMError):
    pass


@dataclass
class LLMResponse:
    message: Dict[str, Any]
    finish_reason: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)
    tools_dropped: bool = False


def resolve_endpoint(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/chat/completion"):
        if url.endswith(suffix):
            return url[: -len(suffix)] + "/chat/completions"
    return url + "/chat/completions"


def _error_text(resp: httpx.Response) -> str:
    detail = ""
    try:
        body = resp.json()
        err = body.get("error", body) if isinstance(body, dict) else body
        detail = err.get("message") or err.get("detail") or "" if isinstance(err, dict) else str(err)
        if not detail and isinstance(body, dict):
            detail = str(body.get("detail", ""))
    except ValueError:
        detail = resp.text[:300]
    hints = {401: " - check the API key", 403: " - the API key may lack access", 404: " - check the base URL and model name",
             429: " - rate limited"}
    return f"The model server returned HTTP {resp.status_code}: {str(detail)[:300] or resp.reason_phrase}{hints.get(resp.status_code, '')}"


class LLMClient:
    def __init__(self, get_cfg: Callable[[], OpenAIConfig], transport: Optional[httpx.AsyncBaseTransport] = None,
                 backoff: float = 1.0):
        self._get_cfg = get_cfg
        self._http = httpx.AsyncClient(transport=transport, follow_redirects=False)
        self.backoff = backoff

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- low level -------------------------------------------------------------------------------
    async def _post(self, cfg: OpenAIConfig, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = resolve_endpoint(cfg.base_url)
        headers = {"Content-Type": "application/json", "User-Agent": "llm-agent/2.0"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        attempts = max(0, cfg.max_retries) + 1
        for attempt in range(attempts):
            retry_after = 0.0
            try:
                resp = await self._http.post(url, json=payload, headers=headers, timeout=cfg.timeout_seconds)
            except httpx.TimeoutException:
                err = LLMError(f"The model did not answer within {cfg.timeout_seconds:g} seconds", retryable=True)
            except httpx.ConnectError:
                host = urlparse(url).netloc
                err = LLMError(f"Cannot connect to the model server at {host}. Is it running and is the URL right?", retryable=True)
            except httpx.HTTPError as e:
                err = LLMError(f"Network error talking to the model server ({type(e).__name__})", retryable=True)
            else:
                if resp.status_code < 400:
                    try:
                        return resp.json()
                    except ValueError:
                        raise LLMError("The model server did not return JSON. Is the base URL an OpenAI-compatible API (usually ending in /v1)?")
                err = LLMError(_error_text(resp), status=resp.status_code, retryable=resp.status_code in RETRYABLE_STATUS)
                try:
                    retry_after = min(20.0, float(resp.headers.get("retry-after", 0)))
                except ValueError:
                    pass
            if not err.retryable or attempt == attempts - 1:
                raise err
            delay = max(retry_after, self.backoff * (2 ** attempt))
            log.warning("LLM request failed (%s); retrying in %.1fs", err, delay)
            await asyncio.sleep(delay)
        raise LLMError("unreachable")  # pragma: no cover

    # ---- public ----------------------------------------------------------------------------------
    async def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None, *,
                   temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                   allow_tools_fallback: bool = True) -> LLMResponse:
        cfg = self._get_cfg()
        if not cfg.base_url or not cfg.model:
            raise LLMNotConfigured("No model is configured yet. An administrator needs to set the model server URL and "
                                   "model name under Admin → Model.")
        payload: Dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature if temperature is None else temperature,
            "max_tokens": cfg.max_tokens if max_tokens is None else max_tokens,
        }
        dropped = False
        if tools:
            payload["tools"] = tools
        try:
            data = await self._post(cfg, payload)
        except LLMError as e:
            if tools and allow_tools_fallback and cfg.retry_without_tools and e.status in (400, 422):
                payload.pop("tools")
                data = await self._post(cfg, payload)
                dropped = True
            else:
                raise
        try:
            choice = data["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError
        except (KeyError, IndexError, TypeError):
            raise LLMError("The model server sent a response in an unexpected format.")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return LLMResponse(message=message, finish_reason=choice.get("finish_reason"), usage=usage, tools_dropped=dropped)

    async def probe(self) -> Dict[str, Any]:
        """Used by the admin 'Test connection' button: one plain call, one tool-calling call."""
        t0 = time.monotonic()
        out: Dict[str, Any] = {"ok": False, "reply": "", "latency_ms": 0, "tool_calling": "unknown"}
        try:
            resp = await self.chat([{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=32,
                                   temperature=0, allow_tools_fallback=False)
            out["ok"] = True
            out["reply"] = str(resp.message.get("content") or "")[:200]
            out["latency_ms"] = int((time.monotonic() - t0) * 1000)
        except LLMError as e:
            out["error"] = str(e)
            return out
        ping = {"type": "function", "function": {"name": "ping", "description": "Call this tool to say hello.",
                "parameters": {"type": "object", "properties": {}}}}
        try:
            resp = await self.chat([{"role": "user", "content": "Call the ping tool now."}], tools=[ping], max_tokens=64,
                                   temperature=0, allow_tools_fallback=False)
            out["tool_calling"] = "supported" if resp.message.get("tool_calls") else "not_used"
        except LLMError as e:
            out["tool_calling"] = "rejected" if e.status in (400, 422) else "unknown"
        return out
