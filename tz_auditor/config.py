"""Настройки агента из переменных окружения и файла .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Settings:
    provider: str
    model: str
    api_key: str
    base_url: str
    temperature: float
    min_interval_sec: float
    max_retries: int
    cache_dir: Path
    use_cache: bool
    replay_dir: Path | None = None
    fallback_models: list[str] | None = None


class ConfigError(Exception):
    pass


def load_settings(env_file: str | None = None, provider: str | None = None,
                  model: str | None = None, use_cache: bool = True) -> Settings:
    load_dotenv(env_file or ".env", override=False)

    prov = (provider or os.getenv("LLM_PROVIDER", "")).strip().lower()
    if not prov:
        if os.getenv("GEMINI_API_KEY") and "your_" not in os.getenv("GEMINI_API_KEY", ""):
            prov = "gemini"
        elif os.getenv("OPENAI_API_KEY"):
            prov = "openai"
        elif os.getenv("OLLAMA_MODEL"):
            prov = "ollama"
        else:
            raise ConfigError(
                "Не найден ключ LLM. Скопируйте .env.example в .env и укажите GEMINI_API_KEY "
                "(бесплатно: https://aistudio.google.com/apikey)."
            )

    if prov == "gemini":
        key = os.getenv("GEMINI_API_KEY", "")
        if not key or key.startswith("your_"):
            raise ConfigError("GEMINI_API_KEY не задан в .env.")
        mdl = model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        base = ""
    elif prov == "openai":
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise ConfigError("OPENAI_API_KEY не задан в .env.")
        mdl = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        base = os.getenv("OPENAI_BASE_URL", "")
    elif prov == "ollama":
        key = "ollama"
        mdl = model or os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
        base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/") + "/v1"
    elif prov == "replay":
        key, base = "", ""
        mdl = model or "replay"
    else:
        raise ConfigError(f"Неизвестный LLM_PROVIDER: {prov}. Варианты: gemini, openai, ollama.")

    replay = os.getenv("REPLAY_DIR")
    fb_env = os.getenv("GEMINI_FALLBACK_MODELS" if prov == "gemini" else "LLM_FALLBACK_MODELS", "")
    if not fb_env and prov == "gemini":
        fb_env = ("gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-2.5-pro,"
                  "gemini-3.5-flash-lite,gemini-3.1-flash-lite")
    fallbacks = [m.strip() for m in fb_env.split(",") if m.strip() and m.strip() != mdl]
    return Settings(
        provider=prov,
        model=mdl,
        api_key=key,
        base_url=base,
        temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
        min_interval_sec=float(os.getenv("LLM_MIN_INTERVAL_SEC", "4" if prov == "gemini" else "0")),
        max_retries=int(os.getenv("LLM_MAX_RETRIES", "5")),
        cache_dir=Path(os.getenv("LLM_CACHE_DIR", ".cache/llm")),
        use_cache=use_cache,
        replay_dir=Path(replay) if replay else None,
        fallback_models=fallbacks,
    )
