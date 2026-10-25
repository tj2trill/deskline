"""OpenAI-compatible chat client. Point LLM_BASE_URL at OpenAI, OpenRouter,
or a local server (Ollama: http://localhost:11434/v1, vLLM, LM Studio)."""
from __future__ import annotations

import json
import re

from ..config import get_settings
from . import http


def available() -> bool:
    s = get_settings()
    # Local servers usually need no key; hosted ones do.
    return bool(s.llm_api_key) or "localhost" in s.llm_base_url or "127.0.0.1" in s.llm_base_url or "ollama" in s.llm_base_url


def chat(messages: list[dict], *, tools: list[dict] | None = None, json_mode: bool = False,
         temperature: float = 0.4, max_tokens: int | None = None, model: str | None = None) -> dict:
    """Returns the assistant message dict ({"content": ..., "tool_calls": [...]})."""
    s = get_settings()
    if not available():
        raise http.NotConfigured("Set LLM_API_KEY (or point LLM_BASE_URL at a local OpenAI-compatible server).")
    body: dict = {"model": model or s.llm_model, "messages": messages, "temperature": temperature}
    if max_tokens:
        body["max_tokens"] = max_tokens
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {s.llm_api_key}"} if s.llm_api_key else {}
    with http.client(timeout=s.llm_timeout_seconds, headers=headers) as c:
        resp = c.post(s.llm_base_url.rstrip("/") + "/chat/completions", json=body)
        if resp.status_code == 400 and json_mode:
            # Some local servers reject response_format; retry once without it.
            body.pop("response_format", None)
            resp = c.post(s.llm_base_url.rstrip("/") + "/chat/completions", json=body)
        http.check("LLM", resp)
    return resp.json()["choices"][0]["message"]


def complete(system: str, user: str, **kw) -> str:
    msg = chat([{"role": "system", "content": system}, {"role": "user", "content": user}], **kw)
    return (msg.get("content") or "").strip()


def complete_json(system: str, user: str, **kw) -> dict:
    text = complete(system + "\nRespond with a single JSON object and nothing else.", user, json_mode=True, **kw)
    return parse_json(text)


def parse_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise
