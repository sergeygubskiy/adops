"""Демо-данные: фейковый менеджер профилей и «страницы» с заскриптованными состояниями.

Нужны, чтобы интерфейс и движок можно было запустить и посмотреть без какого-либо реального
кабинета: `python3 -m adops`. Ничего реального здесь нет — имена и статусы синтетические.
"""

from __future__ import annotations

import time

from adops.profiles import FakeBackend

# синтетические сценарии страницы: (уведомления карусели, статусы объявлений)
SCENARIOS = {
    "ok": ([["Tip: add more assets"]], ["Eligible", "Eligible", "Paused"]),
    "paused": ([["Tip: add more assets"]], ["Paused", "Paused"]),
    "suspended": ([["Improve account security"], ["Your account is suspended"]], ["Not eligible", "Paused"]),
    "verify": ([["Verify your account"]], ["Under review"]),
}


class DemoProbe:
    """PageProbe поверх сценария. `ready_after` — сколько опросов страница «грузится» (имитация медленного канала)."""

    def __init__(self, scenario: str = "ok", ready_after: int = 0, delay: float = 0.0):
        self.alerts, self.statuses = SCENARIOS[scenario]
        self.idx = 0
        self._calls = 0
        self._ready_after = ready_after
        self._delay = delay

    def _ready(self):
        self._calls += 1
        return self._calls > self._ready_after

    def balance_text(self):
        if self._delay:
            time.sleep(self._delay)
        return "$12.50" if self._ready() else None

    def alert_page(self):
        return list(self.alerts[self.idx]), self.idx + 1, len(self.alerts)

    def next_alert(self):
        if self.idx + 1 < len(self.alerts):
            self.idx += 1
            return True
        return False

    def ad_statuses(self):
        return list(self.statuses)


def demo_backend(count: int = 12) -> tuple[FakeBackend, dict]:
    """→ (backend, {profile_id: scenario}). Имена acc-01…; один дубль имени, чтобы показать «неоднозначно»."""
    catalog, plan = {}, {}
    for i in range(1, count + 1):
        pid = f"p{i:03d}"
        catalog[pid] = f"acc-{i:02d}"
        plan[pid] = "suspended" if i % 5 == 0 else "verify" if i % 7 == 0 else "paused" if i % 4 == 0 else "ok"
    catalog["p900"] = "acc-02"          # дубль имени → «неоднозначно»
    plan["p900"] = "ok"
    return FakeBackend(catalog), plan


def demo_probe_for(plan: dict, delay: float = 0.05):
    return lambda pid: DemoProbe(plan.get(pid, "ok"), delay=delay)
