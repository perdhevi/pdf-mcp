"""Configuration, loaded from environment variables and an optional .env file.

Every setting can be overridden with an environment variable prefixed ``PDF_MCP_``
(e.g. ``PDF_MCP_QDRANT_URL``). See ``.env.example`` for the full list.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["ollama", "openai"]

CUSTOMER_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PDF_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paths -----------------------------------------------------------------
    data_dir: Path = Field(Path("./data"), description="Root for registry DB and default inbox.")
    inbox_dir: Path | None = Field(None, description="Folder with one sub-folder per customer code.")

    # --- Vector store ------------------------------------------------------------
    qdrant_url: str | None = Field("http://localhost:6333", description="Qdrant server URL.")
    qdrant_api_key: str | None = None
    qdrant_path: Path | None = Field(
        None, description="Use embedded on-disk Qdrant instead of a server (single process only)."
    )
    collection_prefix: str = "pdfmcp_"

    # --- Model providers ---------------------------------------------------------
    ollama_base_url: str = "http://localhost:11434"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = None
    request_timeout: float = 300.0

    embed_provider: ProviderName = "ollama"
    embed_model: str = "bge-m3"
    embed_batch_size: int = 16

    llm_provider: ProviderName = "ollama"
    llm_model: str = "qwen3-vl:8b"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 1500

    vision_provider: ProviderName = "ollama"
    vision_model: str = "qwen3-vl:8b"

    # Ollama "think" flag for reasoning models (qwen3*). None = don't send it.
    ollama_think: bool | None = None

    # --- Extraction --------------------------------------------------------------
    ocr_enabled: bool = True
    ocr_engine: Literal["tesseract", "vision"] = "tesseract"
    ocr_lang: str = "eng"
    ocr_dpi: int = 300
    ocr_min_chars: int = Field(40, description="Pages with fewer text chars than this are OCR'd.")
    tesseract_cmd: str | None = Field(None, description=r"e.g. C:\Program Files\Tesseract-OCR\tesseract.exe")

    vision_enabled: bool = True
    vision_min_image_px: int = Field(150, description="Ignore images smaller than this (w or h).")
    vision_max_images_per_doc: int = 40
    vision_vector_min_drawings: int = Field(
        12, description="Text pages with at least this many vector drawing ops are rendered and sent to the "
        "vision model (vector charts). 0 disables vector-chart detection.")

    # --- Chunking / retrieval ----------------------------------------------------
    chunk_size: int = 1200
    chunk_overlap: int = 200
    search_limit: int = 8
    score_threshold: float | None = None

    allow_global_search: bool = Field(
        False, description="Expose find_reference_all_customers (exact lookup across every customer)."
    )

    # --- Server ------------------------------------------------------------------
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    public_base_url: str | None = Field(
        None, description="Base URL used in file links. Defaults to http://<host>:<port>."
    )
    file_server_in_stdio: bool = Field(
        True, description="In stdio mode, also serve /files on http_port so links work."
    )

    # --- Watcher -----------------------------------------------------------------
    watch_enabled: bool = True
    watch_polling: bool = Field(False, description="Use polling (needed for Docker bind mounts on Windows).")
    watch_debounce_seconds: float = 3.0
    sync_on_start: bool = True

    log_level: str = "INFO"

    # ---------------------------------------------------------------------------
    @property
    def inbox(self) -> Path:
        return (self.inbox_dir or self.data_dir / "inbox").resolve()

    @property
    def registry_path(self) -> Path:
        return (self.data_dir / "registry.sqlite3").resolve()

    @property
    def base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        host = "localhost" if self.http_host in ("0.0.0.0", "127.0.0.1", "::") else self.http_host
        return f"http://{host}:{self.http_port}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.inbox.mkdir(parents=True, exist_ok=True)


def validate_customer_code(code: str) -> str:
    code = (code or "").strip()
    if not CUSTOMER_CODE_RE.match(code):
        raise ValueError(
            f"Invalid customer code {code!r}: use 1-64 chars of letters, digits, '_' or '-', "
            "matching the folder name under the inbox."
        )
    # Canonical form is upper-case: Windows folders are case-insensitive, so
    # inbox/acme and inbox/ACME must map to the same isolated collection.
    return code.upper()


@lru_cache
def get_settings() -> Settings:
    return Settings()
