"""Навигация по рекламному кабинету — ИНТЕРФЕЙС-ЗАГЛУШКА.

Навигация по кабинету не публикуется: ни пути по интерфейсу, ни привязки к элементам страницы,
ни логика входа в этот репозиторий не входят. Остаётся только контракт: что движку нужно от
слоя, который умеет работать с живым кабинетом.

Реализация этого слоя должна отдавать `PageProbe` (см. adops.health) для открытого профиля.
"""

from __future__ import annotations

from typing import Protocol

from adops.health import PageProbe

NOT_PUBLISHED = "навигация по кабинету не публикуется"


class CabinetNavigator(Protocol):
    def probe_for(self, profile_id: str) -> PageProbe:
        """Зонд страницы кабинета для открытого профиля."""


class UnavailableNavigator:
    """Значение по умолчанию в публичной версии: честно сообщает, что слой не опубликован."""

    def probe_for(self, profile_id: str) -> PageProbe:
        raise NotImplementedError(NOT_PUBLISHED)
