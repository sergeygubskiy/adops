"""Пакетная проверка: очередь ≤N + резолв имён + политика «оставить открытым» — на фейковом менеджере профилей."""
import threading
import time

from adops import batch, health
from adops.demo import DemoProbe, demo_backend, demo_probe_for
from adops.profiles import FakeBackend, resolve_names

FAST = {"funds_wait": 5.0, "bar_wait": 1.0, "poll": 0.01}


def wait_until(pred, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_resolve_names_zero_one_many():
    b = FakeBackend({"p1": "a", "p2": "b", "p3": "b"})
    ok, bad = resolve_names(["a", "b", "zzz", " a ", ""], b)
    assert ok == [("a", "p1")]
    assert dict(bad)["zzz"] == "не найден" and dict(bad)["b"].startswith("неоднозначно")


def test_names_are_case_sensitive():
    b = FakeBackend({"p1": "Spy"})
    assert resolve_names(["spy"], b)[0] == []


def test_ceiling_is_never_exceeded():
    backend = FakeBackend({f"p{i}": f"n{i}" for i in range(20)})
    res = batch.run_health_batch([f"n{i}" for i in range(20)], backend,
                                 lambda pid: DemoProbe("ok", delay=0.01), n=3, check_kwargs=FAST)
    assert len(res) == 20 and all(v.startswith("active") for v in res.values())
    assert backend.max_open <= 3
    assert backend.active_ids() == set(), "здоровые профили закрыты"


def test_problem_profiles_stay_open_and_slots_return_when_closed():
    """Проблемное окно держит слот, пока его не закроют; пока потолок занят, остальные ждут."""
    backend, plan = demo_backend(12)
    names = [f"acc-{i:02d}" for i in range(3, 13)]          # включает suspended (5, 10) и verify (7)
    rt = batch.Runtime.create(backend, n=2, poll_interval=0.02)
    problem_ids = {backend.find(f"acc-{i:02d}")[0] for i in (5, 7, 10)}
    closed, stop_op = set(), threading.Event()

    def operator():                                         # оператор разбирает проблемные окна по одному
        while not stop_op.is_set():
            time.sleep(0.05)
            for pid in sorted(problem_ids & backend.active_ids()):
                backend.stop(pid)
                closed.add(pid)
                break
    t = threading.Thread(target=operator, daemon=True)
    t.start()
    try:
        res = batch.run_health_batch(names, backend, demo_probe_for(plan, delay=0), n=2,
                                     check_kwargs=FAST, runtime=rt)
        assert len(res) == 10 and backend.max_open <= 2
        assert {res["acc-05"], res["acc-10"]} == {"suspended"} and res["acc-07"] == "verification"
        assert wait_until(lambda: closed == problem_ids), "каждое проблемное окно было открыто и закрыто оператором"
        assert wait_until(lambda: rt.gate.snapshot()[0] == 0 and rt.reconciler.pending_count() == 0), \
            "слоты не вернулись после закрытия окон"
    finally:
        stop_op.set()
        rt.stop()


def test_failed_start_is_reported_and_slot_released():
    backend = FakeBackend({"p1": "a", "p2": "b"}, start_fail={"p1"})
    res = batch.run_health_batch(["a", "b"], backend, lambda pid: DemoProbe("ok"), n=1, check_kwargs=FAST)
    assert res["a"] == "не открылся" and res["b"].startswith("active")


def test_probe_exception_does_not_kill_batch():
    backend = FakeBackend({"p1": "a", "p2": "b"})

    def probe_for(pid):
        if pid == "p1":
            raise RuntimeError("boom")
        return DemoProbe("ok")
    res = batch.run_health_batch(["a", "b"], backend, probe_for, n=2, check_kwargs=FAST)
    assert res["a"].startswith("ошибка") and res["b"].startswith("active")
    assert "p1" not in backend.active_ids()


def test_unpublished_navigation_is_reported_honestly():
    from adops.cabinet import NOT_PUBLISHED, UnavailableNavigator
    backend = FakeBackend({"p1": "a"})
    res = batch.run_health_batch(["a"], backend, UnavailableNavigator().probe_for, n=1)
    assert res["a"] == NOT_PUBLISHED and backend.active_ids() == set()


def test_rows_stream_as_ready_and_unresolved_reported_first():
    backend = FakeBackend({"p1": "a"})
    seen = []
    batch.run_health_batch(["ghost", "a"], backend, lambda pid: DemoProbe("ok"), n=1,
                           check_kwargs=FAST, on_row=lambda n, s: seen.append(n))
    assert seen[0] == "ghost" and seen[-1] == "a"


def test_stop_before_start_opens_nothing():
    backend = FakeBackend({f"p{i}": f"n{i}" for i in range(5)})
    stop = threading.Event()
    stop.set()
    batch.run_health_batch([f"n{i}" for i in range(5)], backend, lambda pid: DemoProbe("ok"),
                           n=2, stop_event=stop, check_kwargs=FAST)
    assert backend.starts == []
