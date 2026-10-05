"""Контроллер интерфейса: события приходят через очередь, окно не нужно (Tk не импортируется)."""
import queue

from adops.controller import Controller
from adops.demo import demo_backend, demo_probe_for
from adops.profiles import FakeBackend
from adops.tracker import sync

FAST = {"funds_wait": 5.0, "bar_wait": 1.0, "poll": 0.01}


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def test_health_events_and_done():
    backend, plan = demo_backend(6)
    ctl = Controller(backend, demo_probe_for(plan, delay=0), capacity=2, check_kwargs=FAST)
    ctl.start_health(["acc-01", "acc-03", "acc-02", "nope"])
    ctl.join()
    ev = drain(ctl.events)
    rows = {e[1]: e[2] for e in ev if e[0] == "row"}
    assert rows["acc-01"].startswith("active")
    assert rows["nope"] == "не найден" and rows["acc-02"].startswith("неоднозначно")
    assert ev[-1] == ("done", "health")
    assert any(e[0] == "open" and e[2] == 2 for e in ev)
    ctl.shutdown()


def test_sync_dry_run_through_controller(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "BACKUP_DIR", tmp_path)

    class C:
        def list_campaigns(self):
            return [{"id": 1, "name": "c1"}]
        def tag_report(self, cid):
            return [("t-1/a", 10, 1.0)]
        def update_cost(self, *a):
            raise AssertionError("dry-run не должен писать")

    ctl = Controller(FakeBackend({}), lambda p: None, tracker_client=C())
    ctl.start_sync([["t-1/a", "5,5"], ["bad", "x"]], dry_run=True)
    ctl.join()
    ev = drain(ctl.events)
    assert ("sync_row", "t-1/a", 5.5, "c1: было $1 → станет $5.5") in ev
    assert any(e[0] == "log" and "плохая сумма" in e[1] for e in ev)
    assert ev[-1] == ("done", "sync")


def test_capacity_change_recreates_runtime():
    backend, plan = demo_backend(3)
    ctl = Controller(backend, demo_probe_for(plan, delay=0), capacity=2)
    rt1 = ctl._get_runtime()
    ctl.set_capacity(4)
    rt2 = ctl._get_runtime()
    assert rt2 is not rt1 and rt2.gate.capacity == 4
    ctl.shutdown()


def test_close_open_returns_slots():
    backend, plan = demo_backend(6)
    ctl = Controller(backend, demo_probe_for(plan, delay=0), capacity=3, check_kwargs=FAST)
    ctl.start_health(["acc-05"])               # suspended → окно остаётся открытым
    ctl.join()
    assert backend.active_ids() == {"p005"}
    assert ctl.close_open() == 1 and backend.active_ids() == set()
    ctl.shutdown()
