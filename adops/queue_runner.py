"""
queue_runner — ограниченная очередь открытия профилей браузера: не более N открытых одновременно.

ЗАЧЕМ: наивный пакетный режим открывает ВСЕ профили пачки разом, и машина перегружается.
Здесь — ядро, которое держит НЕ БОЛЕЕ N одновременно ОТКРЫТЫХ
профилей: 1 слот = [открыть 1 → обработать → закрыть/дождаться закрытия → следующий].

ЧИСТЫЙ МОДУЛЬ: без pip-зависимостей и БЕЗ импорта GUI/движка/API. Всё внешнее (открытие,
закрытие, обработка, чтение active()) даёт вызывающий режим через колбэки `work` и `fetch_uuids`.
Поэтому тестируется на фейках без браузера.

КЛЮЧЕВЫЕ ИНВАРИАНТЫ (добыты состязательной критикой плана, НЕ ослаблять):
- Пермит OpenGate = один ОТКРЫТЫЙ профиль. Берётся ПЕРЕД открытием, отдаётся ТОЛЬКО когда
  профиль подтверждённо закрыт. Гарантия: открыто ≤ capacity ВСЕГДА.
- RELEASE строго в `finally` слота + флаг `acquired` (release ⟺ был успешный acquire) — иначе
  утечка пермита на любом исключении work → семафор ужимается до 0 → дедлог.
- ACQUIRE прерывается стопом (timeout-цикл, не блокирующий) — иначе очередь, ждущая пермит при
  всех занятых слотах, не услышит Стоп.
- wait_closed подтверждает закрытие по ДВУМ РАЗНЫМ реальным поллам active() (дебаунс), т.к.
  API профилей может вернуть усечённый успешный 200 → ложное «uuid отсутствует» → преждевременный слот.
- Один общий ActivePoller (не N штук) — источник снапшота active(); wait_closed читает его
  ПОКОЛЕНИЯ (generation), а не «сырое» время, поэтому два чтения одного снапшота дебаунс НЕ
  засчитает как два полла.

Политику «что закрывать / что оставлять открытым / что делать на timeout» решает КАЖДЫЙ сценарий
в своём `work` — здесь только механизм.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor


# ─────────────────────────────────────────────────────────────────────────────
# OpenGate — семафор на ЖИЗНЬ открытого профиля (жёсткий потолок «открыто ≤ capacity»)
# ─────────────────────────────────────────────────────────────────────────────
class OpenGate:
    """Пермит = один открытый профиль. Держится от открытия до подтверждённого закрытия.

    Может быть per-run (создаётся в движке на вызов) ИЛИ App-owned глобальным (общий потолок
    «≤N открыто» на ВСЕ режимы — требование продукта). BoundedSemaphore ловит перерелиз того же
    профиля подряд (ValueError → глушим), но exactly-once держится структурой (finally+acquired),
    а не им.
    """

    def __init__(self, capacity):
        cap = max(1, int(capacity or 1))
        self._cap = cap
        self._sem = threading.BoundedSemaphore(cap)
        self._lock = threading.Lock()
        self._in_use = 0            # для индикатора «Открыто: X/cap» (не для логики лимита)

    @property
    def capacity(self):
        return self._cap

    def acquire(self, stop_event, poll=0.3):
        """Взять пермит. Ждёт освобождения, НО прерывается stop_event (возврат False).
        НЕЛЬЗЯ голый sem.acquire(blocking=True) — он стоп не слышит → зависание на Стопе."""
        while stop_event is None or not stop_event.is_set():
            if self._sem.acquire(timeout=poll):
                with self._lock:
                    self._in_use += 1
                return True
            if stop_event is None:
                # без стопа — блокирующе (для тестов/особых случаев)
                self._sem.acquire()
                with self._lock:
                    self._in_use += 1
                return True
        return False

    def release(self):
        """Отдать пермит. Идемпотентен к перерелизу ТОГО ЖЕ профиля (BoundedSemaphore→ValueError
        глушим). Вызывать РОВНО один раз на каждый успешный acquire (гарантия — в _slot.finally)."""
        try:
            self._sem.release()
        except ValueError:
            # перерелиз (value уже == capacity) — count не растёт, но это симптом двойного
            # release; структурно не должно случаться (см. _slot). Молча не даём инфляции >cap.
            return
        with self._lock:
            self._in_use = max(0, self._in_use - 1)

    def take(self, k):
        """Best-effort зарезервировать k пермитов БЕЗ ожидания (для reconcile тёплых профилей:
        при старте прогона учесть уже-открытые окна). Возвращает сколько реально взято."""
        got = 0
        for _ in range(max(0, int(k))):
            if self._sem.acquire(blocking=False):
                got += 1
            else:
                break
        if got:
            with self._lock:
                self._in_use += got
        return got

    def snapshot(self):
        """(in_use, capacity) — для индикатора. in_use — учётный (может расходиться с реальным
        active() при внешних закрытиях; истина об окнах — ActivePoller)."""
        with self._lock:
            return (self._in_use, self._cap)


# ─────────────────────────────────────────────────────────────────────────────
# ActivePoller — ОДИН фоновый источник снапшота active() (общий для всех wait_closed)
# ─────────────────────────────────────────────────────────────────────────────
class ActivePoller:
    """Фоновый поток раз в `interval` зовёт fetch_uuids() → set(uuid) | None и публикует
    (снапшот, generation). generation растёт ТОЛЬКО на успешном полле (fetch вернул set) — на
    сбое/None снапшот и generation НЕ меняются (сбой не двигает дебаунс закрытия).

    App-owned (создаётся приложением с реальным fetch=api.active_uuids и пересоздаётся при
    ребинде порта). НЕ модульный глобал: `api` не существует при импорте — это был бы NameError.
    """

    def __init__(self, fetch_uuids, interval=2.0, on_snapshot=None):
        self._fetch = fetch_uuids
        self._interval = max(0.2, float(interval))
        self._on_snapshot = on_snapshot   # колбэк(set) на КАЖДОМ успешном полле (питает реконсилятор)
        self._lock = threading.Lock()
        self._snap = None           # set(uuid) | None
        self._gen = 0
        self._stop = threading.Event()
        self._thread = None

    def _publish(self, s):
        with self._lock:
            self._snap = s
            self._gen += 1
        if self._on_snapshot is not None:
            try:
                self._on_snapshot(s)      # реконсилятор.tick — сбой колбэка не роняет поллер
            except Exception:
                pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="active-poller", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                got = self._fetch()
            except Exception:
                got = None
            if got is not None:
                self._publish(set(got))
            self._stop.wait(self._interval)

    def poll_once(self):
        """Синхронно опросить и опубликовать (для reconcile при старте / тестов)."""
        try:
            got = self._fetch()
        except Exception:
            got = None
        if got is not None:
            self._publish(set(got))

    def read(self):
        """(снапшот|None, generation)."""
        with self._lock:
            return (None if self._snap is None else set(self._snap), self._gen)


# ─────────────────────────────────────────────────────────────────────────────
# CloseReconciler — отдаёт пермит гейта, когда «оставленное открытым» окно закрылось
# ─────────────────────────────────────────────────────────────────────────────
class CloseReconciler:
    """Для AUTO-сценариев (проверка состояния): профиль может ОСТАВАТЬСЯ открытым для
    ручного контроля, но рабочий поток обработки НЕ должен на нём залипать (иначе пачка из >N таких профилей
    встанет). Решение: work отдаёт пермит НЕ сам, а передаёт (hand_off) сюда; реконсилятор
    вернёт пермит гейта, когда окно уйдёт из active() (по снапшотам ActivePoller). Так пул
    обработки освобождается сразу, пачка догоняется, а суммарно открытых всё равно ≤ gate.capacity.

    exactly-once: пермит либо отдаёт _slot.finally (work вернул None = закрыл сам), ЛИБО
    реконсилятор (work вернул uuid = передал). Никогда оба (флаг handed в run_queue).

    ЗАЩИТЫ (после сверки волн против реального кода):
    - need_absent (деф. 2): усечённый /active даёт ложное отсутствие → релиз только после N tick.
    - confirm_closed(uuid)->bool (опц.): АВТОРИТЕТНАЯ per-uuid проверка ПЕРЕД релизом по отсутствию
      (усечённый 200 /active может держаться >N поллов → одного дебаунса мало; свежий точечный чек
      закрывает брешь). Нет confirm → релиз по дебаунсу (как раньше).
    - max_hold (опц., сек): забытое окно держит пермит ВЕЧНО → за смену гейт съедается →
      дедлок. По таймауту эвиктим пермит (живучесть > строгий ≤N для брошенного окна) + emit-лог.
    - hand_off ИДЕМПОТЕНТЕН к дубль-uuid (повторный прогон на ещё-открытом окне): старый
      пермит возвращается, новый берётся под наблюдение (иначе утечка старого пермита).
    """

    def __init__(self, need_absent=2, max_hold=None, confirm_closed=None, emit=None):
        self._need = max(1, int(need_absent))
        self._max_hold = max_hold
        self._confirm = confirm_closed
        self._emit = emit
        self._lock = threading.Lock()
        self._pending = {}          # uuid -> [gate, absent_count, t0]

    def hand_off(self, uuid, gate):
        """Принять владение пермитом gate для открытого uuid (вызывает run_queue).
        Дубль uuid → вернуть СТАРЫЙ пермит (новый прогон взял свой) — без утечки."""
        with self._lock:
            old = self._pending.get(uuid)
            self._pending[uuid] = [gate, 0, time.monotonic()]
        # дубль hand_off = два acquire на одно окно (re-run) → вернуть СТАРЫЙ пермит безусловно
        # (даже если это тот же объект gate — это ДРУГОЙ, лишний, пермит), иначе утечка
        if old is not None and old[0] is not None:
            old[0].release()

    def tick(self, active_set):
        """Вызывается ActivePoller на КАЖДОМ успешном полле с текущим set(uuid)."""
        if active_set is None:
            return
        now = time.monotonic()
        candidates = []             # (uuid, gate, why): 'timeout' | 'absent'
        with self._lock:
            for uuid, st in list(self._pending.items()):
                gate, absent, t0 = st
                if self._max_hold is not None and now - t0 >= self._max_hold:
                    candidates.append((uuid, gate, "timeout"))
                elif uuid not in active_set:
                    st[1] = absent + 1
                    if st[1] >= self._need:
                        candidates.append((uuid, gate, "absent"))
                else:
                    st[1] = 0
        # confirm/release — ВНЕ замка (confirm может делать сетевой вызов; не держать lock)
        for uuid, gate, why in candidates:
            if why == "absent" and self._confirm is not None:
                try:
                    closed = bool(self._confirm(uuid))
                except Exception:
                    closed = False
                if not closed:                          # ложное отсутствие (усечённый /active) → сброс
                    with self._lock:
                        cur = self._pending.get(uuid)
                        if cur is not None and cur[0] is gate:
                            cur[1] = 0
                    continue
            with self._lock:
                cur = self._pending.get(uuid)
                do = cur is not None and cur[0] is gate  # не тронуть, если uuid перевзят re-run'ом
                if do:
                    self._pending.pop(uuid, None)
            if do:
                gate.release()
                if why == "timeout" and self._emit:
                    try:
                        self._emit("queue: окно %s висело дольше лимита — пермит возвращён" % uuid, "warn")
                    except Exception:
                        pass

    def pending_count(self):
        with self._lock:
            return len(self._pending)


# ─────────────────────────────────────────────────────────────────────────────
# wait_closed — держит слот, пока профиль не закрыт (дебаунс по РАЗНЫМ поллам)
# ─────────────────────────────────────────────────────────────────────────────
def wait_closed(uuid, stop_event, poller, max_wait=1800.0, need_absent=2, poll=2.5, confirm_closed=None):
    """Ждёт, пока uuid ПОДТВЕРЖДЁННО уйдёт из active() (оператор закрыл / нажал «Готово»).

    Возврат: 'closed' | 'stopped' | 'timeout'.
    Дебаунс: 'closed' только когда uuid отсутствует в `need_absent` РАЗНЫХ поллах (по generation
    ActivePoller), а не в одном — усечённый 200 /active даёт ложное отсутствие в единичном полле.
    confirm_closed(uuid)->bool (опц.): АВТОРИТЕТНАЯ точечная проверка ПЕРЕД возвратом 'closed' —
    если усечение держится >need_absent поллов, дебаунса мало; свежий per-uuid чек закрывает брешь.
    max_wait — предохранитель от вечного залипания на забытом окне."""
    t0 = time.monotonic()
    absent = 0
    last_gen = None
    while True:
        if stop_event is not None and stop_event.is_set():
            return "stopped"
        if time.monotonic() - t0 >= max_wait:
            return "timeout"
        snap, gen = poller.read()
        # засчитываем только НОВЫЙ реальный полл (иначе два чтения одного снапшота = ложный дебаунс)
        if gen != last_gen:
            last_gen = gen
            if snap is not None:
                if uuid not in snap:
                    absent += 1
                    if absent >= need_absent:
                        if confirm_closed is None:
                            return "closed"
                        try:
                            if confirm_closed(uuid):
                                return "closed"
                        except Exception:
                            pass
                        absent = 0            # ложное отсутствие (усечённый /active) → ждём дальше
                else:
                    absent = 0
        # прерываемое ожидание
        if stop_event is not None:
            if stop_event.wait(poll):
                return "stopped"
        else:
            time.sleep(poll)


# ─────────────────────────────────────────────────────────────────────────────
# run_queue — пул на N слотов; открыто ≤ gate.capacity
# ─────────────────────────────────────────────────────────────────────────────
def run_queue(items, work, n, stop_event, gate=None, emit=None, reconciler=None):
    """Гоняет items через пул ≤ n. work(item, stop_event) САМ открывает профиль и обрабатывает.

    ДВА способа завершить слот (что вернул work):
      • work вернул None (GATED: внутри дождался wait_closed и/или закрыл сам) → пермит отдаётся
        в finally ПОСЛЕ возврата work (design A: поток держит слот весь ЖЦ окна).
      • work вернул uuid-строку (AUTO: оставил окно открытым для ручного контроля, обработка сделана) →
        пермит ПЕРЕДАётся `reconciler`, который вернёт его, когда окно уйдёт из active(); поток
        освобождается сразу (пул обработки не залипает на открытом окне).

    Гарантия «открыто ≤ gate.capacity»: пермит берётся ПЕРЕД work; отдаёт РОВНО один владелец —
    либо finally (None), либо reconciler (uuid). Никогда оба (флаг handed).

    - gate: OpenGate. По умолчанию per-run OpenGate(n). Для глобального потолка «≤N на всё
      приложение» передаётся ОБЩИЙ App-owned gate (тогда n — лишь размер пула).
    - reconciler: CloseReconciler (нужен, если work умеет возвращать uuid). Нет reconciler + work
      вернул uuid → пермит отдаётся в finally (деградация к design A, окно уйдёт из-под учёта).
    - stop_event: взведён → новые слоты не открываются; work сам чтит стоп (при запуске профиля и в wait_closed).
    - emit(msg, kind): опциональный лог.
    """
    n = max(1, int(n))
    gate = gate if gate is not None else OpenGate(n)
    items = list(items or [])

    def _slot(item):
        # (1) стоп ДО открытия — не открывать остаток пачки на Стопе
        if stop_event is not None and stop_event.is_set():
            return
        acquired = False
        handed = False
        try:
            # (2) пермит ПЕРЕД открытием, прерываемо стопом
            acquired = gate.acquire(stop_event)
            if not acquired:
                return                      # стоп во время ожидания слота
            res = work(item, stop_event)    # None=закрыл/дождался · uuid=оставил открытым
            if acquired and res is not None and reconciler is not None:
                reconciler.hand_off(res, gate)   # владение пермитом → реконсилятору
                handed = True
        except Exception as e:              # исключение work НЕ роняет пул и НЕ теряет пермит,
            if emit:                        # но ВИДНО (err+тип): баг обвязки/сигнатуры на КАЖДОМ
                try:                        # item дал бы тихую «—» на всю пачку — теперь заметен
                    emit("queue: сбой обработки (%s): %r" % (type(e).__name__, e), "err")
                except Exception:
                    pass
        finally:
            # (3) RELEASE ⟺ ACQUIRED и НЕ передан реконсилятору (exactly-once)
            if acquired and not handed:
                gate.release()

    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = []
        for it in items:
            if stop_event is not None and stop_event.is_set():
                break                        # перестаём даже ставить в очередь после Стопа
            futures.append(ex.submit(_slot, it))
        for f in futures:
            f.result()                       # исключения уже проглочены в _slot
    return gate
