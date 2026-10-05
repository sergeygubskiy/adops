"""Оркестрация переноса расходов в трекер: строки ввода → карта меток → запись + бэкап.

Поток `run_sync`:
  1) клиент из настроек (нет ключа — ошибка, выход);
  2) список кампаний (заодно проверка ключа);
  3) КАРТА «метка → кампании» живьём: отчёты всех кампаний читаются параллельно;
  4) по каждой строке: план записи → dry-run (превью, БЕЗ записи) ИЛИ запись + бэкап старых сумм;
  5) результат в ПОРЯДКЕ ВВОДА + сводка по бакетам.

Безопасность: запись адресная (кампания + значение метки); не найдено — НЕ пишем; перед записью
старые суммы сохраняются в бэкап (права 600). Частичный сбой при делёже суммы между кампаниями
виден в статусе («ЧАСТИЧНО»), а не прячется в «обновлено».
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from adops.tracker import api, cost

BACKUP_DIR = Path.home() / ".adops"


def _noop(*a, **k):
    pass


def _save_backup(rows: list[dict]) -> Path:
    """Сохранить старые/новые суммы перед записью (для отката). Права 600."""
    path = BACKUP_DIR / f"cost_backup_{int(time.time())}.json"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def build_tag_map(client, cids, emit, stop_event, workers=6):
    """{norm_tag: [(campaign_id, canonical_tag, clicks, cost), ...]} — отчёты кампаний читаются параллельно."""
    tag_map: dict[str, list] = {}
    lock = threading.Lock()
    done = [0]
    total = len(cids)

    def _scan(cid):
        if stop_event.is_set():
            return cid, None
        return cid, client.tag_report(cid)     # TrackerAuthError пробрасывается наружу

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_scan, cid): cid for cid in cids}
        for fut in as_completed(futs):
            cid = futs[fut]
            try:
                _cid, rep = fut.result()
            except api.TrackerAuthError:
                raise
            except Exception as e:                # одна кампания упала — не роняем карту
                rep = e
            with lock:
                done[0] += 1
                if done[0] % 10 == 0 or done[0] == total:
                    emit(f"  карта: {done[0]}/{total} кампаний", "info")
            if isinstance(rep, Exception) or rep is None:
                if isinstance(rep, Exception):
                    emit(f"  ! кампания {cid}: {rep}", "warn")
                continue
            for name, clicks, spent in rep:
                k = cost.norm_tag(name)
                if k:
                    tag_map.setdefault(k, []).append((cid, name, clicks, spent))
    return tag_map


def run_sync(rows, emit=None, stop_event=None, cfg=None, client=None, dry_run=False):
    """rows: [{"tag","cost","key"}] (из cost.parse_rows). → list[(tag, cost, status)] в ПОРЯДКЕ ВВОДА."""
    emit = emit or _noop
    stop_event = stop_event or threading.Event()
    cfg = cfg or {}
    map_workers = int(cfg.get("map_workers", 6) or 6)
    write_workers = int(cfg.get("write_workers", 4) or 4)

    if not rows:
        emit("Пусто — вставь метки и суммы.", "head")
        return []

    def _all(status):
        return [(r["tag"], r["cost"], status) for r in rows]

    try:
        client = client or api.TrackerClient.from_config()
    except RuntimeError as e:
        emit(f"⛔ {e}", "err")
        return _all("нет ключа")

    try:
        camps = client.list_campaigns()
    except api.TrackerAuthError as e:
        emit(f"⛔ {e}", "err")
        return _all("ключ невалиден")
    except api.TrackerError as e:
        emit(f"⛔ Трекер недоступен: {e}", "err")
        return _all("нет связи")

    cids = [c["id"] for c in camps]
    name_by_cid = {c["id"]: (c.get("name") or str(c["id"])) for c in camps}
    mode = "ПРОВЕРКА (без записи)" if dry_run else "ЗАПИСЬ расходов"
    emit(f"{mode}. Кампаний: {len(camps)}. Строю карту меток…", "head")

    try:
        tag_map = build_tag_map(client, cids, emit, stop_event, workers=map_workers)
    except api.TrackerAuthError as e:
        emit(f"⛔ {e}", "err")
        return _all("ключ невалиден")
    if stop_event.is_set():
        emit("Остановлено (карта не достроена) — записи нет.", "head")
        return _all("стоп")
    emit(f"Карта готова: {len(tag_map)} меток.", "info")

    backup, backup_lock = [], threading.Lock()

    def _nm(cid):
        return name_by_cid.get(cid, str(cid))

    def _one(pair):
        idx, r = pair
        if stop_event.is_set():
            return idx, (r["tag"], r["cost"], "стоп")
        plan = cost.plan_writes(r["key"], tag_map, r["cost"])
        if not plan:
            return idx, (r["tag"], r["cost"], "метка не найдена")
        split = len(plan) > 1

        if dry_run:
            if not split:
                cid, _canon, _clicks, old_cost, share = plan[0]
                return idx, (r["tag"], r["cost"], f"{_nm(cid)}: было ${cost.fmt_money(old_cost)} "
                                                  f"→ станет ${cost.fmt_money(share)}")
            tot_clicks = sum(p[2] for p in plan)
            bits = "; ".join(f"{_nm(p[0])} ${cost.fmt_money(p[4])} ({p[2]} кл.)" for p in plan)
            return idx, (r["tag"], r["cost"], f"делю на {len(plan)} кампании по {tot_clicks} кл. — {bits}")

        # ЗАПИСЬ: по каждой кампании плана; ни один сбой не глотается — частичный результат виден.
        wrote, errs = [], []
        for cid, canon, _clicks, old_cost, share in plan:
            body = cost.build_cost_body(canon, share)
            try:
                ok, _http, msg = client.update_cost(cid, body)
            except api.TrackerAuthError:
                errs.append(f"{_nm(cid)}: ключ невалиден")
                break
            except api.TrackerError:
                errs.append(f"{_nm(cid)}: ошибка сети")
                continue
            if ok:
                wrote.append((cid, share, old_cost))
                with backup_lock:
                    backup.append({"tag": canon, "campaign_id": cid, "campaign": _nm(cid),
                                   "old_cost": old_cost, "new_cost": share, "ts": int(time.time())})
            elif "not found" in (msg or "").lower():
                errs.append(f"{_nm(cid)}: нет кликов")
            else:
                errs.append(f"{_nm(cid)}: {msg}")

        if wrote and not errs:
            if not split:
                _cid, share, old_cost = wrote[0]
                return idx, (r["tag"], r["cost"], f"обновлено ${cost.fmt_money(share)} "
                                                  f"(было ${cost.fmt_money(old_cost)})")
            bits = "; ".join(f"{_nm(c)} ${cost.fmt_money(s)}" for c, s, _o in wrote)
            return idx, (r["tag"], r["cost"], f"разделено на {len(wrote)}: {bits}")
        if wrote and errs:
            bits = "; ".join(f"{_nm(c)} ${cost.fmt_money(s)}" for c, s, _o in wrote)
            return idx, (r["tag"], r["cost"], f"ЧАСТИЧНО: записано {bits} · НЕ записано — {'; '.join(errs)}")
        return idx, (r["tag"], r["cost"], f"ошибка: {'; '.join(errs) or 'ничего не записано'}")

    slot = {}
    with ThreadPoolExecutor(max_workers=max(1, write_workers)) as ex:
        for idx, res in ex.map(_one, list(enumerate(rows))):
            slot[idx] = res
            emit(f"• {res[0]} — {res[2]}", "info")

    if backup and not dry_run:
        try:
            p = _save_backup(backup)
            emit(f"Бэкап старых сумм ({len(backup)}): {p}", "info")
        except OSError as e:
            emit(f"Бэкап не сохранён: {e}", "warn")

    results = [slot.get(i, (r["tag"], r["cost"], "?")) for i, r in enumerate(rows)]
    cnt = Counter(bucket(st) for _t, _c, st in results)
    emit(f"ИТОГ ({'проверка' if dry_run else 'запись'}): " + " · ".join(f"{k}: {v}" for k, v in cnt.items()), "head")
    return results


def bucket(status: str) -> str:
    """Статус строки → бакет сводки."""
    s = status or ""
    if s.startswith("ЧАСТИЧНО"):          # частичная запись не должна прятаться в успех
        return "частично"
    if s.startswith("обновлено") or s.startswith("разделено"):
        return "обновлено"
    if ("было $" in s and "станет" in s) or s.startswith("делю на"):
        return "готово к записи"
    if "не найдена" in s:
        return "не найдена"
    if "нет кликов" in s:
        return "нет кликов"
    if "стоп" in s:
        return "стоп"
    return "ошибка"
