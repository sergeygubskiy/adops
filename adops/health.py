"""Структура проверки состояния аккаунта: чистая логика + протокол зонда страницы.

Как устроена проверка (и почему именно так):

1. ВЕРДИКТ ТОЛЬКО ПО ДОГРУЖЕННОЙ СТРАНИЦЕ. «Плашки не видно» — не то же самое, что «плашки нет»:
   на медленном канале страница отрисовывается минутами. Поэтому сначала ждём доказательство
   отрисовки (баланс). Нет доказательства до дедлайна — статус `NOT_READY`, а не «всё хорошо».
2. УВЕДОМЛЕНИЯ — КАРУСЕЛЬ. В разметке лежит только текущее; проблемное может быть на 2-м или 3-м
   месте. Поэтому карусель листается до конца, а тексты всех уведомлений накапливаются.
3. ВТОРОЙ ЗАТВОР. Нет проблемных уведомлений, но нет ни одного работающего объявления и есть
   отклонённые — повод ПЕРЕЧИТАТЬ уведомления ещё раз; вердикт всё равно выносят уведомления.
4. ЛОЖНЫЙ НОЛЬ ДОРОЖЕ НЕИЗВЕСТНОСТИ. Не смогли посчитать — пишем `?`, а не 0.

Всё, что касается самого кабинета (как достать тексты, баланс, статусы), спрятано за протоколом
`PageProbe` и в публичную версию не входит: см. `adops.cabinet`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Protocol

# Состояния
ACTIVE = "active"
SUSPENDED = "suspended"
VERIFICATION = "verification"
NOT_READY = "not_ready"

SUSPEND_WORDS = ("suspended",)
VERIFY_WORDS = ("verify your account",)


class PageProbe(Protocol):
    """Что проверке нужно от страницы. Реализация (работа с живым кабинетом) не публикуется."""

    def balance_text(self) -> Optional[str]:
        """Текст баланса или None, пока страница не отрисована."""

    def alert_page(self) -> tuple[list[str], int, int]:
        """(тексты текущего уведомления, номер, всего) — позиция в карусели."""

    def next_alert(self) -> bool:
        """Перелистнуть карусель вперёд. False — дальше листать нельзя."""

    def ad_statuses(self) -> list[str]:
        """Статусы объявлений аккаунта (['Eligible', 'Paused', ...])."""


@dataclass
class HealthResult:
    state: str
    amount: str = ""
    ads: dict = field(default_factory=dict)      # {'Eligible': 2, 'Paused': 1}
    ads_known: bool = False
    note: str = ""

    def line(self) -> str:
        return status_line(self.state, self.ads, self.ads_known)


def parse_amount(text) -> str:
    """'-$7.75' / '₹40,141.58' → число строкой с запятой-десятичной: '-7,75' / '40141,58'.
    Снимает валюту и разделители тысяч, знак сохраняет. '' если числа нет."""
    if not text:
        return ""
    t = str(text).strip()
    m = re.search(r"\d[\d,\s]*(?:\.\d+)?", t)
    if not m:
        return ""
    num = m.group(0).replace(",", "").replace(" ", "").replace(".", ",")
    head = t[:m.start()]
    return ("-" if ("-" in head or "−" in head) else "") + num


def refund_from_funds(amount: str) -> str:
    """Отрицательный баланс (долг) → '0': возврата нет. Иначе — само число."""
    if not amount:
        return amount
    return "0" if str(amount).lstrip().startswith(("-", "−")) else amount


def classify_alerts(alert_texts: Iterable[str]) -> str:
    """Тексты уведомлений → ACTIVE | SUSPENDED | VERIFICATION. Приоритет: suspended > verification."""
    joined = " ".join((t or "").lower() for t in (alert_texts or []))
    if any(w in joined for w in SUSPEND_WORDS):
        return SUSPENDED
    if any(w in joined for w in VERIFY_WORDS):
        return VERIFICATION
    return ACTIVE


def tally(statuses: Iterable[str]) -> dict:
    """['Eligible','Paused','Eligible'] → {'Eligible': 2, 'Paused': 1} (пустые отбрасываются)."""
    out: dict = {}
    for s in statuses or []:
        s = str(s).strip()
        if s:
            out[s] = out.get(s, 0) + 1
    return out


def looks_suspended(kinds: dict) -> bool:
    """Раскладка статусов объявлений намекает на проблемный аккаунт: НИ ОДНОГО работающего и ЕСТЬ
    отклонённые. Это не вердикт, а повод перечитать уведомления."""
    k = {str(a).strip().lower(): int(b or 0) for a, b in (kinds or {}).items()}
    if any(name == "eligible" and cnt > 0 for name, cnt in k.items()):
        return False
    return any(("not eligible" in n or "disapprov" in n) and c > 0 for n, c in k.items())


def status_line(state: str, kinds: dict, kinds_known: bool = True) -> str:
    """Строка статуса для таблицы интерфейса.

    suspended / verification — без деталей по объявлениям; active — `active - 2 Eligible, 1 Paused`
    (один вид — без числа); объявления не прочитались — `active - ?`; страница не догрузилась —
    `not ready (re-check)`. Пауза — не проблема: `active - Paused` нормальный ответ."""
    if state == SUSPENDED:
        return "suspended"
    if state == VERIFICATION:
        return "verification"
    if state == NOT_READY:
        return "not ready (re-check)"
    items = [(str(n), int(c or 0)) for n, c in (kinds or {}).items() if int(c or 0) > 0]
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    if not kinds_known or not items:
        return "active - ?"
    if len(items) == 1:
        return f"active - {items[0][0]}"
    return "active - " + ", ".join(f"{c} {n}" for n, c in items)


def collect_alerts(probe: PageProbe, max_pages: int = 20) -> tuple[list[str], bool]:
    """Пролистать карусель уведомлений до конца, накопив тексты ВСЕХ. → (тексты, панель_отрисована).

    Листание — безопасная операция: ничего не закрывается и не подтверждается. `total == 0` в
    ответе зонда значит «панели уведомлений ещё нет» (страница рисуется), а не «уведомлений нет».
    max_pages — предохранитель от бесконечной карусели."""
    seen: list[str] = []
    bar_seen = False
    for _ in range(max_pages):
        texts, idx, total = probe.alert_page()
        if total > 0 or texts:
            bar_seen = True
        for t in texts:
            if t and t not in seen:
                seen.append(t)
        if total <= 1 or idx >= total:
            break
        if not probe.next_alert():
            break
    return seen, bar_seen


def check_account(probe: PageProbe, funds_wait: float = 150.0, bar_wait: float = 75.0,
                  poll: float = 1.0, clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep,
                  should_stop: Callable[[], bool] = lambda: False) -> HealthResult:
    """Проверка одного аккаунта по зонду. Время и пауза инъецируются — тест идёт без реального ожидания."""
    # 1) доказательство отрисовки: баланс
    t0 = clock()
    funds = None
    while True:
        funds = probe.balance_text()
        if funds:
            break
        if should_stop():
            return HealthResult(NOT_READY, note="остановлено")
        if clock() - t0 >= funds_wait:
            return HealthResult(NOT_READY, note="баланс не появился — страница не догрузилась")
        sleep(poll)
    amount = refund_from_funds(parse_amount(funds))

    # 2) уведомления (карусель целиком). Панель приходит позже баланса: пока её нет — ждём до bar_wait,
    #    но выходим РАНО, как только панель отрисована (даже если проблемных уведомлений в ней нет)
    t1 = clock()
    alerts, bar_seen = collect_alerts(probe)
    while not bar_seen and clock() - t1 < bar_wait:
        if should_stop():
            return HealthResult(NOT_READY, amount, note="остановлено")
        sleep(poll)
        alerts, bar_seen = collect_alerts(probe)
    state = classify_alerts(alerts)
    if state != ACTIVE:
        return HealthResult(state, amount)

    # 3) статусы объявлений + второй затвор
    kinds = tally(probe.ad_statuses())
    if looks_suspended(kinds):
        state = classify_alerts(collect_alerts(probe)[0])
        if state != ACTIVE:
            return HealthResult(state, amount, kinds, True, "поймано вторым затвором")
    return HealthResult(ACTIVE, amount, kinds, bool(kinds))
