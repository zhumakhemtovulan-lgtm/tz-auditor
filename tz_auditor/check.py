"""Быстрая проверка, какие модели Gemini сейчас отвечают.

    python -m tz_auditor.check

Отправляет каждой модели короткий запрос и показывает результат и время ответа.
Помогает выбрать GEMINI_MODEL, когда часть моделей перегружена.
"""

from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv

CANDIDATES = [
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    load_dotenv(".env")
    key = os.getenv("GEMINI_API_KEY", "")
    if not key or key.startswith("your_"):
        print("GEMINI_API_KEY не задан в .env")
        return 3
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=60_000))
    models = sys.argv[1:] or CANDIDATES
    ok = []
    for m in models:
        t0 = time.time()
        try:
            r = client.models.generate_content(
                model=m, contents='Ответь JSON: {"ok": true}',
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
            )
            status = "ОТВЕЧАЕТ" if r.text else "пустой ответ"
            if r.text:
                ok.append(m)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "")
            status = {503: "перегружена (503)", 404: "недоступна (404)", 429: "лимит исчерпан (429)"}.get(
                code, f"ошибка: {str(exc)[:80]}")
        print(f"{m:28s} {status:24s} {time.time() - t0:5.1f} с", flush=True)
    if ok:
        print(f"\nРекомендуется: GEMINI_MODEL={ok[0]}")
        if len(ok) > 1:
            print(f"GEMINI_FALLBACK_MODELS={','.join(ok[1:])}")
    else:
        print("\nСейчас не отвечает ни одна модель. Повторите через несколько минут.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
