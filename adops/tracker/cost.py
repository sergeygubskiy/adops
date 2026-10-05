"""Чистая логика переноса расходов в трекер: разбор ввода, сборка тела запроса, раздел суммы.

stdlib-only, без сети — всё детерминированно и покрыто tests/test_tracker_cost.py и
tests/test_tracker_split.py. Сеть — в api.py, оркестрация — в sync.py.

Главное свойство раздела суммы между кампаниями (`split_by_clicks`): сумма долей РОВНО равна
исходной — деньги не теряются и не появляются на округлении (счёт в целых микро-единицах).
"""

from __future__ import annotations

import re

# --- константы тела запроса записи (иллюстративные; под конкретный трекер переопределяются) ---
TAG_SLOT = 2             # номер слота метки в трекере; СТРОГО int
MODEL_TOTAL = "TOTAL"    # общий расход за период (не цена за клик)
PERIOD = "all_time"
TIMEZONE = "UTC"
CURRENCY = "USD"

_TRAIL_SLASH = re.compile(r"/+$")


def norm_tag(s) -> str:
    """Ключ сверки метки: trim → lower → срез хвостовых '/'. Нужен ТОЛЬКО для сопоставления
    «ввод ↔ метка из отчёта»; в запись идёт каноническое значение из отчёта."""
    return _TRAIL_SLASH.sub("", str(s or "").strip().lower())


def parse_cost(s):
    """'271,83' → 271.83. Запятая-десятичная, срез пробелов/$/неразрывных пробелов.

    float ≥ 0 (округл. до 6 знаков) или None: не число / отрицательное / неоднозначное."""
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    t = t.replace(" ", "").replace(" ", "").replace("$", "")
    t = t.replace(",", ".")
    if t.count(".") > 1:          # '1.234.56' — неоднозначно (тысячные?), не гадаем
        return None
    try:
        v = float(t)
    except (ValueError, TypeError):
        return None
    if v != v or v in (float("inf"), float("-inf")):   # NaN/inf
        return None
    if v < 0:
        return None
    return round(v, 6)


def fmt_money(v) -> str:
    """271.83 → '271.83', 0.0007 → '0.0007', 100.0 → '100', None → '—'. Без хвостовых нулей."""
    if v is None:
        return "—"
    try:
        s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return "—"
    return s or "0"


_HEADER_HINT = re.compile(r"(метк|tag|\bcost\b|сумм|трат|amount)", re.IGNORECASE)


def looks_like_header(cells) -> bool:
    """Строка-шапка вставленной таблицы («Метка | Сумма») → пропустить, не считать данными."""
    joined = " ".join(str(c) for c in cells)
    if not joined.strip():
        return False
    has_hint = bool(_HEADER_HINT.search(joined))
    has_digit = any(ch.isdigit() for ch in joined)
    return has_hint and not has_digit


def parse_rows(grid):
    """grid: строки [метка, сумма] из таблицы интерфейса.

    → (rows, errors); rows = [{"tag": <точное значение>, "cost": float, "key": <norm_tag>}] в ПОРЯДКЕ ВВОДА.
    Пустая строка и шапка — пропуск; частичная или плохая сумма — ошибка; дубль — пропуск (первая остаётся)."""
    rows, errors, seen = [], [], set()
    for i, r in enumerate(grid, 1):
        cells = [(str(c).strip() if c is not None else "") for c in (list(r) + ["", ""])[:2]]
        tag, cost_s = cells
        if not tag and not cost_s:
            continue
        if looks_like_header(cells):
            continue
        if not tag or not cost_s:
            errors.append(f"строка {i}: нужны и метка, и сумма")
            continue
        cost = parse_cost(cost_s)
        if cost is None:
            errors.append(f"строка {i}: плохая сумма «{cost_s}»")
            continue
        key = norm_tag(tag)
        if key in seen:
            errors.append(f"строка {i}: дубль метки {tag} — пропущен (оставлена первая)")
            continue
        seen.add(key)
        rows.append({"tag": tag, "cost": cost, "key": key})
    return rows, errors


def build_cost_body(tag_value, cost) -> dict:
    """Тело запроса записи расхода. tag_value — КАНОНИЧЕСКОЕ значение метки из отчёта, cost — float."""
    return {
        "period": PERIOD,
        "timezone": TIMEZONE,
        "tagSlot": TAG_SLOT,
        "tagValue": tag_value,
        "model": MODEL_TOTAL,
        "cost": cost,
        "currency": CURRENCY,
    }


# Предел точности денег: parse_cost округляет до 6 знаков → считаем в ЦЕЛЫХ микро-единицах.
MICRO = 1_000_000


def split_by_clicks(total, weights):
    """Разделить `total` между получателями пропорционально `weights` (кликам). → list[float].

    Сумма долей РОВНО равна total: счёт в целых микро-единицах (на float наивное `total * w / tw`
    для трёх равных весов даёт 66.67+66.67+66.67 = 200.01) + остаток методом НАИБОЛЬШИХ ОСТАТКОВ
    (при равенстве — кто левее; порядок получателей задаёт вызывающий и он обязан быть детерминированным).

    Нулевой/отрицательный вес → доля 0. Сумма весов 0 или total ≤ 0 → все доли 0:
    делить не по чему, и молча свалить всё на первого нельзя."""
    ws = []
    for w in weights or []:
        try:
            ws.append(max(0, int(w or 0)))
        except (TypeError, ValueError):
            ws.append(0)
    n = len(ws)
    if n == 0:
        return []
    tw = sum(ws)
    try:
        total_micro = int(round(float(total or 0) * MICRO))
    except (TypeError, ValueError):
        return [0.0] * n
    if tw <= 0 or total_micro <= 0:
        return [0.0] * n

    parts, rems = [], []
    for w in ws:
        q, r = divmod(total_micro * w, tw)
        parts.append(q)
        rems.append(r)
    left = total_micro - sum(parts)            # всегда 0 ≤ left < n
    for i in sorted(range(n), key=lambda j: (-rems[j], j))[:left]:
        parts[i] += 1
    return [p / MICRO for p in parts]


def plan_writes(key, tag_map, cost):
    """Что и куда писать по одной строке ввода. → list[(cid, canon_tag, clicks, old_cost, share)].

    tag_map: {norm_tag: [(campaign_id, canonical_tag, clicks, current_cost), ...]}.
    Пусто — метки нет в карте. Один элемент — вся сумма в одну кампанию. 2+ — метка набрала клики
    в нескольких кампаниях, сумма делится ПРОПОРЦИОНАЛЬНО КЛИКАМ (это оценка, а не факт).

    Кампании с нулём кликов в дележе не участвуют. Порядок результата детерминирован
    (клики по убыванию, затем cid): карта строится параллельно, её порядок случаен, а от порядка
    зависит раздача остатка."""
    entries = tag_map.get(key) or []
    if not entries:
        return []
    with_clicks = sorted([e for e in entries if (e[2] or 0) > 0],
                         key=lambda e: (-(e[2] or 0), e[0]))
    if len(with_clicks) > 1:
        shares = split_by_clicks(cost, [e[2] for e in with_clicks])
        return [(e[0], e[1], e[2], e[3], s) for e, s in zip(with_clicks, shares)]
    best = max(entries, key=lambda e: e[2] or 0)
    return [(best[0], best[1], best[2], best[3], cost)]
