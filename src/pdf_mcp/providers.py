"""Model providers: embeddings, chat (RAG answers) and vision (image description).

Two back-ends are supported and can be mixed per role:
  * ``ollama``  – native Ollama REST API (/api/embed, /api/chat)
  * ``openai``  – any OpenAI-compatible API (OpenAI, Azure, OpenRouter, LM Studio, vLLM...)
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Protocol

import httpx

from .config import Settings

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


class ProviderError(RuntimeError):
    pass


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class ChatModel(Protocol):
    model: str

    def chat(
        self,
        system: str,
        user: str,
        images: list[bytes] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str: ...


# ---------------------------------------------------------------------------
class OllamaProvider:
    def __init__(self, settings: Settings, model: str):
        self.s = settings
        self.model = model
        self._client = httpx.Client(base_url=settings.ollama_base_url.rstrip("/"), timeout=settings.request_timeout)

    def _post(self, path: str, payload: dict) -> dict:
        try:
            r = self._client.post(path, json=payload)
        except httpx.HTTPError as e:
            raise ProviderError(f"Ollama unreachable at {self.s.ollama_base_url}: {e}") from e
        if r.status_code >= 400:
            raise ProviderError(f"Ollama {path} failed ({r.status_code}) for model {self.model!r}: {r.text[:500]}")
        return r.json()

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/api/embed", {"model": self.model, "input": texts})
        return data["embeddings"]

    def chat(self, system, user, images=None, temperature=None, max_tokens=None) -> str:
        msg: dict = {"role": "user", "content": user}
        if images:
            msg["images"] = [base64.b64encode(b).decode() for b in images]
        payload: dict = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, msg],
            "options": {
                "temperature": self.s.llm_temperature if temperature is None else temperature,
                "num_predict": max_tokens or self.s.llm_max_tokens,
            },
        }
        if self.s.ollama_think is not None:
            payload["think"] = self.s.ollama_think
        try:
            data = self._post("/api/chat", payload)
        except ProviderError as e:
            if "think" not in payload or "think" not in str(e).lower():
                raise
            payload.pop("think")  # model doesn't support the flag: retry without it
            data = self._post("/api/chat", payload)
        return strip_thinking(data.get("message", {}).get("content", ""))


# ---------------------------------------------------------------------------
class OpenAICompatProvider:
    def __init__(self, settings: Settings, model: str):
        self.s = settings
        self.model = model
        headers = {"Authorization": f"Bearer {settings.openai_api_key}"} if settings.openai_api_key else {}
        self._client = httpx.Client(
            base_url=settings.openai_base_url.rstrip("/"), timeout=settings.request_timeout, headers=headers
        )

    def _post(self, path: str, payload: dict) -> dict:
        try:
            r = self._client.post(path, json=payload)
        except httpx.HTTPError as e:
            raise ProviderError(f"OpenAI-compatible API unreachable at {self.s.openai_base_url}: {e}") from e
        if r.status_code >= 400:
            raise ProviderError(f"{path} failed ({r.status_code}) for model {self.model!r}: {r.text[:500]}")
        return r.json()

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/embeddings", {"model": self.model, "input": texts})
        items = sorted(data["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in items]

    def chat(self, system, user, images=None, temperature=None, max_tokens=None) -> str:
        if images:
            content: list | str = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b).decode()}}
                for b in images
            ]
        else:
            content = user
        data = self._post(
            "/chat/completions",
            {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                "temperature": self.s.llm_temperature if temperature is None else temperature,
                "max_tokens": max_tokens or self.s.llm_max_tokens,
            },
        )
        return strip_thinking(data["choices"][0]["message"].get("content") or "")


# ---------------------------------------------------------------------------
def _make(settings: Settings, provider: str, model: str):
    if provider == "ollama":
        return OllamaProvider(settings, model)
    if provider == "openai":
        return OpenAICompatProvider(settings, model)
    raise ValueError(f"Unknown provider {provider!r}")


class Models:
    """Bundle of the three model roles, built lazily from settings."""

    def __init__(self, settings: Settings, embedder: Embedder | None = None,
                 llm: ChatModel | None = None, vision: ChatModel | None = None):
        self.s = settings
        self._embedder = embedder
        self._llm = llm
        self._vision = vision

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = _make(self.s, self.s.embed_provider, self.s.embed_model)
        return self._embedder

    @property
    def llm(self) -> ChatModel:
        if self._llm is None:
            self._llm = _make(self.s, self.s.llm_provider, self.s.llm_model)
        return self._llm

    @property
    def vision(self) -> ChatModel:
        if self._vision is None:
            self._vision = _make(self.s, self.s.vision_provider, self.s.vision_model)
        return self._vision

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        bs = max(1, self.s.embed_batch_size)
        for i in range(0, len(texts), bs):
            out.extend(self.embedder.embed(texts[i : i + bs]))
        return out
