"""Обращение к языковой модели.

Один метод complete_json(): отправить инструкцию, получить JSON.
Здесь же повторы при ошибках сети и лимитах, кэш ответов и подсчёт токенов.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass

from .config import Settings


class LLMError(Exception):
    pass


@dataclass
class Usage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hits: int = 0


def extract_json(text: str):
    """Достаёт JSON из ответа модели, даже если он обёрнут в ```json ... ```."""
    if text is None:
        raise ValueError("пустой ответ")
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
    if not starts:
        raise ValueError("в ответе нет JSON")
    start = min(starts)
    end = max(s.rfind("}"), s.rfind("]"))
    return json.loads(s[start:end + 1])


class LLMClient:
    def __init__(self, settings: Settings, log=print):
        self.s = settings
        self.usage = Usage()
        self.log = log
        self._last_call = 0.0
        self._current_step = ""
        self.models_used: list[str] = [settings.model]
        self._fallbacks = list(settings.fallback_models or [])
        self._client = None
        if settings.provider == "gemini":
            from google import genai

            self._client = genai.Client(api_key=settings.api_key)
        elif settings.provider in {"openai", "ollama"}:
            from openai import OpenAI

            kwargs = {"api_key": settings.api_key}
            if settings.base_url:
                kwargs["base_url"] = settings.base_url
            self._client = OpenAI(**kwargs)

    # ---------------- кэш ----------------
    def _key(self, step: str, system: str, user: str) -> str:
        h = hashlib.sha256()
        for part in (self.s.provider, self.s.model, str(self.s.temperature), step, system, user):
            h.update(part.encode("utf-8"))
            h.update(b"\x1e")
        return h.hexdigest()[:32]

    def _cache_get(self, key: str):
        if not self.s.use_cache:
            return None
        p = self.s.cache_dir / f"{key}.json"
        if p.exists():
            try:
                return json.loads(p.read_text("utf-8"))
            except json.JSONDecodeError:
                return None
        return None

    def _cache_put(self, key: str, data) -> None:
        if not self.s.use_cache:
            return
        self.s.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.s.cache_dir / f"{key}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")

    # ---------------- вызов ----------------
    def _throttle(self) -> None:
        wait = self.s.min_interval_sec - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _raw_call(self, system: str, user: str) -> str:
        self._throttle()
        if self.s.provider == "gemini":
            from google.genai import types

            resp = self._client.models.generate_content(
                model=self.s.model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=self.s.temperature,
                    response_mime_type="application/json",
                    max_output_tokens=32768,
                ),
            )
            um = getattr(resp, "usage_metadata", None)
            if um is not None:
                self.usage.tokens_in += getattr(um, "prompt_token_count", 0) or 0
                self.usage.tokens_out += getattr(um, "candidates_token_count", 0) or 0
            text = resp.text
            if not text:
                reason = ""
                try:
                    reason = str(resp.candidates[0].finish_reason)
                except Exception:  # noqa: BLE001
                    pass
                raise LLMError(f"Модель вернула пустой ответ {reason}")
            return text

        if self.s.provider in {"openai", "ollama"}:
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            try:
                resp = self._client.chat.completions.create(
                    model=self.s.model, messages=messages, temperature=self.s.temperature,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:  # noqa: BLE001
                if "response_format" not in str(exc):
                    raise
                resp = self._client.chat.completions.create(
                    model=self.s.model, messages=messages, temperature=self.s.temperature,
                )
            if resp.usage:
                self.usage.tokens_in += resp.usage.prompt_tokens or 0
                self.usage.tokens_out += resp.usage.completion_tokens or 0
            return resp.choices[0].message.content or ""

        if self.s.provider == "replay":
            # Тестовый режим без сети: ответы берутся из файлов REPLAY_DIR/<шаг>.json.
            step = self._current_step
            if self.s.replay_dir is None:
                raise LLMError("replay: не задан REPLAY_DIR")
            p = self.s.replay_dir / f"{step}.json"
            if not p.exists():
                p = self.s.replay_dir / f"{step.rsplit('-', 1)[0]}.json"
            if not p.exists():
                raise LLMError(f"replay: нет файла {p}")
            return p.read_text("utf-8")
        raise LLMError(f"Неизвестный провайдер {self.s.provider}")

    def model_label(self) -> str:
        if len(self.models_used) == 1:
            return self.models_used[0]
        return " → ".join(self.models_used) + " (переключение на резервную модель)"

    def _switch_model(self, reason: str) -> bool:
        """Перейти на следующую резервную модель. Возвращает False, если резервных не осталось."""
        if not self._fallbacks:
            return False
        old = self.s.model
        self.s.model = self._fallbacks.pop(0)
        self.models_used.append(self.s.model)
        self.log(f"  ↪ модель {old} {reason}; переключаюсь на резервную {self.s.model}")
        return True

    @staticmethod
    def _is_retryable(exc: Exception) -> tuple[bool, float]:
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        msg = str(exc).lower()
        if code == 429 or "resource_exhausted" in msg or "rate" in msg and "limit" in msg:
            m = re.search(r"retry(?:delay)?['\"]?:?\s*['\"]?(\d+)", msg)
            return True, float(m.group(1)) + 2 if m else 30.0
        if code in (500, 502, 503, 504) or "unavailable" in msg or "overloaded" in msg:
            return True, 10.0
        if isinstance(exc, (TimeoutError, ConnectionError)) or "timeout" in msg or "connection" in msg:
            return True, 10.0
        if isinstance(exc, LLMError) and "пустой ответ" in str(exc):
            return True, 5.0
        return False, 0.0

    def complete_json(self, step: str, system: str, user: str, validate=None):
        """Вернуть JSON-ответ модели. validate(data) может выбросить ошибку: тогда просим модель исправиться."""
        key = self._key(step, system, user)
        cached = self._cache_get(key)
        if cached is not None:
            self.usage.cache_hits += 1
            self.log(f"  · {step}: ответ из кэша")
            return cached

        attempt = 0
        self._current_step = step
        prompt = user
        last_err: Exception | None = None
        while attempt <= self.s.max_retries:
            attempt += 1
            try:
                t0 = time.time()
                self.usage.calls += 1
                text = self._raw_call(system, prompt)
                data = extract_json(text)
                if validate is not None:
                    validate(data)
                self.log(f"  · {step}: готово за {time.time() - t0:.1f} с")
                self._cache_put(key, data)
                return data
            except (ValueError, json.JSONDecodeError) as exc:
                last_err = exc
                self.log(f"  · {step}: ответ не по формату ({exc}), прошу модель исправить")
                prompt = (
                    user + "\n\nВНИМАНИЕ: предыдущий ответ был некорректным: "
                    f"{str(exc)[:300]}. Верни только валидный JSON строго по схеме."
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                msg = str(exc).lower()
                if code == 404 or "not_found" in msg or "no longer available" in msg:
                    if self._switch_model("недоступна (404)"):
                        attempt = 0
                        continue
                    raise LLMError(f"{step}: модель {self.s.model} недоступна. Укажите другую в .env "
                                   f"(GEMINI_MODEL). Ответ сервиса: {exc}") from exc
                retry, wait = self._is_retryable(exc)
                if not retry:
                    raise LLMError(f"{step}: ошибка модели: {exc}") from exc
                overloaded = code in (500, 502, 503, 504) or "unavailable" in msg or "high demand" in msg
                if overloaded and attempt >= 2 and self._switch_model("перегружена (503)"):
                    attempt = 0
                    continue
                wait = min(wait * (1.5 ** (attempt - 1)), 120)
                self.log(f"  · {step}: временная ошибка ({str(exc)[:120]}), повтор через {wait:.0f} с")
                time.sleep(wait)
        raise LLMError(f"{step}: не удалось получить ответ после {self.s.max_retries} попыток: {last_err}")
