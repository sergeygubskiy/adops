"""Окно приложения (Tkinter). Тонкий слой над `adops.controller`.

Две вкладки:
  «Проверка состояния» — список имён слева, таблица «профиль → статус» справа, индикатор «открыто X/N»;
  «Расходы в трекер»   — вставка таблицы «метка | сумма», режим проверки (dry-run) и запись.

Движок работает в своих потоках; окно раз в 100 мс забирает события из очереди контроллера.
Запуск без настройки: `python3 -m adops` — на демо-данных (см. adops.demo).
"""

from __future__ import annotations

import queue
import tkinter as tk
from tkinter import ttk

from adops.controller import Controller
from adops.demo import demo_backend, demo_probe_for
from adops.tracker import cost

POLL_MS = 100


class App(tk.Tk):
    def __init__(self, controller: Controller, sample_names: str = ""):
        super().__init__()
        self.title("AdOps")
        self.geometry("1000x680")
        self.ctl = controller
        self._rows: dict[str, str] = {}          # имя → iid в таблице

        top = ttk.Frame(self, padding=(8, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Потолок открытых профилей:").pack(side="left")
        self.cap_var = tk.IntVar(value=controller.capacity)
        ttk.Spinbox(top, from_=1, to=50, width=4, textvariable=self.cap_var,
                    command=self._on_cap).pack(side="left", padx=(4, 16))
        self.open_var = tk.StringVar(value="открыто: 0/%d" % controller.capacity)
        ttk.Label(top, textvariable=self.open_var).pack(side="left")
        ttk.Button(top, text="Стоп", command=self.ctl.stop).pack(side="right")
        ttk.Button(top, text="Закрыть открытые", command=self.ctl.close_open).pack(side="right", padx=6)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8)
        self._build_health(nb, sample_names)
        self._build_sync(nb)

        self.log = tk.Text(self, height=9, state="disabled", wrap="word")
        self.log.pack(fill="x", padx=8, pady=8)
        for kind, color in (("head", "#1a5fb4"), ("warn", "#9a6700"), ("err", "#c01c28")):
            self.log.tag_config(kind, foreground=color)

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(POLL_MS, self._drain)

    # ── вкладки ──────────────────────────────────────────────────────────────────────────
    def _build_health(self, nb, sample):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text="Проверка состояния")
        left = ttk.Frame(f)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Имена профилей (по одному в строке)").pack(anchor="w")
        self.names = tk.Text(left, width=28, height=20)
        self.names.pack(fill="y", expand=True)
        self.names.insert("1.0", sample)
        ttk.Button(left, text="▶ Проверить", command=self._start_health).pack(fill="x", pady=(6, 0))

        right = ttk.Frame(f)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.table = ttk.Treeview(right, columns=("name", "status"), show="headings")
        self.table.heading("name", text="Профиль")
        self.table.heading("status", text="Статус")
        self.table.column("name", width=160)
        self.table.column("status", width=420)
        self.table.pack(fill="both", expand=True)

    def _build_sync(self, nb):
        f = ttk.Frame(nb, padding=6)
        nb.add(f, text="Расходы в трекер")
        left = ttk.Frame(f)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Метка и сумма (через Tab или пробел)").pack(anchor="w")
        self.grid_in = tk.Text(left, width=34, height=20)
        self.grid_in.pack(fill="y", expand=True)
        row = ttk.Frame(left)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Проверка (без записи)", command=lambda: self._start_sync(True)).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="Записать", command=lambda: self._start_sync(False)).pack(side="left", expand=True, fill="x")

        right = ttk.Frame(f)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.sync_table = ttk.Treeview(right, columns=("tag", "cost", "status"), show="headings")
        for c, t, w in (("tag", "Метка", 180), ("cost", "Сумма", 90), ("status", "Результат", 420)):
            self.sync_table.heading(c, text=t)
            self.sync_table.column(c, width=w)
        self.sync_table.pack(fill="both", expand=True)

    # ── действия ─────────────────────────────────────────────────────────────────────────
    def _on_cap(self):
        try:
            self.ctl.set_capacity(self.cap_var.get())
        except tk.TclError:
            pass

    def _start_health(self):
        self._on_cap()
        names = self.names.get("1.0", "end").splitlines()
        self.table.delete(*self.table.get_children())
        self._rows.clear()
        self.ctl.start_health(names)

    def _start_sync(self, dry_run):
        grid = []
        for line in self.grid_in.get("1.0", "end").splitlines():
            parts = line.replace("\t", " ").rsplit(None, 1)
            grid.append(parts if len(parts) == 2 else [line.strip(), ""])
        self.sync_table.delete(*self.sync_table.get_children())
        self.ctl.start_sync(grid, dry_run=dry_run)

    # ── события ──────────────────────────────────────────────────────────────────────────
    def _drain(self):
        try:
            while True:
                self._handle(self.ctl.events.get_nowait())
        except queue.Empty:
            pass
        self.after(POLL_MS, self._drain)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "log":
            self.log.config(state="normal")
            self.log.insert("end", ev[1] + "\n", ev[2])
            self.log.see("end")
            self.log.config(state="disabled")
        elif kind == "row":
            name, status = ev[1], ev[2]
            if name in self._rows:
                self.table.item(self._rows[name], values=(name, status))
            else:
                self._rows[name] = self.table.insert("", "end", values=(name, status))
        elif kind == "open":
            self.open_var.set("открыто: %d/%d" % (ev[1], ev[2]))
        elif kind == "sync_row":
            self.sync_table.insert("", "end", values=(ev[1], cost.fmt_money(ev[2]), ev[3]))

    def _close(self):
        self.ctl.shutdown()
        self.destroy()


def main():
    backend, plan = demo_backend()
    ctl = Controller(backend, demo_probe_for(plan), capacity=5,
                     check_kwargs={"funds_wait": 5.0, "bar_wait": 2.0, "poll": 0.1})
    sample = "\n".join(sorted(set(backend.catalog.values()))[:12] + ["no-such-profile"])
    App(ctl, sample).mainloop()


if __name__ == "__main__":
    main()
