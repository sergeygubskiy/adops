"""Контроллер между интерфейсом и движком: потоки + очередь событий.

Интерфейс (Tkinter) не вызывает движок напрямую в своём потоке: каждая операция идёт в отдельном
потоке, а результаты возвращаются через `queue.Queue` событий, которую окно опрашивает по таймеру.
Поэтому окно не зависает на пачке, а контроллер тестируется без Tk.

События (кортежи):
  ("log", текст, вид)         — строка лога (вид: info | head | warn | err)
  ("row", имя, статус)        — строка таблицы проверки
  ("open", in_use, capacity)  — индикатор «открыто X/N»
  ("sync_row", метка, сумма, статус)
  ("done", что)               — операция завершена ("health" | "sync")
"""

from __future__ import annotations

import queue
import threading

from adops import batch
from adops.tracker import cost, sync


class Controller:
    def __init__(self, backend, probe_for, events: "queue.Queue | None" = None,
                 capacity: int = 15, check_kwargs: dict | None = None, tracker_client=None):
        self.backend = backend
        self.probe_for = probe_for
        self.events = events or queue.Queue()
        self.capacity = capacity
        self.check_kwargs = check_kwargs or {}
        self.tracker_client = tracker_client      # None → клиент из настроек
        self.stop_event = threading.Event()
        self._runtime = None
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    # ── служебное ────────────────────────────────────────────────────────────────────────
    def _emit(self, text, kind="info"):
        self.events.put(("log", text, kind))

    def _get_runtime(self):
        """Один общий «завод» на приложение; пересоздаётся только при смене потолка."""
        with self._lock:
            rt = self._runtime
            if rt is None or rt.gate.capacity != self.capacity:
                if rt is not None:
                    rt.stop()
                self._runtime = batch.Runtime.create(self.backend, self.capacity, emit=self._emit)
            return self._runtime

    def _spawn(self, target, name):
        t = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(t)
        t.start()
        return t

    def set_capacity(self, n: int):
        self.capacity = max(1, int(n))

    # ── проверка состояния ───────────────────────────────────────────────────────────────
    def start_health(self, names):
        """Независимый воркер: можно запускать новую пачку, пока предыдущая ждёт."""
        names = [n for n in names if (n or "").strip()]
        self.stop_event.clear()
        rt = self._get_runtime()

        def _run():
            try:
                self._emit(f"Проверка состояния: {len(names)} имён, потолок открытых {self.capacity}", "head")
                batch.run_health_batch(
                    names, self.backend, self.probe_for, n=self.capacity, stop_event=self.stop_event,
                    emit=self._emit, on_row=lambda nm, st: self.events.put(("row", nm, st)),
                    on_open=lambda used, cap: self.events.put(("open", used, cap)),
                    check_kwargs=self.check_kwargs, runtime=rt)
            finally:
                self.events.put(("done", "health"))

        return self._spawn(_run, "health")

    # ── перенос расходов ─────────────────────────────────────────────────────────────────
    def start_sync(self, grid, dry_run=True):
        rows, errors = cost.parse_rows(grid)
        for e in errors:
            self._emit(e, "warn")
        self.stop_event.clear()

        def _run():
            try:
                res = sync.run_sync(rows, emit=self._emit, stop_event=self.stop_event,
                                    client=self.tracker_client, dry_run=dry_run)
                for tag, amount, status in res:
                    self.events.put(("sync_row", tag, amount, status))
            finally:
                self.events.put(("done", "sync"))

        return self._spawn(_run, "sync")

    # ── общее ────────────────────────────────────────────────────────────────────────────
    def close_open(self):
        """Закрыть все открытые профили (оператор разобрал проблемные окна). Освободившиеся слоты
        вернёт реконсилятор по очередному опросу active()."""
        ids = self.backend.active_ids() or set()
        for pid in sorted(ids):
            self.backend.stop(pid)
        self._emit(f"Закрыто профилей: {len(ids)}", "info")
        return len(ids)

    def stop(self):
        self.stop_event.set()

    def shutdown(self):
        self.stop_event.set()
        if self._runtime is not None:
            self._runtime.stop()

    def join(self, timeout=10.0):
        for t in list(self._threads):
            t.join(timeout)
