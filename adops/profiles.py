"""Интерфейс к менеджеру профилей браузера + in-memory реализация для тестов и демо.

Реальный клиент менеджера профилей в публичную версию не входит: движку нужны только четыре
операции (`ProfileBackend`). Профили идентифицируются по `profile_id`, а ИМЯ — это лишь запрос
с тремя исходами: 0 найдено / 1 найден / N найдено («неоднозначно» — молча не открываем).
"""

from __future__ import annotations

import threading
from typing import Optional, Protocol


class ProfileBackend(Protocol):
    def find(self, name: str) -> list[str]:
        """Имя → список profile_id (регистрозависимо)."""

    def start(self, profile_id: str) -> bool:
        """Открыть профиль. True — открыт."""

    def stop(self, profile_id: str) -> None:
        """Закрыть профиль."""

    def active_ids(self) -> Optional[set]:
        """Множество открытых профилей или None, если опрос не удался."""


def resolve_names(names, backend: ProfileBackend):
    """Список имён → ([(name, profile_id)], [(name, причина)]).

    Имена не уникальны: 0 совпадений — «не найден», 2+ — «неоднозначно» (профиль НЕ открывается)."""
    ok, bad, seen = [], [], set()
    for raw in names:
        name = (raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ids = backend.find(name)
        if not ids:
            bad.append((name, "не найден"))
        elif len(ids) > 1:
            bad.append((name, f"неоднозначно ({len(ids)})"))
        else:
            ok.append((name, ids[0]))
    return ok, bad


class FakeBackend:
    """In-memory менеджер профилей. `catalog`: {profile_id: name}. Потокобезопасен."""

    def __init__(self, catalog: dict, start_fail: Optional[set] = None):
        self.catalog = dict(catalog)
        self.start_fail = set(start_fail or ())
        self._open: set = set()
        self._lock = threading.Lock()
        self.max_open = 0
        self.starts: list = []

    def find(self, name):
        return [pid for pid, n in self.catalog.items() if n == name]

    def start(self, profile_id):
        if profile_id in self.start_fail:
            return False
        with self._lock:
            self._open.add(profile_id)
            self.max_open = max(self.max_open, len(self._open))
            self.starts.append(profile_id)
        return True

    def stop(self, profile_id):
        with self._lock:
            self._open.discard(profile_id)

    def active_ids(self):
        with self._lock:
            return set(self._open)
