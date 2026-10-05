"""Тесты ядра очереди queue_runner — на фейках, без браузера.
Проверяет инварианты, добытые состязательной критикой плана:
  ≤capacity открыто ВСЕГДА · release без утечки на исключении · стоп прерывает ожидание слота ·
  wait_closed закрывает по 2 РАЗНЫМ поллам (дебаунс), не по одному · ActivePoller generation."""
import threading
import time
from adops import queue_runner as qr

checks = 0
def ok(cond, msg):
    global checks
    assert cond, "FAIL: " + msg
    checks += 1

def wait_until(pred, timeout=3.0, step=0.005):
    """Ждать условие до timeout (антифлейк для потоковых тестов — вместо фиксированных sleep)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(step)
    return pred()


# ── 1. OpenGate: одновременно открыто НИКОГДА не превышает capacity ──────────────
def test_gate_cap():
    cap = 3
    gate = qr.OpenGate(cap)
    lock = threading.Lock()
    state = {"cur": 0, "max": 0, "done": 0}

    def work(item, stop):
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.02)                    # держим слот (имитация обработки)
        with lock:
            state["cur"] -= 1
            state["done"] += 1
        # return = «профиль закрыт» → пермит отдан в finally слота

    stop = threading.Event()
    qr.run_queue(range(20), work, cap, stop, gate=gate)
    ok(state["max"] <= cap, "max одновременных %d > cap %d" % (state["max"], cap))
    ok(state["done"] == 20, "обработано %d из 20" % state["done"])
    ok(gate.snapshot()[0] == 0, "после прогона in_use=%d, не 0" % gate.snapshot()[0])



# ── 2. release БЕЗ утечки: исключение в work восстанавливает пермит ──────────────
def test_no_leak_on_exception():
    cap = 2
    gate = qr.OpenGate(cap)
    calls = {"n": 0}

    def work(item, stop):
        calls["n"] += 1
        raise RuntimeError("бум")           # каждый профиль падает

    stop = threading.Event()
    errs = []
    qr.run_queue(range(6), work, cap, stop, gate=gate, emit=lambda m, k=None: errs.append(m))
    ok(calls["n"] == 6, "work вызван %d раз, не 6" % calls["n"])
    ok(gate.snapshot()[0] == 0, "УТЕЧКА: in_use=%d после падений (должно 0)" % gate.snapshot()[0])
    # пермиты не утекли → повторный прогон полностью проходит
    got2 = {"n": 0}
    qr.run_queue(range(4), lambda i, s: got2.__setitem__("n", got2["n"] + 1), cap, threading.Event(), gate=gate)
    ok(got2["n"] == 4, "после утечки повторный прогон обработал %d из 4" % got2["n"])



# ── 3. Стоп ДО прогона: ни один слот не открывается ─────────────────────────────
def test_stop_before_run():
    opened = []
    stop = threading.Event()
    stop.set()                              # уже взведён
    qr.run_queue(range(10), lambda i, s: opened.append(i), 3, stop)
    ok(len(opened) == 0, "на взведённом стопе открыто %d (должно 0)" % len(opened))



# ── 4. Стоп во время ожидания слота: acquire прерывается быстро ──────────────────
def test_stop_interrupts_acquire():
    gate = qr.OpenGate(1)
    ok(gate.acquire(threading.Event()) is True, "первый пермит не взят")   # заняли единственный слот
    stop = threading.Event()
    res = {"v": None}

    def try_acquire():
        res["v"] = gate.acquire(stop)       # слот занят → будет ждать

    t = threading.Thread(target=try_acquire); t.start()
    time.sleep(0.1)
    stop.set()                              # просим стоп
    t.join(timeout=2.0)
    ok(not t.is_alive(), "acquire не услышал Стоп (завис)")
    ok(res["v"] is False, "acquire на стопе вернул %r, не False" % res["v"])
    gate.release()



# ── Фейковый поллер для wait_closed: отдаёт заскриптованную (снапшот, generation) ─
class FakePoller:
    def __init__(self, script):
        self.script = list(script)          # [(set|None, gen), ...]; последний повторяется
        self.i = 0
    def read(self):
        if self.i < len(self.script):
            v = self.script[self.i]; self.i += 1; return v
        return self.script[-1]


# ── 5. wait_closed: 'closed' только после 2 РАЗНЫХ поллов без uuid (дебаунс) ──────
def test_wait_closed_debounce():
    # present(gen1) → absent(gen2) → absent(gen3) ⇒ closed (2 разных полла без X)
    p = FakePoller([({"X"}, 1), (set(), 2), (set(), 3)])
    r = qr.wait_closed("X", threading.Event(), p, max_wait=5.0, need_absent=2, poll=0.01)
    ok(r == "closed", "должно 'closed', получено %r" % r)

    # ОДИН и тот же полл (gen не меняется) с отсутствием X → НЕ закрывать (усечённый ответ active())
    p2 = FakePoller([(set(), 7)])           # всегда gen=7, X отсутствует
    r2 = qr.wait_closed("X", threading.Event(), p2, max_wait=0.2, need_absent=2, poll=0.01)
    ok(r2 == "timeout", "одиночный полл дал %r, не 'timeout' (дебаунс не сработал)" % r2)

    # X появляется между отсутствиями → счётчик сбрасывается, не закрываем рано
    p3 = FakePoller([(set(), 1), ({"X"}, 2), (set(), 3)])  # absent, present(сброс), absent → только 1 подряд
    r3 = qr.wait_closed("X", threading.Event(), p3, max_wait=0.2, need_absent=2, poll=0.01)
    ok(r3 == "timeout", "сброс счётчика не сработал: %r" % r3)



# ── 6. wait_closed: стоп и таймаут ──────────────────────────────────────────────
def test_wait_closed_stop_timeout():
    p = FakePoller([({"X"}, 1)])            # X всегда открыт
    st = threading.Event()
    def go(res):
        res["v"] = qr.wait_closed("X", st, p, max_wait=10.0, need_absent=2, poll=0.02)
    res = {}
    t = threading.Thread(target=go, args=(res,)); t.start()
    time.sleep(0.1); st.set(); t.join(timeout=2.0)
    ok(not t.is_alive() and res.get("v") == "stopped", "стоп в wait_closed дал %r" % res.get("v"))

    r = qr.wait_closed("X", threading.Event(), FakePoller([({"X"}, 1)]), max_wait=0.15, need_absent=2, poll=0.02)
    ok(r == "timeout", "таймаут дал %r" % r)



# ── 7. take(): reconcile тёплых профилей уменьшает доступные слоты ───────────────
def test_take_reconcile():
    gate = qr.OpenGate(3)
    ok(gate.take(2) == 2, "take(2) не взял 2")
    ok(gate.snapshot()[0] == 2, "после take(2) in_use != 2")
    ok(gate.acquire(threading.Event()) is True, "3-й пермит (последний) не взят")
    ok(gate.snapshot()[0] == 3, "in_use != 3 при полном гейте")
    ok(gate.take(1) == 0, "take сверх ёмкости вернул не 0")
    gate.release(); gate.release(); gate.release()



# ── 8. ActivePoller: generation растёт на успехе, НЕ на сбое/None ────────────────
def test_active_poller_generation():
    box = {"ret": None}                     # первый fetch → None (сбой)
    poller = qr.ActivePoller(lambda: box["ret"], interval=100)  # интервал большой, гоняем poll_once
    poller.poll_once()
    snap, gen = poller.read()
    ok(gen == 0 and snap is None, "None-fetch двинул generation (%r, %d)" % (snap, gen))
    box["ret"] = {"a", "b"}
    poller.poll_once()
    snap, gen = poller.read()
    ok(gen == 1 and snap == {"a", "b"}, "успешный fetch не обновил снапшот/gen (%r, %d)" % (snap, gen))
    box["ret"] = None                       # снова сбой → снапшот/gen НЕ меняются
    poller.poll_once()
    snap, gen = poller.read()
    ok(gen == 1 and snap == {"a", "b"}, "сбой после успеха сбросил снапшот (%r, %d)" % (snap, gen))



# ── 9. GATED-конвейер end-to-end: work держит слот в wait_closed, «оператор»
#      закрывает → следующий открывается; ≤cap ВСЕГДА; все в итоге закрыты ────────
def test_gated_conveyor():
    cap = 2
    gate = qr.OpenGate(cap)
    lock = threading.Lock()
    open_set = set()
    cur = {"c": 0, "max": 0, "closed": 0}

    def fetch():
        with lock:
            return set(open_set)                # снапшот открытых (как api.active_uuids)

    poller = qr.ActivePoller(fetch, interval=0.02)
    poller.start()

    def work(item, stop):
        with lock:
            open_set.add(item)
            cur["c"] += 1
            cur["max"] = max(cur["max"], cur["c"])
        r = qr.wait_closed(item, stop, poller, max_wait=10.0, need_absent=2, poll=0.02)
        assert r == "closed", "gated work: wait_closed=%r" % r
        with lock:
            cur["closed"] += 1

    stop_closer = threading.Event()
    def closer():                               # «оператор» закрывает открытые по одному
        while not stop_closer.is_set():
            time.sleep(0.04)
            with lock:
                if open_set:
                    victim = sorted(open_set)[0]
                    open_set.discard(victim)
                    cur["c"] -= 1
    ct = threading.Thread(target=closer, daemon=True); ct.start()

    qr.run_queue(["u%02d" % i for i in range(8)], work, cap, threading.Event(), gate=gate)
    stop_closer.set(); poller.stop()
    ok(cur["max"] <= cap, "gated: max одновременно %d > cap %d" % (cur["max"], cap))
    ok(cur["closed"] == 8, "gated: закрыто %d из 8" % cur["closed"])
    ok(len(open_set) == 0, "gated: остались открыты %d" % len(open_set))



# ── 10. ОБЩИЙ гейт на ДВЕ пачки разом (глобальный потолок «≤N»): суммарно
#       открыто ≤ gate.capacity, даже когда пул каждой пачки БОЛЬШЕ гейта ─────────
def test_shared_gate_across_batches():
    gate = qr.OpenGate(3)                       # общий потолок на обе пачки
    lock = threading.Lock()
    cur = {"c": 0, "max": 0, "done": 0}

    def work(item, stop):
        with lock:
            cur["c"] += 1; cur["max"] = max(cur["max"], cur["c"])
        time.sleep(0.02)
        with lock:
            cur["c"] -= 1; cur["done"] += 1

    def batch():
        qr.run_queue(range(10), work, 5, threading.Event(), gate=gate)  # пул 5, но гейт 3

    t1 = threading.Thread(target=batch); t2 = threading.Thread(target=batch)
    t1.start(); t2.start(); t1.join(); t2.join()
    ok(cur["max"] <= 3, "общий гейт: max %d > 3 (2 пачки по пулу 5)" % cur["max"])
    ok(cur["done"] == 20, "общий гейт: обработано %d из 20" % cur["done"])



# ── 11. Двойной release НЕ инфлирует ёмкость (BoundedSemaphore ValueError глушим) ─
def test_double_release_no_inflation():
    gate = qr.OpenGate(2)
    ok(gate.acquire(threading.Event()) is True, "acquire 1")
    gate.release()                              # in_use 0, sem назад к 2
    gate.release()                              # ПЕРЕрелиз — должен быть проглочен, НЕ инфляция
    ok(gate.take(2) == 2, "после перерелиза доступно != 2 (инфляция?)")
    ok(gate.take(1) == 0, "ёмкость выросла выше 2 — ИНФЛЯЦИЯ")
    gate.release(); gate.release()



# ── 12. Стоп ПОСРЕДИ прогона: часть обработана, остаток НЕ открыт ────────────────
def test_stop_mid_run():
    processed = []
    stop = threading.Event()

    def work(item, s):
        processed.append(item); time.sleep(0.03)

    def stopper():
        time.sleep(0.09); stop.set()
    t = threading.Thread(target=stopper); t.start()
    qr.run_queue(range(30), work, 1, stop)      # cap 1 → ~0.03с/профиль
    t.join()
    ok(0 < len(processed) < 30, "стоп посреди: обработано %d (ждём >0 и <30)" % len(processed))



# ── 13. Крайние случаи: пустой список, один элемент, need_absent=1 ───────────────
def test_edges():
    qr.run_queue([], lambda i, s: None, 3, threading.Event())         # пусто — без падений
    done = []
    qr.run_queue([42], lambda i, s: done.append(i), 3, threading.Event())
    ok(done == [42], "один элемент: %r" % done)
    p = FakePoller([(set(), 1)])                                       # need_absent=1 → сразу closed
    ok(qr.wait_closed("X", threading.Event(), p, max_wait=1.0, need_absent=1, poll=0.01) == "closed",
       "need_absent=1 не закрыл")



# ── 14. ActivePoller как реальный фоновый поток: старт/обновление/стоп ───────────
def test_active_poller_thread():
    box = {"r": {"a"}}
    p = qr.ActivePoller(lambda: box["r"], interval=0.02)
    p.start()
    ok(wait_until(lambda: p.read()[0] == {"a"} and p.read()[1] >= 1),
       "поток не обновил снапшот за таймаут: %r" % (p.read(),))
    box["r"] = {"b", "c"}
    ok(wait_until(lambda: p.read()[0] == {"b", "c"}),
       "поток не подхватил новый снапшот за таймаут: %r" % (p.read()[0],))
    p.stop()
    time.sleep(0.08)                            # дать доработать возможному in-flight fetch
    g1 = p.read()[1]
    time.sleep(0.15)
    ok(p.read()[1] == g1, "после stop() generation растёт (поток не остановлен)")



# ── 15. CloseReconciler юнит: дебаунс релиза, сброс на present, tick(None), независимость ──
def test_reconciler_unit():
    gate = qr.OpenGate(3)
    ok(gate.acquire(threading.Event()) and gate.acquire(threading.Event()), "2 пермита не взяты")
    ok(gate.snapshot()[0] == 2, "in_use != 2 до hand_off")
    rec = qr.CloseReconciler(need_absent=2)
    rec.hand_off("A", gate); rec.hand_off("B", gate)         # владение 2 пермитами → реконсилятору
    ok(rec.pending_count() == 2 and gate.snapshot()[0] == 2, "hand_off изменил in_use")
    # B закрылся (нет в active), A открыт: релиз B только после 2 tick подряд
    rec.tick({"A"})
    ok(gate.snapshot()[0] == 2, "B отпущен ДО дебаунса (1 tick)")
    rec.tick({"A"})
    ok(gate.snapshot()[0] == 1 and rec.pending_count() == 1, "B не отпущен после 2 tick")
    # tick(None) (сбой поллера) — ничего не трогает
    rec.tick(None)
    ok(gate.snapshot()[0] == 1 and rec.pending_count() == 1, "tick(None) что-то сделал")
    # A мигал (present сбрасывает счётчик отсутствий)
    rec.tick(set())                                          # A absent=1
    rec.tick({"A"})                                          # A present → reset
    ok(gate.snapshot()[0] == 1, "A отпущен рано (сброс не сработал)")
    rec.tick(set()); rec.tick(set())                        # A absent 2 → релиз
    ok(gate.snapshot()[0] == 0 and rec.pending_count() == 0, "A не отпущен в конце")



# ── 16. AUTO end-to-end: work оставляет окно (возвращает uuid), «оператор» закрывает,
#       реконсилятор возвращает пермит → следующий; ≤cap открыто; пул не залипает ──────
def test_auto_leave_open_conveyor():
    cap = 2
    gate = qr.OpenGate(cap)
    rec = qr.CloseReconciler(need_absent=2)
    lock = threading.Lock()
    open_set = set()
    st = {"max": 0, "cur": 0, "scraped": 0}

    def fetch():
        with lock:
            return set(open_set)

    poller = qr.ActivePoller(fetch, interval=0.02, on_snapshot=rec.tick)   # поллер питает реконсилятор
    poller.start()

    def work(item, stop):
        with lock:
            open_set.add(item)
            st["cur"] += 1; st["max"] = max(st["max"], st["cur"])
            st["scraped"] += 1                              # «скрейп» мгновенный
        return item                                        # AUTO: оставить открытым → hand_off

    stop_closer = threading.Event()
    def closer():                                          # «оператор» закрывает окна по одному
        while not stop_closer.is_set():
            time.sleep(0.04)
            with lock:
                if open_set:
                    v = sorted(open_set)[0]
                    open_set.discard(v); st["cur"] -= 1
    ct = threading.Thread(target=closer, daemon=True); ct.start()

    qr.run_queue(["u%02d" % i for i in range(8)], work, cap, threading.Event(), gate=gate, reconciler=rec)
    # все 8 «сняты» сразу (work вернул), пермиты у реконсилятора; дождаться возврата (окна закрылись)
    ok(wait_until(lambda: gate.snapshot()[0] == 0 and rec.pending_count() == 0, timeout=8),
       "пермиты не вернулись: in_use=%d pending=%d" % (gate.snapshot()[0], rec.pending_count()))
    stop_closer.set(); poller.stop()
    ok(st["scraped"] == 8, "скрейп %d из 8" % st["scraped"])
    ok(st["max"] <= cap, "AUTO: max открытых %d > cap %d" % (st["max"], cap))



# ── 17. Реконсилятор: эвикция забытого окна по max_hold (пермит не течёт вечно) ──
def test_reconciler_max_hold():
    gate = qr.OpenGate(2)
    gate.acquire(threading.Event())                          # 1 пермит под наблюдением
    logs = []
    rec = qr.CloseReconciler(need_absent=2, max_hold=0.05, emit=lambda m, k=None: logs.append(m))
    rec.hand_off("A", gate)
    rec.tick({"A"})                                          # окно открыто, таймаут не вышел
    ok(gate.snapshot()[0] == 1 and rec.pending_count() == 1, "эвикция ДО таймаута")
    time.sleep(0.06)
    rec.tick({"A"})                                          # окно ещё «открыто», но max_hold прошёл → эвикт
    ok(gate.snapshot()[0] == 0 and rec.pending_count() == 0, "забытое окно не эвиктнуто по таймауту")
    ok(any("висело" in m for m in logs), "нет лога эвикции по таймауту")



# ── 18. Реконсилятор: дубль hand_off (re-run на открытом окне) возвращает СТАРЫЙ пермит ──
def test_reconciler_dup_handoff():
    gate = qr.OpenGate(3)
    gate.acquire(threading.Event()); gate.acquire(threading.Event())   # 2 пермита (старый прогон + новый)
    rec = qr.CloseReconciler(need_absent=1)
    rec.hand_off("A", gate)
    ok(gate.snapshot()[0] == 2, "in_use != 2 до дубля")
    rec.hand_off("A", gate)                                  # дубль → вернуть старый пермит
    ok(gate.snapshot()[0] == 1, "УТЕЧКА: дубль hand_off не вернул старый пермит (in_use=%d)" % gate.snapshot()[0])
    ok(rec.pending_count() == 1, "дубль оставил лишнюю запись")
    rec.tick(set())                                          # окно закрылось (need_absent=1) → вернуть новый
    ok(gate.snapshot()[0] == 0 and rec.pending_count() == 0, "новый пермит не вернулся")



# ── 19. Реконсилятор: confirm_closed=False держит пермит (защита от усечённого active()) ──
def test_reconciler_confirm():
    gate = qr.OpenGate(2); gate.acquire(threading.Event())
    box = {"closed": False}
    rec = qr.CloseReconciler(need_absent=1, confirm_closed=lambda u: box["closed"])
    rec.hand_off("A", gate)
    rec.tick(set())                                          # A «отсутствует», но confirm=False → НЕ релиз
    ok(gate.snapshot()[0] == 1 and rec.pending_count() == 1, "confirm=False всё равно отпустил (усечённый active() не защищён)")
    box["closed"] = True
    rec.tick(set())                                          # confirm=True → релиз
    ok(gate.snapshot()[0] == 0 and rec.pending_count() == 0, "confirm=True не отпустил")



# ── 20. wait_closed: confirm_closed=False → НЕ 'closed' на усечённом снапшоте ────────────
def test_wait_closed_confirm():
    p = FakePoller([(set(), 1), (set(), 2), (set(), 3)])     # X отсутствует во всех поллах (усечение)
    r = qr.wait_closed("X", threading.Event(), p, max_wait=0.15, need_absent=2, poll=0.01,
                       confirm_closed=lambda u: False)
    ok(r == "timeout", "confirm=False дал %r, не timeout (брешь усечения открыта)" % r)
    p2 = FakePoller([(set(), 1), (set(), 2)])
    r2 = qr.wait_closed("X", threading.Event(), p2, max_wait=2.0, need_absent=2, poll=0.01,
                        confirm_closed=lambda u: True)
    ok(r2 == "closed", "confirm=True дал %r, не closed" % r2)



