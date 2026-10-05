"""Пакетная проверка состояния: имена → профили → очередь ≤N открытых → результат по строкам.

Склейка трёх частей: резолв имён (profiles), очередь с потолком открытых профилей (queue_runner)
и проверка состояния по зонду страницы (health). Политика «кого оставить открытым»:
аккаунт с проблемой остаётся открытым для ручного разбора, и его слот возвращается в очередь
через `CloseReconciler`, когда окно закрылось; здоровый — закрывается сразу.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

from adops import health, profiles, queue_runner as qr


def _noop(*a, **k):
    pass


@dataclass
class Runtime:
    """Общий для приложения «завод»: один потолок открытых, один поллер, один реконсилятор.

    Создаётся приложением ОДИН раз и переживает отдельные пачки — тогда потолок «≤N открытых»
    действует на все пачки сразу, а окна, оставленные открытыми, продолжают учитываться."""
    gate: qr.OpenGate
    reconciler: qr.CloseReconciler
    poller: qr.ActivePoller

    @classmethod
    def create(cls, backend, n: int = 15, poll_interval: float = 2.0,
               max_hold: Optional[float] = None, emit=None) -> "Runtime":
        def confirm_closed(pid):
            """Точечная проверка перед возвратом слота. Пустое множество — валидный ответ (никого нет),
            None — опрос не удался и закрытие НЕ подтверждено."""
            ids = backend.active_ids()
            return ids is not None and pid not in ids

        rec = qr.CloseReconciler(need_absent=2, max_hold=max_hold, emit=emit, confirm_closed=confirm_closed)
        poller = qr.ActivePoller(backend.active_ids, interval=poll_interval, on_snapshot=rec.tick)
        poller.start()
        return cls(qr.OpenGate(n), rec, poller)

    def stop(self):
        self.poller.stop()


def run_health_batch(names, backend, probe_for: Callable, n: int = 15,
                     stop_event: Optional[threading.Event] = None, emit=None, on_row=None,
                     on_open=None, check_kwargs: Optional[dict] = None,
                     leave_open=(health.SUSPENDED, health.VERIFICATION, health.NOT_READY),
                     runtime: Optional[Runtime] = None, poll_interval: float = 2.0,
                     max_hold: Optional[float] = None):
    """→ {имя: строка статуса}.

    probe_for(profile_id) → PageProbe; backend — ProfileBackend; n — потолок одновременно открытых.
    on_row(name, line) вызывается по мере готовности (таблица наполняется по ходу, а не в конце);
    on_open(in_use, capacity) — индикатор «открыто X/N». runtime — общий «завод» приложения; без него
    создаётся одноразовый на эту пачку."""
    emit = emit or _noop
    on_row = on_row or _noop
    stop_event = stop_event or threading.Event()
    check_kwargs = check_kwargs or {}
    results: dict = {}
    lock = threading.Lock()

    resolved, bad = profiles.resolve_names(names, backend)
    for name, why in bad:
        results[name] = why
        on_row(name, why)
        emit(f"{name}: {why}", "warn")
    if not resolved:
        return results

    owns = runtime is None
    rt = runtime or Runtime.create(backend, n, poll_interval, max_hold, emit)
    gate = rt.gate

    def _report(name, line):
        with lock:
            results[name] = line
        on_row(name, line)
        if on_open:
            on_open(*gate.snapshot())

    def work(item, stop):
        name, pid = item
        if not backend.start(pid):
            _report(name, "не открылся")
            return None
        if on_open:
            on_open(*gate.snapshot())
        try:
            res = health.check_account(probe_for(pid), should_stop=stop.is_set, **check_kwargs)
        except NotImplementedError as e:
            backend.stop(pid)
            _report(name, str(e))
            return None
        except Exception as e:                       # сбой одного профиля не роняет пачку
            backend.stop(pid)
            _report(name, f"ошибка: {type(e).__name__}")
            return None
        _report(name, res.line())
        if res.state in leave_open:
            return pid                               # окно остаётся; слот вернёт реконсилятор
        backend.stop(pid)
        return None

    try:
        qr.run_queue(resolved, work, n, stop_event, gate=gate, emit=emit, reconciler=rt.reconciler)
    finally:
        if owns:
            rt.stop()
    if on_open:
        on_open(*gate.snapshot())
    return results
