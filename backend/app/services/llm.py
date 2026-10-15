"""Structured-output calls to the local Ollama model."""
from __future__ import annotations

import json

import httpx

from app.config import settings


class LlmError(Exception):
    pass


async def generate_json(prompt: str, schema: dict, timeout: float = 180) -> dict:
    """Return the model's JSON answer constrained by `schema`; raise LlmError on any failure."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{settings.OLLAMA_URL}/api/generate", json={
                "model": settings.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "think": False,          # thinking mode takes minutes for no gain on these tasks
                "format": schema,
                "options": {"temperature": 0},
            })
            resp.raise_for_status()
            out = json.loads(resp.json().get("response") or "")
    except (httpx.HTTPError, ValueError) as exc:
        raise LlmError(f"LLM call failed: {type(exc).__name__}: {exc}") from exc
    if not isinstance(out, dict):
        raise LlmError(f"LLM returned non-object JSON: {out!r}")
    return out
