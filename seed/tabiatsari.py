"""Клиент к API tabiatsari.uz — источнику треков.

Доступ открытый, ключей не нужно, но **обязателен `User-Agent`**: без него
сервер отвечает 403 (`urllib` по умолчанию его не шлёт и получает отказ).

Всё скачанное складывается на диск и при повторном запуске читается оттуда.
Импорт запускают многократно — сначала вхолостую, потом набело, потом ещё раз
после правок, — и каждый прогон не должен ложиться нагрузкой на чужой сервер.
"""

import json
import time
import urllib.request
from pathlib import Path
from typing import Any

BASE = "https://api.tabiatsari.uz/api"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "tabiatsari"

# Представляемся честно: у владельца сайта должна быть возможность понять,
# кто ходит, и связаться, если что-то не так
_UA = "Sayr/0.1 (hiking guide for Uzbekistan; +https://sayr.duckdns.org)"
_PAUSE_SEC = 0.25


def _get(url: str, cache_name: str, *, binary: bool = False) -> Any:
    path = CACHE_DIR / cache_name
    if path.exists():
        return path.read_bytes() if binary else json.loads(path.read_text("utf-8"))

    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    # Три попытки с нарастающей паузой: при заливке полусотни мест подряд
    # их сервер начинает рвать соединения на ровном месте (SSL EOF),
    # и один обрыв не должен ронять весь импорт
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    time.sleep(_PAUSE_SEC)

    path.write_bytes(data)
    return data if binary else json.loads(data.decode("utf-8"))


def points() -> list[dict]:
    """Опубликованные точки. Черновики источника пропускаем: у всех трёх
    нет треков, а именно за ними мы и идём."""
    data = _get(f"{BASE}/points?status=published&fetchAll=true", "points.json")
    return [p for p in data["data"] if "test" not in p["name"].lower()]


def point(point_id: str) -> dict:
    data = _get(f"{BASE}/points/{point_id}", f"points/{point_id}.json")
    return data.get("data", data)


def tracks(point_id: str) -> list[dict]:
    data = _get(f"{BASE}/points/{point_id}/tracks", f"tracks/{point_id}.json")
    return data.get("data", [])


def fetch_file(url: str) -> bytes:
    """Файл с assets.tabiatsari.uz — GPX или снимок."""
    return _get(url, f"files/{url.rsplit('/', 1)[-1]}", binary=True)
