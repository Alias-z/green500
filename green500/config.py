"""Explicit environment configuration for the standalone application."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    """Keep database, files and model configuration independent of granny_data."""

    database_url: str = field(repr=False)
    data_dir: Path
    llm_base_url: str
    llm_api_key: str = field(repr=False)
    llm_model: str
    ops_token: str = field(repr=False)
    max_document_bytes: int = 0
    max_source_characters: int = 1_000_000
    max_chunk_characters: int = 18_000
    max_model_calls: int = 16
    view_password: str = field(default="", repr=False)
    run_worker: bool = True

    def __post_init__(self) -> None:
        """Validate operator-supplied processing allowances before work begins."""
        if self.max_document_bytes < 0:
            raise ValueError("GREEN500_MAX_DOCUMENT_BYTES must be nonnegative; zero allows any original file size.")
        if not 1000 <= self.max_source_characters <= 2_000_000:
            raise ValueError(
                "GREEN500_MAX_SOURCE_CHARACTERS must be between 1000 and 2000000."
            )
        if not 1 <= self.max_model_calls <= 128:
            raise ValueError("GREEN500_MAX_MODEL_CALLS must be between 1 and 128.")

    @property
    def task_timeout_seconds(self) -> int:
        """Allow the declared model attempts plus parsing and receipt persistence."""
        return self.max_model_calls * 100 + 120


def load_settings() -> Settings:
    """Load only the project's environment, never another application's secrets."""
    load_dotenv(PROJECT_DIR / ".env")
    database_url = os.getenv("GREEN500_DATABASE_URL", "")
    if not database_url:
        raise ValueError(
            "GREEN500_DATABASE_URL is required; run the database setup first."
        )
    base_url = os.getenv("GREEN500_LLM_BASE_URL", "https://api.openai.com/v1").rstrip(
        "/"
    )
    endpoint = urlsplit(base_url)
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError(
            "GREEN500_LLM_BASE_URL must be an HTTP(S) endpoint without credentials, query or fragment."
        )
    return Settings(
        database_url=database_url,
        data_dir=Path(
            os.getenv("GREEN500_DATA_DIR", str(PROJECT_DIR / "data"))
        ).resolve(),
        llm_base_url=base_url,
        llm_api_key=os.getenv("GREEN500_LLM_API_KEY", ""),
        llm_model=os.getenv("GREEN500_LLM_MODEL", "gpt-4.1-mini"),
        ops_token=os.getenv("GREEN500_OPS_TOKEN", ""),
        view_password=os.getenv("GREEN500_VIEW_PASSWORD", ""),
        run_worker=os.getenv("GREEN500_RUN_WORKER", "true").lower() in {"true","1","yes"},
        max_document_bytes=int(os.getenv("GREEN500_MAX_DOCUMENT_BYTES", "0")),
        max_source_characters=int(
            os.getenv("GREEN500_MAX_SOURCE_CHARACTERS", "1000000")
        ),
        max_model_calls=int(os.getenv("GREEN500_MAX_MODEL_CALLS", "16")),
    )
